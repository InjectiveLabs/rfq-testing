"""Contract client for RFQ smart contract interactions."""

import asyncio
import base64
import json
import logging
import re
from decimal import Decimal
from typing import Optional

from rfq_test.exceptions import (
    ContractExecutionError,
    ContractUnauthorizedError,
    ContractValidationError,
)
from rfq_test.models.config import ContractConfig, ChainConfig
from rfq_test.models.types import Direction, Quote

logger = logging.getLogger(__name__)

_HEX_SIGNATURE_RE = re.compile(r"^(0x)?[0-9a-fA-F]+$")


def _get_sender_address(private_key: str) -> str:
    """Get Injective address from private key."""
    from pyinjective.wallet import PrivateKey
    
    # PrivateKey.from_hex expects hex WITHOUT 0x prefix
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    priv_key = PrivateKey.from_hex(private_key)
    pub_key = priv_key.to_public_key()
    return pub_key.to_address().to_acc_bech32()


def _normalize_contract_quote(quote: dict) -> dict:
    """Normalize quote payload to the contract's current JSON shape."""
    normalized = dict(quote)

    expiry = normalized.get("expiry")
    if isinstance(expiry, str):
        if expiry.isdigit():
            normalized["expiry"] = {"ts": int(expiry)}
    elif isinstance(expiry, int):
        normalized["expiry"] = {"ts": expiry}
    elif isinstance(expiry, dict):
        if "ts" in expiry or "h" in expiry:
            normalized["expiry"] = expiry
        elif "timestamp" in expiry:
            normalized["expiry"] = {"ts": int(expiry["timestamp"])}
        elif "height" in expiry:
            normalized["expiry"] = {"h": int(expiry["height"])}

    signature = normalized.get("signature")
    if isinstance(signature, str) and _HEX_SIGNATURE_RE.fullmatch(signature):
        sig_hex = signature[2:] if signature.startswith("0x") else signature
        sig_bytes = bytes.fromhex(sig_hex)
        normalized["signature"] = base64.b64encode(sig_bytes).decode("utf-8")

    return normalized


class ContractClient:
    """Client for interacting with the RFQ smart contract.

    Handles contract execution messages:
    - RegisterMaker / RevokeMaker (admin only)
    - AcceptQuote (taker)
    - CancelIntentLane / CancelAllIntents (taker — cancel conditional orders)
    """
    
    def __init__(
        self,
        contract_config: ContractConfig,
        chain_config: ChainConfig,
    ):
        self.contract_address = contract_config.address
        self.chain_config = chain_config
        self._network = None
        self._composer = None
        self._async_client = None
    
    async def _get_async_client(self):
        """Get or create async client for querying chain."""
        if self._async_client is None:
            from pyinjective.async_client_v2 import AsyncClient
            
            network = await self._get_network()
            self._async_client = AsyncClient(network)
        return self._async_client
    
    async def _wait_for_tx_result(self, tx_hash: str, timeout: float = 30.0) -> dict:
        """Wait for transaction to be confirmed and return full result.
        
        CRITICAL: The broadcast result only confirms tx was accepted into mempool.
        We MUST query the chain to get the actual execution result (code, rawLog).
        
        Args:
            tx_hash: Transaction hash from broadcast
            timeout: Maximum time to wait for confirmation
            
        Returns:
            Dict with 'code', 'rawLog', and other tx fields
            
        Raises:
            ContractExecutionError: If tx not found within timeout
        """
        import httpx
        
        start = asyncio.get_event_loop().time()
        
        # Use LCD endpoint for tx query - more reliable than gRPC in some environments
        lcd_url = self.chain_config.lcd_endpoint.rstrip('/')
        
        while (asyncio.get_event_loop().time() - start) < timeout:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    # Query tx by hash from LCD
                    response = await client.get(f"{lcd_url}/cosmos/tx/v1beta1/txs/{tx_hash}")
                    
                    if response.status_code == 200:
                        data = response.json()
                        tx_response = data.get('tx_response', {})
                        
                        # Return the relevant fields
                        return {
                            'code': tx_response.get('code', 0),
                            'rawLog': tx_response.get('raw_log', ''),
                            'txhash': tx_response.get('txhash', tx_hash),
                            'height': tx_response.get('height', ''),
                            'gasUsed': tx_response.get('gas_used', ''),
                            'gasWanted': tx_response.get('gas_wanted', ''),
                            'events': tx_response.get('events', []),
                        }
                    elif response.status_code == 404:
                        # Tx not found yet, keep waiting
                        pass
                    else:
                        logger.debug(f"LCD query returned {response.status_code}: {response.text[:200]}")
            except Exception as e:
                logger.debug(f"Error querying tx {tx_hash}: {e}")
            
            await asyncio.sleep(1.0)
        
        raise ContractExecutionError(f"Transaction {tx_hash} not confirmed within {timeout}s")
    
    async def _get_network(self):
        """Get or create network instance."""
        if self._network is None:
            from pyinjective.core.network import Network
            
            # Check if this is a local network by looking at the grpc endpoint
            is_local = "localhost" in self.chain_config.grpc_endpoint or "127.0.0.1" in self.chain_config.grpc_endpoint
            
            # Optional endpoints: use config if set, else main grpc_endpoint (avoid "dns:///" noise).
            # See docs/INJECTIVE_GRPC_ENDPOINTS.md and docs/PYINJECTIVE_GRPC_NOISE.md
            grpc_main = self.chain_config.grpc_endpoint
            grpc_exchange = getattr(self.chain_config, "grpc_exchange_endpoint", None) or grpc_main
            grpc_explorer = getattr(self.chain_config, "grpc_explorer_endpoint", None) or grpc_main
            chain_stream = getattr(self.chain_config, "chain_stream_endpoint", None) or grpc_main
            if is_local:
                self._network = Network.custom(
                    lcd_endpoint=self.chain_config.lcd_endpoint,
                    tm_websocket_endpoint="",
                    grpc_endpoint=grpc_main,
                    grpc_exchange_endpoint=grpc_exchange,
                    grpc_explorer_endpoint=grpc_explorer,
                    chain_id=self.chain_config.chain_id,
                    env="local",
                    chain_stream_endpoint=chain_stream,
                    official_tokens_list_url="",
                )
            elif "888" in self.chain_config.chain_id:
                self._network = Network.testnet()
            else:
                self._network = Network.custom(
                    lcd_endpoint=self.chain_config.lcd_endpoint,
                    tm_websocket_endpoint="",
                    grpc_endpoint=grpc_main,
                    grpc_exchange_endpoint=grpc_exchange,
                    grpc_explorer_endpoint=grpc_explorer,
                    chain_id=self.chain_config.chain_id,
                    env="local",
                    chain_stream_endpoint=chain_stream,
                    official_tokens_list_url="",
                )
        return self._network
    
    # Contract list_makers pagination: max limit is 20; next page uses start_after=<last address>
    LIST_MAKERS_PAGE_LIMIT = 20

    async def query_makers(self, limit: int = LIST_MAKERS_PAGE_LIMIT) -> set[str]:
        """Query all whitelisted makers from the contract.
        
        Uses contract pagination: first page {"list_makers": {}}, then
        {"list_makers": {"start_after": "<last address>", "limit": 20}} until no more.
        Contract max limit per page is 20.
        
        Args:
            limit: Max makers per page (contract allows at most 20).
            
        Returns:
            Set of maker addresses (bech32 format)
        """
        import httpx
        
        page_limit = min(int(limit), self.LIST_MAKERS_PAGE_LIMIT)
        all_makers: set[str] = set()
        start_after = None
        
        # Use LCD REST API for queries (more reliable than gRPC for smart contract queries)
        lcd_endpoint = self.chain_config.lcd_endpoint.rstrip('/')
        
        while True:
            # Build query: first page {"list_makers": {}}, then with start_after
            query_msg: dict = {"list_makers": {"limit": page_limit}}
            if start_after:
                query_msg["list_makers"]["start_after"] = start_after
            
            # Base64 encode the query
            query_json = json.dumps(query_msg)
            query_b64 = base64.b64encode(query_json.encode()).decode()
            
            # Query via LCD REST API
            url = f"{lcd_endpoint}/cosmwasm/wasm/v1/contract/{self.contract_address}/smart/{query_b64}"
            
            try:
                async with httpx.AsyncClient(timeout=30.0) as http_client:
                    response = await http_client.get(url)
                    response.raise_for_status()
                    
                    result = response.json()
                    
                    # Response format: {"data": {"makers": {...}, "next_key": ...}} or makers as dict keyed by address
                    data = result.get("data", result)
                    
                    # Extract makers from response (makers can be dict address -> value)
                    makers_dict = data.get("makers", {})
                    for addr in makers_dict.keys():
                        all_makers.add(addr)
                    
                    # Next page: contract may return next_key, or we use last address when we got a full page
                    next_key = data.get("next_key")
                    if next_key:
                        start_after = next_key
                    elif len(makers_dict) >= page_limit:
                        # Full page but no next_key: use last address (contract expects start_after = last address)
                        start_after = list(makers_dict.keys())[-1] if makers_dict else None
                    else:
                        start_after = None
                    if not start_after or len(makers_dict) < page_limit:
                        break
                    
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error querying makers: {e.response.status_code} - {e.response.text}")
                raise ContractExecutionError(f"Query makers failed: {e}") from e
            except Exception as e:
                logger.error(f"Failed to query makers: {e}")
                raise ContractExecutionError(f"Query makers failed: {e}") from e
        
        logger.info(f"Found {len(all_makers)} whitelisted makers")
        return all_makers
    
    async def is_maker_registered(self, maker_address: str) -> bool:
        """Check if a maker address is whitelisted.
        
        Args:
            maker_address: The bech32 address to check
            
        Returns:
            True if maker is whitelisted, False otherwise
        """
        makers = await self.query_makers()
        return maker_address in makers
    
    async def register_maker(
        self,
        private_key: str,
        maker_address: str,
    ) -> str:
        """Register a market maker (admin only).
        
        Args:
            private_key: Admin's private key
            maker_address: Address to register as MM
            
        Returns:
            Transaction hash
            
        Raises:
            ContractUnauthorizedError: If sender is not admin
            ContractValidationError: If maker already registered
        """
        from pyinjective.composer_v2 import Composer
        from pyinjective.core.broadcaster import MsgBroadcasterWithPk
        
        network = await self._get_network()
        sender_address = _get_sender_address(private_key)
        
        # Detailed logging for debugging authorization issues
        # Verify the private key derives to the expected sender address
        from pyinjective.wallet import PrivateKey as PK
        pk_for_verify = private_key[2:] if private_key.startswith("0x") else private_key
        derived_address = PK.from_hex(pk_for_verify).to_public_key().to_address().to_acc_bech32()
        
        logger.info(f"RegisterMaker: sender_address={sender_address}")
        logger.info(f"RegisterMaker: derived_from_pk={derived_address}")
        logger.info(f"RegisterMaker: maker={maker_address}")
        logger.info(f"RegisterMaker: contract={self.contract_address}")
        
        if sender_address != derived_address:
            logger.error(f"MISMATCH! sender_address={sender_address} != derived={derived_address}")
        assert sender_address == derived_address, f"Sender address mismatch: {sender_address} != {derived_address}"
        
        msg = {
            "register_maker": {
                "maker": maker_address,
            }
        }
        
        try:
            # For E2E testing, we want real transactions, not simulations
            # Use gas heuristics to estimate gas and broadcast directly
            # This ensures we test the actual on-chain execution
            broadcaster = MsgBroadcasterWithPk.new_using_gas_heuristics(
                network=network,
                private_key=private_key,
            )
            
            composer = Composer(network=network.string())
            execute_msg = composer.msg_execute_contract(
                sender=sender_address,
                contract=self.contract_address,
                msg=json.dumps(msg),
            )
            
            result = await broadcaster.broadcast([execute_msg])
            
            # Extract tx_hash from broadcast result (mempool acceptance)
            tx_response = None
            if isinstance(result, dict):
                tx_response = result.get('txResponse', result)
            elif hasattr(result, 'txResponse'):
                tx_response = result.txResponse
            else:
                tx_response = result
            
            tx_hash = None
            if isinstance(tx_response, dict):
                tx_hash = tx_response.get('txhash') or tx_response.get('txHash')
            else:
                tx_hash = getattr(tx_response, 'txhash', None) or getattr(tx_response, 'txHash', None)
            
            if not tx_hash:
                logger.error(f"Could not extract txhash from result: {result}")
                raise ContractExecutionError(f"Failed to get transaction hash from broadcast result: {result}")
            
            logger.info(f"RegisterMaker broadcast accepted: {tx_hash} (sender={sender_address})")
            
            # CRITICAL: Wait for transaction to be confirmed and get actual execution result
            # The broadcast result only confirms tx was accepted into mempool (code=0).
            # We MUST query the chain to get the actual execution result.
            logger.info(f"RegisterMaker: waiting for tx confirmation...")
            tx_result = await self._wait_for_tx_result(tx_hash, timeout=30.0)
            
            code = tx_result.get('code', 0)
            raw_log = tx_result.get('rawLog', '')
            
            logger.info(f"RegisterMaker confirmed: code={code}, rawLog={str(raw_log)[:200]}")
            
            # If code is non-zero, transaction failed on-chain
            if code and code != 0:
                error_msg = raw_log or f"Transaction failed with code {code}"
                logger.warning(f"RegisterMaker tx failed (code={code}): {error_msg}")
                
                error_lower = error_msg.lower()
                if "unauthorized" in error_lower:
                    raise ContractUnauthorizedError(f"Not admin: {error_msg}")
                if "already registered" in error_lower:
                    raise ContractValidationError(f"Maker already registered: {error_msg}")
                raise ContractExecutionError(f"RegisterMaker failed: {error_msg}")
            
            logger.info(f"RegisterMaker tx SUCCESS: {tx_hash} (sender={sender_address})")
            return tx_hash
            
        except (ContractUnauthorizedError, ContractValidationError, ContractExecutionError):
            # Re-raise our custom exceptions
            raise
        except Exception as e:
            error_msg = str(e).lower()
            logger.warning(f"RegisterMaker error (sender={sender_address}): {e}")
            if "unauthorized" in error_msg:
                raise ContractUnauthorizedError(f"Not admin: {e}") from e
            if "already registered" in error_msg:
                raise ContractValidationError(f"Maker already registered: {e}") from e
            raise ContractExecutionError(f"RegisterMaker failed: {e}") from e
    
    async def revoke_maker(
        self,
        private_key: str,
        maker_address: str,
    ) -> str:
        """Revoke a market maker (admin only).
        
        Args:
            private_key: Admin's private key
            maker_address: Address to revoke
            
        Returns:
            Transaction hash
        """
        from pyinjective.composer_v2 import Composer
        from pyinjective.core.broadcaster import MsgBroadcasterWithPk
        
        network = await self._get_network()
        sender_address = _get_sender_address(private_key)
        
        # Detailed logging for debugging authorization issues
        # Verify the private key derives to the expected sender address
        from pyinjective.wallet import PrivateKey as PK
        pk_for_verify = private_key[2:] if private_key.startswith("0x") else private_key
        derived_address = PK.from_hex(pk_for_verify).to_public_key().to_address().to_acc_bech32()
        
        logger.info(f"RevokeMaker: sender_address={sender_address}")
        logger.info(f"RevokeMaker: derived_from_pk={derived_address}")
        logger.info(f"RevokeMaker: maker={maker_address}")
        logger.info(f"RevokeMaker: contract={self.contract_address}")
        
        if sender_address != derived_address:
            logger.error(f"MISMATCH! sender_address={sender_address} != derived={derived_address}")
        assert sender_address == derived_address, f"Sender address mismatch: {sender_address} != {derived_address}"
        
        msg = {
            "revoke_maker": {
                "maker": maker_address,
            }
        }
        
        try:
            # For E2E testing, we want real transactions, not simulations
            # Use gas heuristics to estimate gas and broadcast directly
            # This ensures we test the actual on-chain execution
            broadcaster = MsgBroadcasterWithPk.new_using_gas_heuristics(
                network=network,
                private_key=private_key,
            )
            
            composer = Composer(network=network.string())
            execute_msg = composer.msg_execute_contract(
                sender=sender_address,
                contract=self.contract_address,
                msg=json.dumps(msg),
            )
            
            result = await broadcaster.broadcast([execute_msg])
            
            # Extract tx_hash from broadcast result (mempool acceptance)
            tx_response = None
            if isinstance(result, dict):
                tx_response = result.get('txResponse', result)
            elif hasattr(result, 'txResponse'):
                tx_response = result.txResponse
            else:
                tx_response = result
            
            tx_hash = None
            if isinstance(tx_response, dict):
                tx_hash = tx_response.get('txhash') or tx_response.get('txHash')
            else:
                tx_hash = getattr(tx_response, 'txhash', None) or getattr(tx_response, 'txHash', None)
            
            if not tx_hash:
                logger.error(f"Could not extract txhash from result: {result}")
                raise ContractExecutionError(f"Failed to get transaction hash from broadcast result: {result}")
            
            logger.info(f"RevokeMaker broadcast accepted: {tx_hash} (sender={sender_address})")
            
            # CRITICAL: Wait for transaction to be confirmed and get actual execution result
            # The broadcast result only confirms tx was accepted into mempool (code=0).
            # We MUST query the chain to get the actual execution result.
            logger.info(f"RevokeMaker: waiting for tx confirmation...")
            tx_result = await self._wait_for_tx_result(tx_hash, timeout=30.0)
            
            code = tx_result.get('code', 0)
            raw_log = tx_result.get('rawLog', '')
            
            logger.info(f"RevokeMaker confirmed: code={code}, rawLog={str(raw_log)[:200]}")
            
            # If code is non-zero, transaction failed on-chain
            if code and code != 0:
                error_msg = raw_log or f"Transaction failed with code {code}"
                logger.warning(f"RevokeMaker tx failed (code={code}): {error_msg}")
                
                error_lower = error_msg.lower()
                if "unauthorized" in error_lower:
                    raise ContractUnauthorizedError(f"Not admin: {error_msg}")
                if "not registered" in error_lower:
                    raise ContractValidationError(f"Maker not registered: {error_msg}")
                raise ContractExecutionError(f"RevokeMaker failed: {error_msg}")
            
            logger.info(f"RevokeMaker tx SUCCESS: {tx_hash} (sender={sender_address})")
            return tx_hash
            
        except (ContractUnauthorizedError, ContractValidationError, ContractExecutionError):
            # Re-raise our custom exceptions
            raise
        except Exception as e:
            error_msg = str(e).lower()
            logger.warning(f"RevokeMaker error (sender={sender_address}): {e}")
            if "unauthorized" in error_msg:
                raise ContractUnauthorizedError(f"Not admin: {e}") from e
            raise ContractExecutionError(f"RevokeMaker failed: {e}") from e
    
    async def accept_quote(
        self,
        private_key: str,
        quotes: list[dict],
        rfq_id: str,
        market_id: str,
        direction: Direction,
        margin: Decimal,
        quantity: Decimal,
        worst_price: Optional[Decimal] = None,
        unfilled_action: Optional[dict] = None,
        cid: Optional[str] = None,
    ) -> str:
        """Accept quote(s) and settle trade.
        
        Args:
            private_key: Taker's private key
            quotes: List of quotes to accept (empty for orderbook settlement)
            rfq_id: Request ID (nonce)
            market_id: Market ID
            direction: Trade direction
            margin: Margin amount
            quantity: Trade quantity
            worst_price: Maximum price willing to pay (for long) or minimum to receive (for short)
            unfilled_action: Optional action for unfilled quantity. Can be:
                - None: No fallback, only fill via RFQ quotes
                - {"limit": {"price": "4.5"}}: Place limit order for unfilled at specified price
                - {"market": {}}: Place market order (IOC) for unfilled at worst_price
            cid: Optional settlement CID attached to any orderbook execution created by AcceptQuote
            
        Returns:
            Transaction hash
        """
        from pyinjective.composer_v2 import Composer
        from pyinjective.core.broadcaster import MsgBroadcasterWithPk
        
        network = await self._get_network()
        sender_address = _get_sender_address(private_key)
        
        # Build accept_quote message
        # Fields are at top level, not nested under 'args'
        # Contract expects rfq_id as number (uint64), direction as integer (0=Long, 1=Short)
        # worst_price is required by the contract
        # Convert rfq_id to int if it's a string (from test input)
        rfq_id_int = int(rfq_id) if isinstance(rfq_id, str) else rfq_id
        
        # Contract expects Direction as lowercase string ("long" or "short"), not integer!
        # The indexer uses 0/1, but the contract uses lowercase "long"/"short"
        direction_str = "long" if direction == Direction.LONG else "short"
        
        accept_msg = {
            "rfq_id": rfq_id_int,  # Contract expects uint64 (number, not string)
            "market_id": market_id,
            "direction": direction_str,  # Contract expects "Long" or "Short" (string)
            "margin": str(margin),
            "quantity": str(quantity),
            "quotes": quotes,
        }
        
        # Add worst_price if provided (or use quote price as fallback)
        if worst_price is not None:
            accept_msg["worst_price"] = str(worst_price)
        elif quotes and "price" in quotes[0]:
            # Use first quote price as worst_price fallback
            accept_msg["worst_price"] = quotes[0]["price"]
        
        # Add unfilled_action (contract expects Option<PostUnfilledAction>)
        # Can be: None, {"limit": {"price": "X"}}, or {"market": {}}
        accept_msg["unfilled_action"] = unfilled_action
        if cid:
            accept_msg["cid"] = cid

        accept_msg["quotes"] = [_normalize_contract_quote(quote) for quote in quotes]
        
        msg = {
            "accept_quote": accept_msg
        }
        
        # Debug: log the message being sent (use INFO so it shows in test output)
        logger.info(f"AcceptQuote message: {json.dumps(msg, indent=2)}")
        
        try:
            # For E2E testing, we want real transactions with accurate gas estimation
            # Use simulation-based gas estimation for complex contract calls
            # AcceptQuote involves multiple submessages and can use significant gas
            broadcaster = MsgBroadcasterWithPk.new_using_simulation(
                network=network,
                private_key=private_key,
            )
            
            composer = Composer(network=network.string())
            # Use ensure_ascii=False and sort_keys=False to preserve structure
            msg_json = json.dumps(msg, ensure_ascii=False)
            logger.info(f"Sending JSON to contract: {msg_json}")
            execute_msg = composer.msg_execute_contract(
                sender=sender_address,
                contract=self.contract_address,
                msg=msg_json,
            )
            
            result = await broadcaster.broadcast([execute_msg])
            
            # Extract tx_hash from broadcast result (mempool acceptance)
            tx_response = None
            if isinstance(result, dict):
                tx_response = result.get('txResponse', result)
            elif hasattr(result, 'txResponse'):
                tx_response = result.txResponse
            else:
                tx_response = result
            
            tx_hash = None
            if isinstance(tx_response, dict):
                tx_hash = tx_response.get('txhash') or tx_response.get('txHash')
            else:
                tx_hash = getattr(tx_response, 'txhash', None) or getattr(tx_response, 'txHash', None)
            
            if not tx_hash:
                logger.error(f"Could not extract txhash from result: {result}")
                raise ContractExecutionError(f"Failed to get transaction hash from broadcast result: {result}")
            
            logger.info(f"AcceptQuote broadcast accepted: {tx_hash}")
            
            # CRITICAL: Wait for transaction to be confirmed and get actual execution result
            # The broadcast result only confirms tx was accepted into mempool (code=0).
            # We MUST query the chain to get the actual execution result.
            logger.info(f"AcceptQuote: waiting for tx confirmation...")
            tx_result = await self._wait_for_tx_result(tx_hash, timeout=30.0)
            
            code = tx_result.get('code', 0)
            raw_log = tx_result.get('rawLog', '')
            
            logger.info(f"AcceptQuote confirmed: code={code}, rawLog={str(raw_log)[:200]}")
            
            # If code is non-zero, transaction failed on-chain
            if code and code != 0:
                error_msg = raw_log or f"Transaction failed with code {code}"
                logger.warning(f"AcceptQuote tx failed (code={code}): {error_msg}")
                
                error_lower = error_msg.lower()
                if "expired" in error_lower:
                    raise ContractValidationError(f"Quote expired: {error_msg}")
                if "signature" in error_lower:
                    raise ContractValidationError(f"Invalid signature: {error_msg}")
                if "maker not registered" in error_lower:
                    raise ContractValidationError(f"Maker not registered: {error_msg}")
                if "nonce" in error_lower or "replay" in error_lower:
                    raise ContractValidationError(f"Nonce error: {error_msg}")
                if "worst price" in error_lower:
                    raise ContractValidationError(f"Price validation failed: {error_msg}")
                if "unauthorized" in error_lower:
                    raise ContractUnauthorizedError(f"Unauthorized: {error_msg}")
                raise ContractExecutionError(f"AcceptQuote failed: {error_msg}")
            
            logger.info(f"AcceptQuote tx SUCCESS: {tx_hash}")
            return tx_hash
            
        except (ContractUnauthorizedError, ContractValidationError, ContractExecutionError):
            # Re-raise our custom exceptions
            raise
        except Exception as e:
            error_msg = str(e).lower()
            if "expired" in error_msg:
                raise ContractValidationError(f"Quote expired: {e}") from e
            if "signature" in error_msg:
                raise ContractValidationError(f"Invalid signature: {e}") from e
            if "maker not registered" in error_msg:
                raise ContractValidationError(f"Maker not registered: {e}") from e
            if "nonce" in error_msg or "replay" in error_msg:
                raise ContractValidationError(f"Nonce error: {e}") from e
            raise ContractExecutionError(f"AcceptQuote failed: {e}") from e

    async def cancel_intent_lane(
        self,
        private_key: str,
        market_id: str,
        subaccount_nonce: int = 0,
    ) -> str:
        """Cancel all active conditional orders for a specific market lane.

        Calls ``CancelIntentLane { market_id, subaccount_nonce }`` on the RFQ contract.
        The contract increments the lane version for (taker, market_id, subaccount_nonce).
        The indexer then marks any conditional orders signed with the previous lane version
        as cancelled.

        Use this to cancel conditional orders on a single market without affecting orders
        on other markets. To cancel across all markets in one call, use cancel_all_intents().

        Args:
            private_key: Taker's private key. The sender's address becomes the taker.
            market_id: Derivative market ID (0x hex string).
            subaccount_nonce: Subaccount index used when the orders were created (default 0).

        Returns:
            Transaction hash.
        """
        msg = {
            "cancel_intent_lane": {
                "market_id": market_id,
                "subaccount_nonce": subaccount_nonce,
            }
        }
        logger.info("CancelIntentLane: market=%s subaccount_nonce=%d", market_id[:20], subaccount_nonce)
        return await self.execute_contract_msg(private_key=private_key, msg=msg)

    async def cancel_all_intents(
        self,
        private_key: str,
    ) -> str:
        """Cancel ALL active conditional orders for a taker across every market.

        Calls ``CancelAllIntents {}`` on the RFQ contract. The contract increments
        the taker's epoch. The indexer then cancels every conditional order for this
        taker that was signed with the previous epoch.

        Use this for a full reset — all pending TP/SL orders on all markets are
        cancelled in one transaction.

        Args:
            private_key: Taker's private key.

        Returns:
            Transaction hash.
        """
        msg = {"cancel_all_intents": {}}
        logger.info("CancelAllIntents")
        return await self.execute_contract_msg(private_key=private_key, msg=msg)

    async def execute_contract_msg(
        self,
        private_key: str,
        msg: dict,
    ) -> str:
        """Execute an arbitrary contract message and return the transaction hash.

        Waits for on-chain confirmation and raises ContractExecutionError on failure.

        Args:
            private_key: Sender's private key (hex, with or without 0x prefix)
            msg: Contract execute message as a dict (e.g. {"cancel_all_intents": {}})

        Returns:
            Transaction hash string.
        """
        from pyinjective.composer_v2 import Composer
        from pyinjective.core.broadcaster import MsgBroadcasterWithPk

        network = await self._get_network()
        sender_address = _get_sender_address(private_key)

        try:
            broadcaster = MsgBroadcasterWithPk.new_using_gas_heuristics(
                network=network,
                private_key=private_key,
            )

            composer = Composer(network=network.string())
            execute_msg = composer.msg_execute_contract(
                sender=sender_address,
                contract=self.contract_address,
                msg=json.dumps(msg),
            )

            result = await broadcaster.broadcast([execute_msg])

            tx_response = None
            if isinstance(result, dict):
                tx_response = result.get("txResponse", result)
            elif hasattr(result, "txResponse"):
                tx_response = result.txResponse
            else:
                tx_response = result

            tx_hash = None
            if isinstance(tx_response, dict):
                tx_hash = tx_response.get("txhash") or tx_response.get("txHash")
            else:
                tx_hash = getattr(tx_response, "txhash", None) or getattr(tx_response, "txHash", None)

            if not tx_hash:
                raise ContractExecutionError(
                    f"Failed to get transaction hash from broadcast result: {result}"
                )

            tx_result = await self._wait_for_tx_result(tx_hash, timeout=30.0)
            code = tx_result.get("code", 0)
            raw_log = tx_result.get("rawLog", "")

            if code and code != 0:
                error_msg = raw_log or f"Transaction failed with code {code}"
                error_lower = error_msg.lower()
                if "unauthorized" in error_lower:
                    raise ContractUnauthorizedError(error_msg)
                raise ContractExecutionError(error_msg)

            logger.info("Contract tx SUCCESS: %s", tx_hash)
            return tx_hash

        except (ContractUnauthorizedError, ContractValidationError, ContractExecutionError):
            raise
        except Exception as e:
            error_msg = str(e).lower()
            if "unauthorized" in error_msg:
                raise ContractUnauthorizedError(str(e)) from e
            raise ContractExecutionError(str(e)) from e
