"""Factory for generating RFQ request test data."""

import time
from decimal import Decimal
from typing import Optional, Union

from rfq_test.models.config import MarketConfig
from rfq_test.models.types import Direction


class RequestFactory:
    """Factory for creating RFQ request test data.
    
    Supports creating valid requests and intentionally invalid
    requests for validation testing.
    """
    
    def __init__(self, default_market: Optional[MarketConfig] = None):
        self.default_market = default_market
    
    def create(
        self,
        taker: str,
        market: Optional[MarketConfig] = None,
        direction: Direction = Direction.LONG,
        margin: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
        rfq_id: Optional[str] = None,
        **overrides,
    ) -> dict:
        """Create a valid request.
        
        Args:
            taker: Taker's Injective address
            market: Market configuration (uses default if None)
            direction: Trade direction
            margin: Margin amount (derives from market if None)
            quantity: Quantity (derives from market if None)
            rfq_id: Request ID (generates if None)
            **overrides: Override any field
            
        Returns:
            Request data dict
        """
        market = market or self.default_market
        if not market:
            raise ValueError("No market provided and no default set")
        
        request = {
            "rfq_id": rfq_id or str(int(time.time() * 1000)),
            "taker": taker,
            "market_id": market.id,
            "direction": direction.value,
            "margin": str(margin or market.typical_margin),
            "quantity": str(quantity or market.typical_quantity),
        }
        
        # Apply overrides
        request.update(overrides)
        return request

    def create_indexer_request(
        self,
        taker_address: str,
        market: Optional[MarketConfig] = None,
        direction: Union[Direction, str] = Direction.LONG,
        margin: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
        worst_price: Optional[Decimal] = None,
        expiry_ms: Optional[int] = None,
        client_id: Optional[str] = None,
        **overrides,
    ) -> dict:
        """Create a request in the shape expected by the indexer (TakerStream).

        Uses request_address (not taker). The backend assigns rfq_id, so this
        factory emits client_id plus RFQExpiryType-style expiry instead.
        Prefer this for indexer/contract tests and actors.
        """
        market = market or self.default_market
        if not market:
            raise ValueError("No market provided and no default set")
        _dir = direction.value if isinstance(direction, Direction) else direction
        # Indexer API requires lowercase "long" or "short"
        if _dir in (0, "0"):
            _dir = "long"
        elif _dir in (1, "1"):
            _dir = "short"
        elif isinstance(_dir, str):
            _dir = _dir.lower()
        _client_id = client_id or str(int(time.time() * 1000))
        if expiry_ms is None:
            expiry_ms = int(time.time() * 1000) + 300_000  # 5 min default
        request = {
            "request_address": taker_address,
            "client_id": _client_id,
            "market_id": market.id,
            "direction": _dir,
            "margin": str(margin or market.typical_margin),
            "quantity": str(quantity or market.typical_quantity),
            "worst_price": str(worst_price or (market.price or Decimal("100"))),
            "expiry": {"ts": expiry_ms},
        }
        request.update(overrides)
        return request

    def create_invalid_missing_field(
        self,
        taker: str,
        field: str,
        market: Optional[MarketConfig] = None,
    ) -> dict:
        """Create request with a missing required field.
        
        Args:
            taker: Taker's Injective address
            field: Field to omit
            market: Market configuration
            
        Returns:
            Request data dict without the specified field
        """
        request = self.create(taker=taker, market=market)
        request.pop(field, None)
        return request
    
    def create_invalid_margin(
        self,
        taker: str,
        margin_value: str,
        market: Optional[MarketConfig] = None,
    ) -> dict:
        """Create request with invalid margin.
        
        Args:
            taker: Taker's Injective address
            margin_value: Invalid margin value (e.g., "-100", "0", "abc")
            market: Market configuration
            
        Returns:
            Request data dict with invalid margin
        """
        return self.create(taker=taker, market=market, margin=margin_value)
    
    def create_invalid_quantity(
        self,
        taker: str,
        quantity_value: str,
        market: Optional[MarketConfig] = None,
    ) -> dict:
        """Create request with invalid quantity.
        
        Args:
            taker: Taker's Injective address
            quantity_value: Invalid quantity value
            market: Market configuration
            
        Returns:
            Request data dict with invalid quantity
        """
        return self.create(taker=taker, market=market, quantity=quantity_value)
    
    def create_invalid_direction(
        self,
        taker: str,
        direction_value: str,
        market: Optional[MarketConfig] = None,
    ) -> dict:
        """Create request with invalid direction.
        
        Args:
            taker: Taker's Injective address
            direction_value: Invalid direction (e.g., "INVALID", "long")
            market: Market configuration
            
        Returns:
            Request data dict with invalid direction
        """
        request = self.create(taker=taker, market=market)
        request["direction"] = direction_value
        return request
    
    def create_invalid_market_id(
        self,
        taker: str,
        market_id: str,
        market: Optional[MarketConfig] = None,
    ) -> dict:
        """Create request with invalid market ID.
        
        Args:
            taker: Taker's Injective address
            market_id: Invalid market ID
            market: Market configuration (for other fields)
            
        Returns:
            Request data dict with invalid market_id
        """
        request = self.create(taker=taker, market=market)
        request["market_id"] = market_id
        return request
