"""Chain client for Injective blockchain interactions."""

import asyncio
import logging
from typing import Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from rfq_test.exceptions import (
    ChainConnectionError,
    ChainTimeoutError,
    InsufficientFundsError,
)
from rfq_test.models.config import ChainConfig

logger = logging.getLogger(__name__)


def get_subaccount_id(address: str, nonce: int = 0) -> str:
    """Derive exchange subaccount ID from bech32 address and nonce."""
    from bech32 import bech32_decode, convertbits

    _, data = bech32_decode(address)
    if data is None:
        raise ValueError(f"Invalid address: {address}")
    decoded = convertbits(data, 5, 8, False)
    if decoded is None:
        raise ValueError(f"Failed to decode address: {address}")
    address_hex = bytes(decoded).hex()
    nonce_hex = format(nonce, "024x")
    return "0x" + address_hex + nonce_hex


class ChainClient:
    """Client for interacting with Injective chain.
    
    Wraps injective-py for chain operations.
    """
    
    def __init__(self, config: ChainConfig):
        self.config = config
        self._network = None
        self._client = None
        self._composer = None
    
    async def connect(self) -> None:
        """Initialize connection to chain."""
        try:
            # Use v2 client
            from pyinjective.async_client_v2 import AsyncClient
            from pyinjective.core.network import Network
            
            # Optional endpoints: use config if set, else main grpc_endpoint (avoid "dns:///" noise).
            # See docs/INJECTIVE_GRPC_ENDPOINTS.md
            is_local = "localhost" in self.config.grpc_endpoint or "127.0.0.1" in self.config.grpc_endpoint
            grpc_main = self.config.grpc_endpoint
            grpc_exchange = getattr(self.config, "grpc_exchange_endpoint", None) or grpc_main
            grpc_explorer = getattr(self.config, "grpc_explorer_endpoint", None) or grpc_main
            chain_stream = getattr(self.config, "chain_stream_endpoint", None) or grpc_main
            if is_local:
                self._network = Network.custom(
                    lcd_endpoint=self.config.lcd_endpoint,
                    tm_websocket_endpoint="",
                    grpc_endpoint=grpc_main,
                    grpc_exchange_endpoint=grpc_exchange,
                    grpc_explorer_endpoint=grpc_explorer,
                    chain_id=self.config.chain_id,
                    env="local",
                    chain_stream_endpoint=chain_stream,
                    official_tokens_list_url="",
                )
            elif "888" in self.config.chain_id:
                self._network = Network.testnet()
            else:
                self._network = Network.custom(
                    lcd_endpoint=self.config.lcd_endpoint,
                    tm_websocket_endpoint="",
                    grpc_endpoint=grpc_main,
                    grpc_exchange_endpoint=grpc_exchange,
                    grpc_explorer_endpoint=grpc_explorer,
                    chain_id=self.config.chain_id,
                    env="local",
                    chain_stream_endpoint=chain_stream,
                    official_tokens_list_url="",
                )
            
            self._client = AsyncClient(self._network)
            await self._client.sync_timeout_height()
            
            logger.info(f"Connected to chain: {self.config.chain_id}")
            
        except Exception as e:
            raise ChainConnectionError(f"Failed to connect to chain: {e}") from e
    
    async def close(self) -> None:
        """Close chain connection."""
        if self._client:
            # AsyncClient v2 doesn't have a close method, just clear the reference
            # The client handles cleanup internally
            self._client = None
            logger.info("Chain connection closed")
    
    async def __aenter__(self) -> "ChainClient":
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
    
    @retry(
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    async def get_balance(self, address: str, denom: str = "inj") -> int:
        """Get account balance.
        
        Args:
            address: Injective address
            denom: Token denomination
            
        Returns:
            Balance in smallest unit
        """
        if not self._client:
            raise ChainConnectionError("Not connected")
        
        try:
            response = await self._client.fetch_bank_balance(
                address=address,
                denom=denom,
            )
            return int(response.get("balance", {}).get("amount", 0))
        except Exception as e:
            logger.error(f"Failed to get balance: {e}")
            raise
    
    async def wait_for_tx(
        self,
        tx_hash: str,
        timeout: Optional[float] = None,
    ) -> dict:
        """Wait for transaction confirmation.
        
        Args:
            tx_hash: Transaction hash
            timeout: Maximum wait time
            
        Returns:
            Transaction result
            
        Raises:
            ChainTimeoutError: If tx not confirmed within timeout
        """
        timeout = timeout or self.config.tx_timeout_seconds
        
        if not self._client:
            raise ChainConnectionError("Not connected")
        
        start = asyncio.get_event_loop().time()
        
        while (asyncio.get_event_loop().time() - start) < timeout:
            try:
                result = await self._client.fetch_tx(hash=tx_hash)
                if result:
                    return result
            except Exception:
                pass  # Tx not found yet
            
            await asyncio.sleep(0.5)
        
        raise ChainTimeoutError(f"Tx {tx_hash} not confirmed within {timeout}s")
    
    async def ensure_funded(
        self,
        address: str,
        min_balance: int = 1_000_000_000_000_000_000,  # 1 INJ
        denom: str = "inj",
    ) -> None:
        """Ensure account has minimum balance.
        
        Args:
            address: Injective address
            min_balance: Minimum required balance
            denom: Token denomination
            
        Raises:
            InsufficientFundsError: If balance below minimum
        """
        balance = await self.get_balance(address, denom)
        if balance < min_balance:
            raise InsufficientFundsError(
                f"Insufficient funds: {balance} < {min_balance} {denom}"
            )

    async def deposit_to_subaccount(
        self,
        private_key: str,
        sender_address: str,
        amount_wei: str,
        denom: str = "inj",
    ) -> str:
        """Deposit from bank to exchange subaccount so the wallet can trade derivatives.

        Args:
            private_key: Sender's private key (hex).
            sender_address: Sender's bech32 address.
            amount_wei: Amount in smallest unit (e.g. 1 INJ = 10^18).
            denom: Token denom (default inj).

        Returns:
            Transaction hash.

        Raises:
            ChainConnectionError: If not connected or broadcast fails.
        """
        if not self._client:
            await self.connect()

        from pyinjective.composer_v2 import Composer
        from pyinjective.core.broadcaster import MsgBroadcasterWithPk

        subaccount_id = get_subaccount_id(sender_address)
        composer = Composer(network=self._network.string())
        msg = composer.msg_deposit(
            sender=sender_address,
            subaccount_id=subaccount_id,
            amount=amount_wei,
            denom=denom,
        )
        broadcaster = MsgBroadcasterWithPk.new_using_simulation(
            network=self._network,
            private_key=private_key,
        )
        result = await broadcaster.broadcast([msg])

        tx_response = result.txResponse if hasattr(result, "txResponse") else result
        code = getattr(tx_response, "code", 0)
        if code and code != 0:
            raw_log = getattr(tx_response, "rawLog", "") or getattr(tx_response, "raw_log", "")
            raise ChainConnectionError(f"Deposit failed with code {code}: {raw_log}")

        tx_hash = getattr(tx_response, "txhash", None) or getattr(tx_response, "txHash", None)
        if not tx_hash:
            raise ChainConnectionError("Deposit succeeded but no tx hash returned")
        logger.info(f"Deposited to subaccount {sender_address[:20]}... (tx: {tx_hash[:16]}...)")
        return tx_hash

    async def grant_authz(
        self,
        private_key: str,
        grantee: str,
        msg_type: str = "/cosmos.bank.v1beta1.MsgSend",
        expire_in_seconds: int = 365 * 24 * 60 * 60,  # 1 year default
        spend_limit_amount: str = "1000000000000000000000",  # 1000 INJ default (in smallest unit) - unused, kept for compatibility
        spend_limit_denom: str = "inj",  # unused, kept for compatibility
    ) -> str:
        """Grant authorization to another address.
        
        This allows the grantee to execute specific message types on behalf of the granter.
        Used by Market Makers to grant the RFQ contract permission to send tokens for settlement.
        
        Creates a GenericAuthorization (not SendAuthorization) as expected by the RFQ contract.
        The contract expects GenericAuthorization for all message types, including MsgSend.
        
        Args:
            private_key: Granter's private key (the MM)
            grantee: Address receiving the grant (the RFQ contract)
            msg_type: Message type to authorize (default: MsgSend for bank transfers)
            expire_in_seconds: Grant expiration time in seconds (default: 1 year)
            spend_limit_amount: Unused, kept for compatibility
            spend_limit_denom: Unused, kept for compatibility
            
        Returns:
            Transaction hash
            
        Raises:
            ChainConnectionError: If not connected
        """
        from pyinjective.composer_v2 import Composer
        from pyinjective.core.broadcaster import MsgBroadcasterWithPk
        from pyinjective.wallet import PrivateKey
        
        if not self._network:
            await self.connect()
        
        # Get granter address from private key
        if private_key.startswith("0x"):
            private_key = private_key[2:]
        priv_key = PrivateKey.from_hex(private_key)
        granter_address = priv_key.to_public_key().to_address().to_acc_bech32()
        
        try:
            # Contract expects GenericAuthorization for all message types, including MsgSend
            # According to RFQ setup scripts, grants should have NO expiration (expiration: null)
            # However, pyinjective's msg_grant_generic requires expire_in parameter
            # We'll create the grant manually to set expiration to null
            
            from pyinjective.proto.cosmos.authz.v1beta1 import authz_pb2, tx_pb2 as authz_tx_pb2
            from google.protobuf import any_pb2
            
            # Create GenericAuthorization
            generic_authz = authz_pb2.GenericAuthorization()
            generic_authz.msg = msg_type
            
            # Wrap in Any
            authz_any = any_pb2.Any()
            authz_any.type_url = "/cosmos.authz.v1beta1.GenericAuthorization"
            authz_any.value = generic_authz.SerializeToString()
            
            # Create Grant
            # According to RFQ contract requirements, grants should have NO expiration (expiration: null)
            # Leave expiration field unset to create a permanent grant
            grant = authz_pb2.Grant()
            grant.authorization.CopyFrom(authz_any)
            # Do NOT set expiration - leave it unset to create expiration: null
            # This creates a permanent grant that can only be revoked manually
            
            # Create MsgGrant
            grant_msg = authz_tx_pb2.MsgGrant()
            grant_msg.granter = granter_address
            grant_msg.grantee = grantee
            grant_msg.grant.CopyFrom(grant)
            
            # Debug: Log the grant message structure
            logger.debug(f"Grant message type: {type(grant_msg)}")
            if hasattr(grant_msg, 'grant') and hasattr(grant_msg.grant, 'authorization'):
                authz_type_url = grant_msg.grant.authorization.type_url if hasattr(grant_msg.grant.authorization, 'type_url') else 'N/A'
                logger.debug(f"Grant authorization type_url: {authz_type_url}")
                if hasattr(grant_msg.grant, 'expiration'):
                    exp = grant_msg.grant.expiration
                    if exp.seconds == 0 and exp.nanos == 0:
                        logger.debug(f"Grant expiration: null (no expiration - permanent grant)")
                    else:
                        logger.debug(f"Grant expiration: {exp}")
                else:
                    logger.debug(f"Grant expiration: not set (no expiration - permanent grant)")
            
            # Log the grant message before broadcasting
            logger.info(f"Broadcasting grant: granter={granter_address}, grantee={grantee}, msg_type={msg_type}")
            logger.debug(f"Grant message: {grant_msg}")
            
            # Grant transactions have a known issue: simulation underestimates gas, causing
            # "out of gas" errors or even panic errors. We use gas heuristics from the start
            # to avoid these issues entirely.
            
            if not self._client:
                await self.connect()
            
            # Use gas heuristics from the start for grant transactions to avoid panic/out-of-gas errors
            # Gas heuristics provides a more reliable gas estimate than simulation for these transactions
            from pyinjective.core.broadcaster import MsgBroadcasterWithPk
            
            broadcaster = MsgBroadcasterWithPk.new_using_gas_heuristics(
                network=self._network,
                private_key=private_key,
            )
            
            try:
                result = await broadcaster.broadcast([grant_msg])
            except Exception as e:
                error_str = str(e)
                # Check if it's a sequence mismatch error (code=32)
                # This can happen if the sequence number changed between creating the broadcaster and broadcasting
                is_sequence_mismatch = (
                    'sequence mismatch' in error_str.lower() or 
                    'code=32' in error_str or
                    ('incorrect account sequence' in error_str.lower())
                )
                
                if is_sequence_mismatch:
                    # Sequence mismatch - wait a bit and retry with fresh broadcaster
                    logger.warning(f"Sequence mismatch detected, waiting for sequence to update...")
                    await asyncio.sleep(2)
                    # Create a fresh broadcaster which will query the current sequence
                    broadcaster_fresh = MsgBroadcasterWithPk.new_using_gas_heuristics(
                        network=self._network,
                        private_key=private_key,
                    )
                    result = await broadcaster_fresh.broadcast([grant_msg])
                else:
                    # For any other error, raise it - we're already using gas heuristics
                    raise
            
            # Convert result to expected format (match broadcaster's format)
            if isinstance(result, dict):
                tx_hash = result.get('txhash') or result.get('txHash')
                if not tx_hash:
                    tx_response = result.get('tx_response', result.get('txResponse', result))
                    if isinstance(tx_response, dict):
                        tx_hash = tx_response.get('txhash') or tx_response.get('txHash')
                    elif hasattr(tx_response, 'txhash'):
                        tx_hash = tx_response.txhash
                result = {'txResponse': {'txhash': tx_hash, 'code': result.get('code', 0)}} if tx_hash else result
            elif hasattr(result, 'txhash'):
                result = {'txResponse': {'txhash': result.txhash, 'code': getattr(result, 'code', 0)}}
            elif hasattr(result, 'tx_response'):
                tx_resp = result.tx_response
                tx_hash = tx_resp.txhash if hasattr(tx_resp, 'txhash') else (tx_resp.get('txhash') if isinstance(tx_resp, dict) else None)
                result = {'txResponse': {'txhash': tx_hash, 'code': getattr(result, 'code', 0)}} if tx_hash else result
            
            # Extract tx hash and check for errors
            tx_hash = None
            tx_code = None
            
            if isinstance(result, dict):
                if 'txResponse' in result and isinstance(result['txResponse'], dict):
                    tx_response = result['txResponse']
                    tx_hash = tx_response.get('txhash') or tx_response.get('txHash')
                    tx_code = tx_response.get('code')
                else:
                    tx_hash = result.get('txhash') or result.get('txHash') or result.get('tx_hash')
                    tx_code = result.get('code')
            elif hasattr(result, 'txResponse'):
                tx_response = result.txResponse
                if hasattr(tx_response, 'txhash'):
                    tx_hash = tx_response.txhash
                    tx_code = getattr(tx_response, 'code', None)
                elif isinstance(tx_response, dict):
                    tx_hash = tx_response.get('txhash') or tx_response.get('txHash')
                    tx_code = tx_response.get('code')
            elif hasattr(result, 'txhash'):
                tx_hash = result.txhash
                tx_code = getattr(result, 'code', None)
            else:
                tx_hash = getattr(result, 'txHash', None)
                tx_code = getattr(result, 'code', None)
            
            if not tx_hash:
                logger.error(f"Could not extract txhash from grant result: {result}")
                raise ChainConnectionError(f"Failed to get transaction hash from grant result: {result}")
            
            # Check if transaction failed during broadcast
            if tx_code is not None and tx_code != 0:
                error_msg = result.get('rawLog', 'Unknown error') if isinstance(result, dict) else 'Transaction failed'
                raise ChainConnectionError(f"Grant transaction failed with code {tx_code}: {error_msg}")
            
            logger.info(f"Authz grant created: {granter_address} -> {grantee} (tx: {tx_hash})")
            
            # Wait for transaction to be confirmed before returning
            # This ensures the account sequence is updated before the next transaction
            logger.info(f"Waiting for grant transaction to be confirmed...")
            tx_result = await self.wait_for_tx(tx_hash, timeout=15)
            
            # Debug: Log the full transaction result structure
            logger.debug(f"Full tx_result type: {type(tx_result)}")
            logger.debug(f"Full tx_result keys: {list(tx_result.keys()) if isinstance(tx_result, dict) else 'not a dict'}")
            
            # fetch_tx might return different structure - check both possibilities
            if isinstance(tx_result, dict):
                # Try different possible structures
                confirmed_code = (
                    tx_result.get('code') or
                    tx_result.get('tx_response', {}).get('code') or
                    tx_result.get('txResponse', {}).get('code') or
                    0
                )
                # Get tx_response from various possible locations
                tx_response = (
                    tx_result.get('tx_response') or
                    tx_result.get('txResponse') or
                    tx_result
                )
            else:
                # If it's an object, try to get attributes
                confirmed_code = getattr(tx_result, 'code', 0)
                tx_response = getattr(tx_result, 'tx_response', tx_result)
            
            logger.debug(f"Extracted confirmed_code: {confirmed_code}")
            logger.debug(f"tx_response type: {type(tx_response)}")
            logger.debug(f"tx_response keys: {list(tx_response.keys()) if isinstance(tx_response, dict) else 'not a dict'}")
            
            if confirmed_code == 0:
                logger.info(f"Grant transaction confirmed successfully")
                
                # Log transaction details for debugging
                # tx_response already extracted above
                tx_logs = tx_response.get('logs', []) if isinstance(tx_response, dict) else []
                tx_events = tx_response.get('events', []) if isinstance(tx_response, dict) else []
                
                logger.debug(f"tx_response keys: {list(tx_response.keys()) if isinstance(tx_response, dict) else 'not a dict'}")
                logger.debug(f"tx_response structure: {type(tx_response)}")
                
                if tx_logs:
                    logger.info(f"Transaction logs: {tx_logs}")
                else:
                    logger.warning(f"No logs found in transaction result")
                
                if tx_events:
                    # Prettify event output for better readability
                    from rfq_test.utils.formatting import format_events_summary
                    logger.info(f"Transaction events:\n{format_events_summary(tx_events, max_events=15)}")
                else:
                    logger.warning(f"No events found in transaction result")
                    # Check for any errors in logs
                    for log in tx_logs:
                        if isinstance(log, dict):
                            events = log.get('events', [])
                            for event in events:
                                event_type = event.get('type', '')
                                if 'error' in event_type.lower() or 'failure' in event_type.lower():
                                    logger.error(f"Error event in transaction: {event}")
                                # Check event attributes for errors
                                attrs = event.get('attributes', [])
                                for attr in attrs:
                                    if isinstance(attr, dict):
                                        key = attr.get('key', '')
                                        value = attr.get('value', '')
                                        if 'error' in str(key).lower() or 'error' in str(value).lower():
                                            logger.error(f"Error in event attribute: {attr}")
                
                # Check if grant was actually created by looking at events
                # tx_response already extracted above
                events = tx_events
                
                # Also check logs for grant-related events
                logs = tx_logs
                all_events = list(events) if events else []
                for log in logs:
                    if isinstance(log, dict) and 'events' in log:
                        log_events = log.get('events', [])
                        if log_events:
                            all_events.extend(log_events)
                
                grant_created = False
                for event in all_events:
                    event_type = event.get('type') if isinstance(event, dict) else getattr(event, 'type', None)
                    if event_type and ('authz' in event_type.lower() or 'grant' in event_type.lower()):
                        grant_created = True
                        logger.info(f"Grant-related event found: {event}")
                        break
                
                if not grant_created:
                    logger.error(f"⚠️  No grant event found in transaction!")
                    logger.error(f"⚠️  Events: {events}")
                    logger.error(f"⚠️  Logs: {logs}")
                    logger.error(f"⚠️  Transaction succeeded (code 0) but grant was not stored!")
                    logger.error(f"⚠️  This suggests the MsgGrant message was not processed by the chain")
                    
                    # Try to decode the transaction to see what messages were included
                    try:
                        tx_body = tx_response.get('tx', {}).get('body', {})
                        messages = tx_body.get('messages', [])
                        logger.error(f"⚠️  Transaction contained {len(messages)} message(s)")
                        for i, msg in enumerate(messages):
                            msg_type = msg.get('@type', msg.get('type_url', 'unknown'))
                            logger.error(f"⚠️  Message {i}: type={msg_type}")
                            # Try to log the grant details if available
                            if 'grant' in msg_type.lower() and isinstance(msg, dict):
                                grant_data = msg.get('grant', {})
                                logger.error(f"⚠️    Grant data: {grant_data}")
                    except Exception as e:
                        logger.debug(f"Could not inspect transaction body: {e}")
                    
                    # Don't fail here - let the query verification catch it
                    # But log a strong warning
                    logger.warning(f"⚠️  Grant event missing - grant may not have been created!")
                
                # Verify the grant exists by querying it
                # This is critical - if no grant event and query fails, the grant wasn't created
                grant_verified = False
                try:
                    await self._verify_grant_exists(granter_address, grantee, msg_type)
                    grant_verified = True
                    logger.info(f"Grant verified: {msg_type} from {granter_address} to {grantee}")
                except Exception as e:
                    logger.error(f"Grant verification failed: {e}")
                    if not grant_created:
                        # No event AND query failed - grant definitely wasn't created
                        raise ChainConnectionError(
                            f"Grant transaction succeeded but grant was not created. "
                            f"No grant event emitted and grant query failed. "
                            f"This indicates the MsgGrant message was not processed. "
                            f"Transaction: {tx_hash}, Error: {e}"
                        )
            else:
                # Transaction failed - check if it's out-of-gas (code 11)
                error_msg = tx_result.get('rawLog', tx_result.get('tx_response', {}).get('rawLog', 'Unknown error'))
                error_str = str(error_msg).lower()
                
                # Code 11 is typically "out of gas" in Cosmos SDK
                # If it's out-of-gas, retry with gas heuristics
                if confirmed_code == 11 and ('out of gas' in error_str or ('gaswanted' in error_str and 'gasused' in error_str)):
                    logger.warning(f"Grant transaction failed with code 11 (out of gas). Retrying with gas heuristics...")
                    
                    # Extract gas needed if available
                    import re
                    gas_match = re.search(r'gasused:\s*(\d+)', error_str)
                    if gas_match:
                        gas_needed = int(gas_match.group(1))
                        logger.info(f"Detected gas needed: {gas_needed}")
                    
                    # Retry with gas heuristics (sequence will be auto-incremented)
                    broadcaster_heuristic = MsgBroadcasterWithPk.new_using_gas_heuristics(
                        network=self._network,
                        private_key=private_key,
                    )
                    result_retry = await broadcaster_heuristic.broadcast([grant_msg])
                    
                    # Extract tx hash from retry result
                    if isinstance(result_retry, dict):
                        tx_hash_retry = result_retry.get('txhash') or result_retry.get('txHash')
                        if not tx_hash_retry:
                            tx_response_retry = result_retry.get('tx_response', result_retry.get('txResponse', result_retry))
                            if isinstance(tx_response_retry, dict):
                                tx_hash_retry = tx_response_retry.get('txhash') or tx_response_retry.get('txHash')
                    else:
                        tx_hash_retry = getattr(result_retry, 'txhash', None) or getattr(result_retry, 'txHash', None)
                    
                    if tx_hash_retry:
                        logger.info(f"Retry grant transaction: {tx_hash_retry}")
                        # Wait for retry transaction to confirm
                        tx_result_retry = await self.wait_for_tx(tx_hash_retry, timeout=15)
                        
                        # Check retry result
                        if isinstance(tx_result_retry, dict):
                            retry_code = tx_result_retry.get('code') or tx_result_retry.get('tx_response', {}).get('code') or 0
                        else:
                            retry_code = getattr(tx_result_retry, 'code', 0)
                        
                        if retry_code == 0:
                            logger.info(f"Retry grant transaction succeeded: {tx_hash_retry}")
                            # Verify the grant was created
                            await self._verify_grant_exists(granter_address, grantee, msg_type)
                            logger.info(f"Grant verified: {msg_type} from {granter_address} to {grantee}")
                            return tx_hash_retry
                        else:
                            raise ChainConnectionError(f"Grant transaction retry also failed with code {retry_code}")
                    else:
                        raise ChainConnectionError(f"Could not get tx hash from retry transaction")
                
                # Not out-of-gas, or retry failed - raise original error
                raise ChainConnectionError(f"Grant transaction failed with code {confirmed_code}: {error_msg}")
            
            return tx_hash
            
        except Exception as e:
            logger.error(f"Failed to create authz grant: {e}")
            raise ChainConnectionError(f"Failed to create authz grant: {e}") from e
    
    async def _verify_grant_exists(
        self,
        granter: str,
        grantee: str,
        msg_type: str,
    ) -> None:
        """Verify that a grant exists on-chain.
        
        Args:
            granter: Granter address
            grantee: Grantee address
            msg_type: Message type to check
            
        Raises:
            ChainConnectionError: If grant not found
        """
        if not self._client:
            raise ChainConnectionError("Not connected")
        
        # Query grants using AsyncClient.fetch_grants method
        # fetch_grants accepts optional msg_type_url parameter for filtering
        try:
            response = await self._client.fetch_grants(
                granter=granter,
                grantee=grantee,
                msg_type_url=msg_type,  # Filter by message type directly
            )
            
            # Response should contain grants list
            grants = response.get('grants', []) if isinstance(response, dict) else []
            
            logger.debug(f"Found {len(grants)} grant(s) for {msg_type} between {granter} and {grantee}")
            
            if grants:
                # Grant found (already filtered by msg_type_url)
                logger.debug(f"Grant verified: {msg_type} from {granter} to {grantee}")
                return
            else:
                # No matching grant found
                raise ChainConnectionError(f"Grant not found: {msg_type} from {granter} to {grantee}")
            
        except Exception as e:
            # Check if it's a "not found" error vs a connection error
            error_str = str(e).lower()
            if 'not found' in error_str or 'no grant' in error_str or isinstance(e, ChainConnectionError):
                raise
            else:
                # Connection or other error - re-raise so caller can decide
                logger.warning(f"Grant verification query failed: {e}")
                raise
