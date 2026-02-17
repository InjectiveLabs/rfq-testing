"""Configuration models."""

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChainConfig(BaseModel):
    """Chain connection configuration."""
    model_config = ConfigDict(extra="forbid")
    
    grpc_endpoint: str
    lcd_endpoint: str
    chain_id: str
    tx_timeout_seconds: int = 5
    # Optional gRPC endpoints (pyinjective). If unset, code uses grpc_endpoint for all to avoid "dns:///" noise.
    grpc_exchange_endpoint: Optional[str] = None
    grpc_explorer_endpoint: Optional[str] = None
    chain_stream_endpoint: Optional[str] = None


class IndexerConfig(BaseModel):
    """RFQ Indexer configuration."""
    model_config = ConfigDict(extra="forbid")
    
    ws_endpoint: str  # Base URL for WebSocket streams (TakerStream/MakerStream appended)
    http_endpoint: str
    grpc_web_endpoint: Optional[str] = None  # Optional gRPC-web endpoint


class ContractConfig(BaseModel):
    """RFQ Contract configuration."""
    model_config = ConfigDict(extra="forbid")
    
    address: str


class MarketConfig(BaseModel):
    """Market configuration."""
    model_config = ConfigDict(extra="forbid")
    
    id: str = Field(..., description="Market ID on chain")
    symbol: str = Field(..., description="Human-readable symbol (e.g., INJ/USDT)")
    base: str = Field(..., description="Base asset (e.g., INJ)")
    quote: str = Field(..., description="Quote asset (e.g., USDT)")
    price: Optional[Decimal] = Field(None, description="Static price (for local)")
    price_source: Literal["static", "oracle"] = "static"
    min_quantity: Decimal = Field(Decimal("1.0"), description="Minimum trade quantity")
    
    @property
    def typical_margin(self) -> Decimal:
        """Calculate typical margin based on price (~10 units worth)."""
        if self.price:
            return self.price * 10
        return Decimal("100")  # Default if price unknown
    
    @property
    def typical_quantity(self) -> Decimal:
        """Calculate typical quantity for tests."""
        return self.min_quantity * 10


class FaucetConfig(BaseModel):
    """Faucet configuration for testnet."""
    model_config = ConfigDict(extra="forbid")
    
    enabled: bool = False
    url: Optional[str] = None
    rate_limit_seconds: int = 60


class EnvironmentConfig(BaseModel):
    """Full environment configuration."""
    model_config = ConfigDict(extra="forbid")
    
    environment: Literal["local", "testnet"]
    chain: ChainConfig
    indexer: IndexerConfig
    contract: ContractConfig
    markets: list[MarketConfig]
    faucet: FaucetConfig = FaucetConfig()
    
    @property
    def default_market(self) -> MarketConfig:
        """Get the default (first) market."""
        if not self.markets:
            raise ValueError("No markets configured")
        return self.markets[0]
    
    @property
    def default_market_id(self) -> str:
        """Get the default market ID."""
        return self.default_market.id
    
    def get_market(self, symbol: str) -> MarketConfig:
        """Get market by symbol."""
        for market in self.markets:
            if market.symbol == symbol:
                return market
        raise ValueError(f"Market not found: {symbol}")
    
    def get_market_by_id(self, market_id: str) -> MarketConfig:
        """Get market by ID."""
        for market in self.markets:
            if market.id == market_id:
                return market
        raise ValueError(f"Market not found: {market_id}")

    @property
    def signing_context(self) -> tuple[str, str]:
        """Single source for chain_id and contract address (signing and indexer quote).
        
        Use this whenever building a quote signature or indexer quote payload
        so both contract verification and indexer validation see the same values.
        """
        return (self.chain.chain_id, self.contract.address)
