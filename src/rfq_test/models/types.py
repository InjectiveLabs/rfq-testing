"""Core data types for RFQ system."""

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Direction(str, Enum):
    """Trade direction."""
    LONG = "Long"
    SHORT = "Short"
    
    def to_indexer_value(self) -> int:
        """Convert to indexer/contract integer value (0=Long, 1=Short)."""
        return 0 if self == Direction.LONG else 1


class Request(BaseModel):
    """RFQ Request from retail user."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    
    rfq_id: str = Field(..., description="Indexer-assigned RFQ request ID")
    taker: str = Field(..., description="Retail user's Injective address")
    market_id: str = Field(..., description="Perpetual market ID")
    direction: Direction
    margin: Decimal = Field(..., gt=0, description="Collateral amount")
    quantity: Decimal = Field(..., gt=0, description="Trade quantity")
    

class Quote(BaseModel):
    """Quote from Market Maker."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    
    rfq_id: str = Field(..., description="Reference to the request")
    maker: str = Field(..., description="Market Maker's Injective address")
    taker: str = Field(..., description="Retail user's Injective address")
    market_id: str = Field(..., description="Perpetual market ID")
    direction: Direction
    margin: Decimal = Field(..., gt=0)
    quantity: Decimal = Field(..., gt=0)
    price: Decimal = Field(..., gt=0, description="Quote price")
    expiry: int = Field(..., gt=0, description="Unix timestamp when quote expires")
    signature: str = Field(..., description="MM's signature of the quote")


class Settlement(BaseModel):
    """Settlement result from on-chain execution."""
    model_config = ConfigDict(extra="forbid")
    
    rfq_id: str
    taker: str
    maker: Optional[str] = None  # None if settled via orderbook
    market_id: str
    direction: Direction
    margin: Decimal
    quantity: Decimal
    price: Optional[Decimal] = None
    tx_hash: str
    block_height: int
    settled_via: str = Field(..., description="'mm' or 'orderbook'")


class TradeMetrics(BaseModel):
    """Metrics for a single trade execution."""
    model_config = ConfigDict(extra="forbid")
    
    # Overall
    total_latency_ms: float
    success: bool
    error: Optional[str] = None
    
    # Per-step breakdown (in milliseconds)
    ws_connect_ms: Optional[float] = None
    request_create_ms: Optional[float] = None
    quote_receive_ms: Optional[float] = None
    accept_quote_ms: Optional[float] = None
    tx_confirm_ms: Optional[float] = None
    settlement_event_ms: Optional[float] = None
