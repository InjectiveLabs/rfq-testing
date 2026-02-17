"""Price fetching utilities for multi-market testing.

Supports:
- Static prices from config (for local testing)
- Oracle prices from Injective chain (for testnet)
"""

import asyncio
import logging
from decimal import Decimal
from typing import Optional

import httpx

from rfq_test.models.config import MarketConfig, EnvironmentConfig

logger = logging.getLogger(__name__)


class PriceFetcher:
    """Fetches market prices from various sources."""
    
    def __init__(self, config: EnvironmentConfig):
        self.config = config
        self._cache: dict[str, Decimal] = {}
        self._cache_ttl_seconds: float = 60.0
        self._last_fetch: dict[str, float] = {}
    
    async def get_price(self, market: MarketConfig) -> Decimal:
        """Get current price for a market.
        
        Args:
            market: Market configuration
            
        Returns:
            Current price
        """
        if market.price_source == "static":
            return self._get_static_price(market)
        elif market.price_source == "oracle":
            return await self._get_oracle_price(market)
        else:
            raise ValueError(f"Unknown price source: {market.price_source}")
    
    async def get_all_prices(self) -> dict[str, Decimal]:
        """Get prices for all configured markets.
        
        Returns:
            Dict mapping market symbol to price
        """
        prices = {}
        for market in self.config.markets:
            try:
                price = await self.get_price(market)
                prices[market.symbol] = price
            except Exception as e:
                logger.warning(f"Failed to get price for {market.symbol}: {e}")
        return prices
    
    def _get_static_price(self, market: MarketConfig) -> Decimal:
        """Get price from static config."""
        if market.price is None:
            raise ValueError(f"No static price configured for {market.symbol}")
        return market.price
    
    async def _get_oracle_price(self, market: MarketConfig) -> Decimal:
        """Get price from Injective oracle.
        
        For testnet, we query the chain's oracle module.
        """
        # Check cache
        import time
        now = time.time()
        cache_key = market.id
        
        if cache_key in self._cache:
            last_fetch = self._last_fetch.get(cache_key, 0)
            if now - last_fetch < self._cache_ttl_seconds:
                return self._cache[cache_key]
        
        # Fetch from oracle
        price = await self._fetch_oracle_price(market)
        
        # Update cache
        self._cache[cache_key] = price
        self._last_fetch[cache_key] = now
        
        return price
    
    async def _fetch_oracle_price(self, market: MarketConfig) -> Decimal:
        """Fetch price from Injective LCD.
        
        Tries in order:
        1. Exchange v2 derivative market (mark_price, human-readable) – preferred.
        2. Exchange v1beta1 derivative market (markPrice/oraclePrice, may be scaled).
        3. Oracle price by base/quote.
        Uses market.id so any configured market_id is supported.
        """
        lcd_url = self.config.chain.lcd_endpoint.rstrip("/")
        
        try:
            async with httpx.AsyncClient() as client:
                # 1) Prefer v2 derivative market endpoint (returns mark_price at top level)
                response = await client.get(
                    f"{lcd_url}/injective/exchange/v2/derivative/markets/{market.id}",
                    timeout=10.0,
                )
                if response.status_code == 200:
                    data = response.json()
                    # v2: top-level mark_price is human-readable; some proxies nest under "market"
                    mark_price = data.get("mark_price") or (data.get("market") or {}).get("mark_price")
                    if mark_price is not None:
                        return Decimal(str(mark_price))
                    # v2 inner market object (camelCase from proto)
                    market_data = data.get("market", {}) or {}
                    inner = market_data.get("market", market_data)
                    mark_price = inner.get("markPrice") or inner.get("mark_price") or inner.get("oraclePrice")
                    if mark_price is not None:
                        return Decimal(str(mark_price))
                
                # 2) Fallback: v1beta1 derivative market
                response = await client.get(
                    f"{lcd_url}/injective/exchange/v1beta1/derivative/markets/{market.id}",
                    timeout=10.0,
                )
                if response.status_code == 200:
                    data = response.json()
                    market_data = data.get("market", {}).get("market", {})
                    mark_price = market_data.get("markPrice") or market_data.get("oraclePrice")
                    if mark_price is not None:
                        return Decimal(str(mark_price)) / Decimal("1e18")
                
                # 3) Fallback: oracle price by base/quote
                response = await client.get(
                    f"{lcd_url}/injective/oracle/v1beta1/price",
                    params={"base": market.base, "quote": market.quote},
                    timeout=10.0,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    price_str = data.get("price", {}).get("price")
                    if price_str:
                        return Decimal(price_str)
        
        except Exception as e:
            logger.error(f"Failed to fetch oracle price for {market.symbol}: {e}")
        
        # Final fallback: use static price if available
        if market.price:
            logger.warning(f"Using static fallback price for {market.symbol}")
            return market.price
        
        raise ValueError(f"Could not fetch price for {market.symbol}")


def calculate_test_parameters(
    market: MarketConfig,
    price: Decimal,
    position_size_usd: Decimal = Decimal("1000"),
    leverage: Decimal = Decimal("5"),
) -> dict:
    """Calculate test parameters based on market price.
    
    Args:
        market: Market configuration
        price: Current market price
        position_size_usd: Desired position size in USD
        leverage: Leverage to use
        
    Returns:
        Dict with margin, quantity, and other parameters
    """
    # Calculate quantity (position size / price)
    quantity = position_size_usd / price
    
    # Round to market's minimum quantity
    min_qty = market.min_quantity
    quantity = (quantity // min_qty) * min_qty
    quantity = max(quantity, min_qty)
    
    # Calculate margin (position size / leverage)
    margin = position_size_usd / leverage
    
    return {
        "quantity": str(quantity),
        "margin": str(margin),
        "position_value": str(position_size_usd),
        "price": str(price),
        "leverage": str(leverage),
    }


class MultiMarketTestHelper:
    """Helper for running tests across multiple markets."""
    
    def __init__(self, config: EnvironmentConfig):
        self.config = config
        self.price_fetcher = PriceFetcher(config)
        self._prices: dict[str, Decimal] = {}
    
    async def initialize(self):
        """Initialize by fetching all market prices."""
        self._prices = await self.price_fetcher.get_all_prices()
        logger.info(f"Initialized prices for {len(self._prices)} markets")
        for symbol, price in self._prices.items():
            logger.info(f"  {symbol}: {price}")
    
    def get_markets(self) -> list[MarketConfig]:
        """Get all configured markets."""
        return self.config.markets
    
    def get_price(self, symbol: str) -> Decimal:
        """Get cached price for a market."""
        if symbol not in self._prices:
            raise ValueError(f"Price not loaded for {symbol}")
        return self._prices[symbol]
    
    def get_test_params(
        self,
        symbol: str,
        position_size_usd: Decimal = Decimal("1000"),
    ) -> dict:
        """Get test parameters for a market."""
        market = self.config.get_market(symbol)
        price = self.get_price(symbol)
        return calculate_test_parameters(market, price, position_size_usd)
    
    def get_all_test_params(
        self,
        position_size_usd: Decimal = Decimal("1000"),
    ) -> dict[str, dict]:
        """Get test parameters for all markets."""
        return {
            market.symbol: self.get_test_params(market.symbol, position_size_usd)
            for market in self.config.markets
            if market.symbol in self._prices
        }
