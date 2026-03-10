"""Retail user actor."""

import logging
import time
from decimal import Decimal
from typing import Optional

from rfq_test.clients.websocket import TakerStreamClient
from rfq_test.clients.contract import ContractClient
from rfq_test.crypto.wallet import Wallet
from rfq_test.factories.request import RequestFactory
from rfq_test.models.config import ContractConfig, ChainConfig, MarketConfig
from rfq_test.models.types import Direction

logger = logging.getLogger(__name__)


class RetailUser:
    """Retail user actor for requesting quotes and accepting trades.
    
    A retail user:
    - Creates RFQ requests via WebSocket (TakerStream)
    - Receives quotes automatically on the same stream
    - Accepts quotes by submitting to smart contract
    """
    
    def __init__(
        self,
        wallet: Wallet,
        ws_url: str,
        contract_config: ContractConfig,
        chain_config: ChainConfig,
    ):
        self.wallet = wallet
        self.ws_url = ws_url
        self.contract_client = ContractClient(contract_config, chain_config)
        self._ws_client: Optional[TakerStreamClient] = None
    
    @property
    def address(self) -> str:
        """Get retail user's Injective address."""
        return self.wallet.inj_address
    
    async def connect(self) -> None:
        """Connect to WebSocket TakerStream (sends request_address as metadata)."""
        self._ws_client = TakerStreamClient(
            self.ws_url,
            request_address=self.address,
        )
        await self._ws_client.connect()
    
    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        if self._ws_client:
            await self._ws_client.close()
            self._ws_client = None
    
    async def __aenter__(self) -> "RetailUser":
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.disconnect()
    
    def generate_rfq_id(self) -> int:
        """Generate unique RFQ ID (timestamp-based nonce)."""
        return int(time.time() * 1000)
    
    async def create_request(
        self,
        market: MarketConfig,
        direction: Direction,
        margin: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
        rfq_id: Optional[int] = None,
        worst_price: Optional[Decimal] = None,
    ) -> dict:
        """Create an RFQ request.
        
        Args:
            market: Market configuration
            direction: Trade direction
            margin: Margin amount (uses market default if None)
            quantity: Quantity (uses market default if None)
            rfq_id: Optional custom RFQ ID
            worst_price: Worst acceptable price
            
        Returns:
            Request data that was sent
        """
        if not self._ws_client:
            raise RuntimeError("Not connected")

        factory = RequestFactory(default_market=market)
        request_data = factory.create_indexer_request(
            taker_address=self.address,
            market=market,
            direction=direction,
            margin=margin,
            quantity=quantity,
            worst_price=worst_price,
            rfq_id=rfq_id or self.generate_rfq_id(),
        )

        logger.info(f"Creating request: RFQ#{request_data['rfq_id']}")
        await self._ws_client.send_request(request_data)
        
        return request_data
    
    async def wait_for_quotes(
        self,
        rfq_id: int,
        timeout: float = 10.0,
        min_quotes: int = 1,
    ) -> list[dict]:
        """Wait for quotes for a request.
        
        Args:
            rfq_id: Request ID to wait for
            timeout: Maximum wait time
            min_quotes: Minimum quotes to collect before returning
            
        Returns:
            List of received quotes
        """
        if not self._ws_client:
            raise RuntimeError("Not connected")
        
        return await self._ws_client.collect_quotes(
            rfq_id=rfq_id,
            timeout=timeout,
            min_quotes=min_quotes,
        )
    
    def select_best_quote(
        self,
        quotes: list[dict],
        direction: Direction,
    ) -> Optional[dict]:
        """Select the best quote based on price.
        
        Args:
            quotes: List of quotes
            direction: Trade direction (determines "best" price)
            
        Returns:
            Best quote or None if no quotes
        """
        if not quotes:
            return None
        
        if direction == Direction.LONG:
            # For long, lower price is better
            return min(quotes, key=lambda q: Decimal(q["price"]))
        else:
            # For short, higher price is better
            return max(quotes, key=lambda q: Decimal(q["price"]))
    
    async def accept_quote(
        self,
        quote: dict,
        rfq_id: int,
        market_id: str,
        direction: Direction,
        margin: Decimal,
        quantity: Decimal,
        worst_price: Optional[Decimal] = None,
        unfilled_action: Optional[dict] = None,
        cid: Optional[str] = None,
    ) -> str:
        """Accept a quote and settle the trade on-chain.
        
        For partial fill (quote quantity < request quantity), pass unfilled_action so the
        contract places the unfilled portion on the orderbook. Example:
        - unfilled_action={"limit": {"price": "4.5"}} for limit order at 4.5
        - unfilled_action={"market": {}} for market (IOC) at worst_price
        
        Args:
            quote: Quote to accept (may be partial: quote["quantity"] < quantity)
            rfq_id: Request ID
            market_id: Market ID
            direction: Trade direction
            margin: Taker margin (full request amount)
            quantity: Taker quantity (full request amount)
            worst_price: Max price (long) or min price (short). Required by contract; default from quote price.
            unfilled_action: When quote fills only part of quantity, contract posts remainder to orderbook:
                {"limit": {"price": "X"}} or {"market": {}}
            cid: Optional settlement CID for any orderbook execution created by AcceptQuote
            
        Returns:
            Transaction hash
        """
        contract_quote = {
            "maker": quote["maker"],
            "margin": quote["margin"],
            "quantity": quote["quantity"],
            "price": quote["price"],
            "expiry": quote["expiry"],
            "signature": quote["signature"],
        }
        
        logger.info(f"Accepting quote from {quote['maker']} (qty {quote['quantity']} of {quantity})")
        return await self.contract_client.accept_quote(
            private_key=self.wallet.private_key,
            quotes=[contract_quote],
            rfq_id=str(rfq_id),
            market_id=market_id,
            direction=direction,
            margin=margin,
            quantity=quantity,
            worst_price=worst_price,
            unfilled_action=unfilled_action,
            cid=cid,
        )
    
    async def settle_via_orderbook(
        self,
        rfq_id: int,
        market_id: str,
        direction: Direction,
        margin: Decimal,
        quantity: Decimal,
        worst_price: Decimal,
        unfilled_action: Optional[dict] = None,
        cid: Optional[str] = None,
    ) -> str:
        """Settle trade via orderbook (no MM quotes).
        
        Contract requires unfilled_action when quotes are empty (orderbook-only path).
        Defaults to {"market": {}} (IOC at worst_price) if not provided.
        
        Args:
            rfq_id: Request ID
            market_id: Market ID
            direction: Trade direction
            margin: Margin amount
            quantity: Trade quantity
            worst_price: Max (long) or min (short) price for the orderbook order
            unfilled_action: Required when quotes=[]; use {"market": {}} or {"limit": {"price": "X"}}
            cid: Optional settlement CID for the fallback orderbook execution
            
        Returns:
            Transaction hash
        """
        if unfilled_action is None:
            unfilled_action = {"market": {}}
        logger.info(f"Settling via orderbook: RFQ#{rfq_id}")
        return await self.contract_client.accept_quote(
            private_key=self.wallet.private_key,
            quotes=[],
            rfq_id=str(rfq_id),
            market_id=market_id,
            direction=direction,
            margin=margin,
            quantity=quantity,
            worst_price=worst_price,
            unfilled_action=unfilled_action,
            cid=cid,
        )
