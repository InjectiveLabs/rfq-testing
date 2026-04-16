"""Price fetching and tick-size quantization utilities.

Supports:
- Static prices from config (for local testing)
- Oracle prices from Injective chain (for testnet)
- Price quantization to market tick sizes (required before signing)

CRITICAL — Tick Size Rule
--------------------------
Every price and quantity you use in a quote or conditional order MUST be
quantized to the market's tick sizes before you sign it. The signed value
and the value sent to the contract/indexer must be byte-for-byte identical,
so quantize first, then sign, then send.

  Wrong order:   compute price → sign → quantize → send   (sig mismatch!)
  Correct order: compute price → quantize → sign → send

If you skip this, the exchange will reject the synthetic trade with an error
like "invalid price tick" even though the RFQ contract accepted the signature.

See quantize_to_tick() and quantize_for_fpdecimal() below.
"""

import logging
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING
from typing import Optional, Union

import httpx

from rfq_test.models.config import MarketConfig, EnvironmentConfig

logger = logging.getLogger(__name__)

# The RFQ contract uses FPDecimal which rejects values with more than 18
# fractional digits. Oracle mark prices can have many digits; always quantize
# computed prices before signing.
_FPDECIMAL_MAX_FRAC_DIGITS = 18
_FPDECIMAL_QUANTIZER = Decimal(10) ** -_FPDECIMAL_MAX_FRAC_DIGITS


def quantize_for_fpdecimal(value: Union[Decimal, str, int, float]) -> str:
    """Quantize a value to at most 18 fractional digits for FPDecimal compatibility.

    The RFQ contract uses FPDecimal which rejects values with more than 18
    fractional digits. Oracle mark prices and computed spread prices can have
    many digits; this ensures they are valid before signing/sending.

    Use quantize_to_tick() instead when you know the market's min_price_tick_size,
    as that is the stricter (and more correct) constraint.

    Args:
        value: Price, worst_price, quantity, margin, etc.

    Returns:
        String suitable for the signed payload and contract messages.
    """
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    quantized = d.quantize(_FPDECIMAL_QUANTIZER, rounding=ROUND_HALF_UP)
    normalized = quantized.normalize()
    return format(normalized, "f")


def quantize_to_tick(
    value: Union[Decimal, str, int, float],
    tick_size: Optional[Decimal] = None,
    rounding: str = ROUND_HALF_UP,
) -> str:
    """Quantize a price to the market's min_price_tick_size.

    The Injective exchange rejects synthetic trade prices that are not exact
    multiples of the market's min_price_tick_size. This function must be
    called on every price BEFORE signing the quote — the signed price and
    the price in AcceptQuote must be identical.

    Args:
        value: Price to quantize.
        tick_size: Market's min_price_tick_size (e.g. Decimal("0.001")).
            Obtain this from get_market_tick_sizes() or your config YAML.
            If None or zero, falls back to quantize_for_fpdecimal (18 digits).
        rounding: Decimal rounding mode.
            For MM quote prices:
              - LONG direction (taker buys): use ROUND_FLOOR so MM's price ≤ taker's worst_price
              - SHORT direction (taker sells): use ROUND_CEILING so MM's price ≥ taker's worst_price
            For conditional order worst_price (taker perspective):
              - SHORT direction: use ROUND_FLOOR (lower floor → MM has more room → more fills)
              - LONG direction: use ROUND_CEILING (higher cap → MM has more room → more fills)
            Default ROUND_HALF_UP is safe when using a generous band (e.g. ±5% from mark).

    Returns:
        String aligned to the market's price tick, ready for signing.

    Example:
        >>> quantize_to_tick("4.16789", Decimal("0.001"))
        "4.168"
        >>> # For a short worst_price (conservative — round down):
        >>> from decimal import ROUND_FLOOR
        >>> quantize_to_tick("4.16789", Decimal("0.001"), rounding=ROUND_FLOOR)
        "4.167"
    """
    d = Decimal(str(value)) if not isinstance(value, Decimal) else value
    if tick_size and tick_size > 0:
        quantized = (d / tick_size).to_integral_value(rounding=rounding) * tick_size
        return format(quantized.normalize(), "f")
    return quantize_for_fpdecimal(d)


def quantize_quantity(
    value: Union[Decimal, str, int, float],
    min_qty_tick: Optional[Decimal] = None,
) -> str:
    """Quantize a quantity to the market's min_quantity_tick_size.

    Quantities that are not multiples of min_quantity_tick_size are rejected
    by the exchange. Always floor-round quantities (never round up — that
    would exceed the taker's requested amount).

    Args:
        value: Quantity to quantize.
        min_qty_tick: Market's min_quantity_tick_size.
            If None, falls back to quantize_for_fpdecimal.

    Returns:
        String aligned to the market's quantity tick.
    """
    return quantize_to_tick(value, min_qty_tick, rounding=ROUND_FLOOR)


async def get_market_tick_sizes(
    lcd_endpoint: str,
    market_id: str,
) -> dict:
    """Fetch min_price_tick_size and min_quantity_tick_size for a market from the chain.

    Args:
        lcd_endpoint: Chain LCD URL (e.g. "https://testnet.sentry.lcd.injective.network")
        market_id: Derivative market ID (0x hex string)

    Returns:
        Dict with keys "min_price_tick" and "min_qty_tick" (Decimal values),
        or an empty dict if the fetch fails.

    Example:
        ticks = await get_market_tick_sizes(lcd_url, market_id)
        price_tick = ticks.get("min_price_tick")
        qty_tick   = ticks.get("min_qty_tick")
        quote_price = quantize_to_tick(raw_price, price_tick)
        quantity    = quantize_quantity(raw_qty, qty_tick)
        # Now sign quote_price and quantity — they are tick-aligned
    """
    url = f"{lcd_endpoint.rstrip('/')}/injective/exchange/v2/derivative/markets/{market_id}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                inner = (data.get("market") or {}).get("market") or {}
                result = {}
                tick_str = inner.get("min_price_tick_size")
                qty_str = inner.get("min_quantity_tick_size")
                if tick_str:
                    result["min_price_tick"] = Decimal(tick_str)
                if qty_str:
                    result["min_qty_tick"] = Decimal(qty_str)
                return result
    except Exception as e:
        logger.warning("Failed to fetch tick sizes for %s: %s", market_id[:20], e)
    return {}


class PriceFetcher:
    """Fetches market prices and tick sizes from the chain or static config.

    After the first oracle price fetch for a market, tick sizes are cached for
    the session lifetime (they don't change during a run).
    """

    def __init__(self, config: EnvironmentConfig):
        self.config = config
        self._cache: dict[str, Decimal] = {}
        self._cache_ttl_seconds: float = 60.0
        self._last_fetch: dict[str, float] = {}
        self._market_info: dict[str, dict] = {}  # session cache for tick sizes
    
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
    
    def get_price_tick(self, market: MarketConfig) -> Optional[Decimal]:
        """Return the cached min_price_tick_size for a market.

        Priority: session cache (from chain) → YAML config → None.
        Call get_price() at least once before this so the chain value is cached.
        Returns None when unknown so callers can fall back to quantize_for_fpdecimal.
        """
        cached = self._market_info.get(market.id, {})
        if "min_price_tick" in cached:
            return cached["min_price_tick"]
        if market.min_price_tick is not None:
            return market.min_price_tick
        return None

    def get_qty_tick(self, market: MarketConfig) -> Optional[Decimal]:
        """Return the cached min_quantity_tick_size for a market.

        Same priority as get_price_tick. Returns None when unknown.
        """
        return self._market_info.get(market.id, {}).get("min_qty_tick")

    async def _fetch_oracle_price(self, market: MarketConfig) -> Decimal:
        """Fetch price from Injective LCD, caching tick sizes as a side-effect.

        Tries in order:
        1. Exchange v2 derivative market (mark_price, human-readable) – preferred.
           Also caches min_price_tick_size and min_quantity_tick_size.
        2. Exchange v1beta1 derivative market (markPrice/oraclePrice, may be scaled).
        3. Oracle price by base/quote.
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
                    # Cache tick sizes from this response (session lifetime)
                    inner = (data.get("market") or {}).get("market") or {}
                    tick_info = {}
                    if inner.get("min_price_tick_size"):
                        tick_info["min_price_tick"] = Decimal(inner["min_price_tick_size"])
                    if inner.get("min_quantity_tick_size"):
                        tick_info["min_qty_tick"] = Decimal(inner["min_quantity_tick_size"])
                    if tick_info:
                        self._market_info[market.id] = tick_info

                    # v2: top-level mark_price is human-readable; some proxies nest under "market"
                    mark_price = data.get("mark_price") or (data.get("market") or {}).get("mark_price")
                    if mark_price is not None:
                        return Decimal(str(mark_price))
                    # v2 inner market object (camelCase from proto)
                    market_data = data.get("market", {}) or {}
                    inner_market = market_data.get("market", market_data)
                    mark_price = inner_market.get("markPrice") or inner_market.get("mark_price") or inner_market.get("oraclePrice")
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
