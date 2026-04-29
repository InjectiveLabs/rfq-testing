"""Market Maker actor."""

import asyncio
import logging
import time
from decimal import Decimal
from typing import Callable, Optional

from rfq_test.clients.chain import ChainClient
from rfq_test.clients.websocket import MakerStreamClient
from rfq_test.crypto.eip712 import sign_quote_v2
from rfq_test.crypto.wallet import Wallet
from rfq_test.models.config import MarketConfig
from rfq_test.models.types import Direction

logger = logging.getLogger(__name__)


class MarketMaker:
    """Market Maker actor for providing quotes.
    
    A market maker:
    - Connects to WebSocket MakerStream
    - Receives RFQ requests automatically
    - Builds, signs, and sends quotes
    """
    
    def __init__(
        self,
        wallet: Wallet,
        ws_url: str,
        price_spread_bps: int = 50,  # 0.5% spread
        quote_validity_seconds: int = 20,
        chain_id: Optional[str] = None,
        contract_address: Optional[str] = None,
        evm_chain_id: Optional[int] = None,
        subscribe_to_quotes_updates: bool = False,
        subscribe_to_settlement_updates: bool = False,
    ):
        self.wallet = wallet
        self.ws_url = ws_url
        self.price_spread_bps = price_spread_bps
        self.quote_validity_seconds = quote_validity_seconds
        self.chain_id = chain_id
        self.contract_address = contract_address
        self.evm_chain_id = evm_chain_id
        self.subscribe_to_quotes_updates = subscribe_to_quotes_updates
        self.subscribe_to_settlement_updates = subscribe_to_settlement_updates
        self._ws_client: Optional[MakerStreamClient] = None
    
    @property
    def address(self) -> str:
        """Get MM's Injective address."""
        return self.wallet.inj_address
    
    async def connect(self) -> None:
        """Connect to WebSocket MakerStream."""
        self._ws_client = MakerStreamClient(
            self.ws_url,
            maker_address=self.address,
            subscribe_to_quotes_updates=self.subscribe_to_quotes_updates,
            subscribe_to_settlement_updates=self.subscribe_to_settlement_updates,
        )
        await self._ws_client.connect()
    
    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        if self._ws_client:
            await self._ws_client.close()
            self._ws_client = None
    
    async def __aenter__(self) -> "MarketMaker":
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()
    
    def calculate_quote_price(
        self,
        market_price: Decimal,
        direction: Direction,
    ) -> Decimal:
        """Calculate quote price with spread.
        
        Args:
            market_price: Current market price
            direction: Taker's direction
            
        Returns:
            Quote price (worse for taker = profit for MM)
        """
        spread = market_price * Decimal(self.price_spread_bps) / Decimal(10000)
        
        if direction == Direction.LONG:
            # Taker wants to go long, MM sells higher
            return market_price + spread
        else:
            # Taker wants to go short, MM buys lower
            return market_price - spread
    
    async def wait_for_request(self, timeout: float = 30.0) -> dict:
        """Wait for an RFQ request.
        
        Args:
            timeout: Maximum wait time
            
        Returns:
            Request data
        """
        if not self._ws_client:
            raise RuntimeError("Not connected")
        
        return await self._ws_client.wait_for_request(timeout=timeout)
    
    async def build_and_send_quote(
        self,
        request: dict,
        market: MarketConfig,
        price: Optional[Decimal] = None,
        quantity_override: Optional[Decimal] = None,
        margin_override: Optional[Decimal] = None,
        maker_subaccount_nonce: int = 0,
        min_fill_quantity: Optional[str] = None,
    ) -> dict:
        """Build, sign, and send a quote for a request.
        
        Args:
            request: The RFQ request data
            market: Market configuration
            price: Optional price override (otherwise calculated from market)
            quantity_override: Optional quote quantity (for partial fill). If set, MM quotes
                this amount instead of full request quantity; retail must pass unfilled_action
                to send the remainder to the orderbook.
            margin_override: Optional quote margin. If None and quantity_override is set,
                margin is scaled proportionally (maker_margin = taker_margin * quote_qty / taker_qty).
            maker_subaccount_nonce: Optional maker subaccount nonce to include in the
                quote payload and signature.
            min_fill_quantity: Optional minimum fill quantity to include in the quote
                payload and signature.
            
        Returns:
            Quote data that was sent
        """
        if not self._ws_client:
            raise RuntimeError("Not connected")
        
        # Extract request data (indexer may send direction as 0/1, "long"/"short", or "Long"/"Short")
        rfq_id = request["rfq_id"]
        market_id = request["market_id"]
        taker = request.get("taker") or request.get("request_address", "")
        raw_direction = request["direction"]
        if raw_direction in (0, "0") or (isinstance(raw_direction, str) and raw_direction.lower() == "long"):
            direction = Direction.LONG
        elif raw_direction in (1, "1") or (isinstance(raw_direction, str) and raw_direction.lower() == "short"):
            direction = Direction.SHORT
        else:
            direction = Direction(raw_direction)
        taker_margin = Decimal(request["margin"])
        taker_quantity = Decimal(request["quantity"])
        
        # Calculate price if not provided
        if price is None:
            market_price = market.price or Decimal("1.0")
            price = self.calculate_quote_price(market_price, direction)
        
        # Full fill vs partial fill: MM quotes up to quantity_override/margin_override if set
        if quantity_override is not None:
            maker_quantity = quantity_override
            maker_margin = margin_override if margin_override is not None else (
                taker_margin * maker_quantity / taker_quantity
            )
            logger.info(f"Partial quote: quantity={maker_quantity} (request {taker_quantity}), margin={maker_margin}")
        else:
            maker_margin = taker_margin
            maker_quantity = taker_quantity
        
        # Expiry timestamp (milliseconds)
        expiry = int(time.time() * 1000) + (self.quote_validity_seconds * 1000)
        
        # Sign the quote with EIP-712 v2. evm_chain_id + contract_address are
        # required: they bind the signature to a specific chain + contract via
        # the domain separator.
        if self.evm_chain_id is None or self.contract_address is None:
            raise RuntimeError(
                "MarketMaker requires evm_chain_id and contract_address for v2 signing"
            )
        signature = sign_quote_v2(
            private_key=self.wallet.private_key,
            evm_chain_id=self.evm_chain_id,
            verifying_contract_bech32=self.contract_address,
            market_id=market_id,
            rfq_id=int(rfq_id),
            taker=taker,
            direction=direction.value,
            taker_margin=str(taker_margin),
            taker_quantity=str(taker_quantity),
            maker=self.address,
            maker_margin=str(maker_margin),
            maker_quantity=str(maker_quantity),
            price=str(price),
            expiry_ms=expiry,
            maker_subaccount_nonce=maker_subaccount_nonce,
            min_fill_quantity=min_fill_quantity,
        )

        # Build quote payload (indexer expects chain_id, contract_address, and
        # sign_mode set per https://devnet.api.injective.dev/swagger/#/).
        quote_data = {
            "rfq_id": rfq_id,
            "market_id": market_id,
            "taker_direction": direction.to_indexer_value(),
            "margin": str(maker_margin),
            "quantity": str(maker_quantity),
            "price": str(price),
            "expiry": expiry,
            "maker": self.address,
            "taker": taker,
            "signature": signature,
            "sign_mode": "v2",
            "maker_subaccount_nonce": maker_subaccount_nonce,
            "contract_address": self.contract_address,
        }
        if min_fill_quantity is not None:
            quote_data["min_fill_quantity"] = min_fill_quantity
        if self.chain_id is not None:
            quote_data["chain_id"] = self.chain_id
        
        logger.info(f"Sending quote for RFQ#{rfq_id}: price={price}")
        await self._ws_client.send_quote(quote_data)
        
        return quote_data
    
    async def listen_and_quote(
        self,
        market: MarketConfig,
        price_fn: Optional[Callable[[dict], Decimal]] = None,
        max_quotes: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> list[dict]:
        """Listen for requests and automatically send quotes.
        
        Args:
            market: Market configuration
            price_fn: Optional function to calculate price from request
            max_quotes: Stop after N quotes (None = infinite)
            timeout: Stop after timeout seconds
            
        Returns:
            List of sent quotes
        """
        if not self._ws_client:
            raise RuntimeError("Not connected")
        
        quotes_sent = []
        start_time = time.monotonic()
        _timeout = timeout or 60.0
        
        async for request in self._ws_client.requests(timeout=_timeout):
            # Check timeout
            if timeout and (time.monotonic() - start_time) > timeout:
                break
            
            # Check max quotes
            if max_quotes and len(quotes_sent) >= max_quotes:
                break
            
            # Calculate price
            price = None
            if price_fn:
                price = price_fn(request)
            
            # Send quote
            try:
                quote = await self.build_and_send_quote(
                    request=request,
                    market=market,
                    price=price,
                )
                quotes_sent.append(quote)
            except Exception as e:
                logger.error(f"Failed to send quote: {e}")
        
        return quotes_sent
    
    async def grant_authz_to_contract(
        self,
        chain_client: ChainClient,
        contract_address: str,
        expire_in_seconds: int = 365 * 24 * 60 * 60,  # 1 year default
    ) -> list[str]:
        """Grant authorization to the RFQ contract.
        
        Grants permissions required for RFQ settlement:
        1. /cosmos.bank.v1beta1.MsgSend - for pulling funds from MM
        2. /injective.exchange.v2.MsgPrivilegedExecuteContract - for synthetic trades
        
        Args:
            chain_client: Chain client for broadcasting transactions
            contract_address: RFQ contract address (grantee)
            expire_in_seconds: Grant expiration time
            
        Returns:
            List of transaction hashes
        """
        logger.info(f"Granting authz to contract {contract_address} for MM {self.address}")
        
        tx_hashes = []
        
        # Grant 1: MsgSend
        logger.info("Granting MsgSend permission")
        tx_hash_1 = await chain_client.grant_authz(
            private_key=self.wallet.private_key,
            grantee=contract_address,
            msg_type="/cosmos.bank.v1beta1.MsgSend",
            expire_in_seconds=expire_in_seconds,
        )
        tx_hashes.append(tx_hash_1)
        logger.info(f"MsgSend grant confirmed: {tx_hash_1}")
        
        await asyncio.sleep(2)  # Wait for state propagation
        
        # Grant 2: MsgPrivilegedExecuteContract
        logger.info("Granting MsgPrivilegedExecuteContract permission")
        tx_hash_2 = await chain_client.grant_authz(
            private_key=self.wallet.private_key,
            grantee=contract_address,
            msg_type="/injective.exchange.v2.MsgPrivilegedExecuteContract",
            expire_in_seconds=expire_in_seconds,
        )
        tx_hashes.append(tx_hash_2)
        logger.info(f"MsgPrivilegedExecuteContract grant confirmed: {tx_hash_2}")
        
        await asyncio.sleep(2)  # Wait for state propagation
        
        return tx_hashes
