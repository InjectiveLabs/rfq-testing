"""Configurable mark-based Market Maker loop.

This example listens on MakerStream and quotes incoming RFQs for one configured
market:

- taker long  -> maker sells at mark + edge bps
- taker short -> maker buys at mark - edge bps

Use --fixed-price to quote one static price instead. The script keeps signing
and wire decimal strings identical, quantizes to market ticks, answers
MakerStream auth challenges, and prints quote/settlement updates.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

import dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

dotenv.load_dotenv(Path(__file__).with_name(".env"))
dotenv.load_dotenv(ROOT / ".env")

from rfq_test.clients.websocket import MakerStreamClient
from rfq_test.config import get_environment_config, get_settings
from rfq_test.crypto.eip712 import sign_quote_v2
from rfq_test.crypto.wallet import Wallet
from rfq_test.exceptions import IndexerTimeoutError, IndexerValidationError
from rfq_test.models.config import EnvironmentConfig, MarketConfig
from rfq_test.utils.price import (
    PriceFetcher,
    quantize_for_fpdecimal,
    quantize_quantity,
    quantize_to_tick,
)


def _direction(raw: str) -> str | None:
    value = str(raw).lower()
    if value in {"0", "long"}:
        return "long"
    if value in {"1", "short"}:
        return "short"
    return None


def _private_key_from_env() -> tuple[str, str]:
    settings = get_settings()
    value = os.getenv("MM_PRIVATE_KEY") or settings.mm_private_key
    if not value:
        active = settings.rfq_env.upper()
        raise SystemExit(f"Set MM_PRIVATE_KEY or {active}_MM_PRIVATE_KEY before running.")
    if os.getenv("MM_PRIVATE_KEY"):
        source = "MM_PRIVATE_KEY"
    else:
        source = f"{settings.rfq_env.upper()}_MM_PRIVATE_KEY"
    return value, source


def _select_market(
    config: EnvironmentConfig,
    market_id: str | None,
    symbol: str | None,
) -> MarketConfig:
    if market_id:
        return config.get_market_by_id(market_id)
    if symbol:
        return config.get_market(symbol)
    return config.default_market


def _quote_price(
    *,
    mark: Decimal,
    direction: str,
    edge_bps: Decimal,
    tick: Decimal | None,
    fixed_price: Decimal | None,
) -> str:
    rounding = ROUND_FLOOR if direction == "long" else ROUND_CEILING
    if fixed_price is not None:
        return quantize_to_tick(fixed_price, tick, rounding=rounding)

    edge = edge_bps / Decimal("10000")
    if direction == "long":
        raw_price = mark * (Decimal("1") + edge)
    else:
        raw_price = mark * (Decimal("1") - edge)
    return quantize_to_tick(raw_price, tick, rounding=rounding)


def _fit_price_to_worst(price: str, direction: str, worst_price: str, tick: Decimal | None) -> str:
    price_dec = Decimal(price)
    worst_dec = Decimal(worst_price)
    if direction == "long" and price_dec > worst_dec:
        return quantize_to_tick(worst_dec, tick, rounding=ROUND_FLOOR)
    if direction == "short" and price_dec < worst_dec:
        return quantize_to_tick(worst_dec, tick, rounding=ROUND_CEILING)
    return price


def _quote_size_and_margin(
    request: dict,
    max_quantity: Decimal,
    qty_tick: Decimal | None,
) -> tuple[str, str]:
    taker_qty = Decimal(request["quantity"])
    taker_margin = Decimal(request["margin"])

    maker_qty = min(taker_qty, max_quantity)
    maker_qty_str = quantize_quantity(maker_qty, qty_tick)
    maker_qty = Decimal(maker_qty_str)
    if maker_qty <= 0:
        raise ValueError("quote quantity rounded to zero")

    if maker_qty == taker_qty:
        maker_margin = taker_margin
    else:
        maker_margin = taker_margin * maker_qty / taker_qty

    return maker_qty_str, quantize_for_fpdecimal(maker_margin)


async def _send_quote(
    *,
    client: MakerStreamClient,
    wallet: Wallet,
    request: dict,
    chain_id: str,
    contract_address: str,
    evm_chain_id: int,
    price: str,
    quantity: str,
    margin: str,
    quote_validity_ms: int,
    maker_subaccount_nonce: int,
) -> dict | None:
    taker = request.get("taker") or request.get("request_address", "")
    expiry_ms = int(time.time() * 1000) + quote_validity_ms

    signature = sign_quote_v2(
        private_key=wallet.private_key,
        evm_chain_id=evm_chain_id,
        verifying_contract_bech32=contract_address,
        market_id=request["market_id"],
        rfq_id=int(request["rfq_id"]),
        taker=taker,
        direction=request["direction"],
        taker_margin=request["margin"],
        taker_quantity=request["quantity"],
        maker=wallet.inj_address,
        maker_subaccount_nonce=maker_subaccount_nonce,
        maker_quantity=quantity,
        maker_margin=margin,
        price=price,
        expiry_ms=expiry_ms,
    )

    return await client.send_quote(
        {
            "chain_id": chain_id,
            "contract_address": contract_address,
            "rfq_id": int(request["rfq_id"]),
            "market_id": request["market_id"],
            "taker_direction": request["direction"],
            "margin": margin,
            "quantity": quantity,
            "price": price,
            "expiry": expiry_ms,
            "maker": wallet.inj_address,
            "taker": taker,
            "signature": signature,
            "sign_mode": "v2",
            "evm_chain_id": evm_chain_id,
            "maker_subaccount_nonce": maker_subaccount_nonce,
        },
        wait_for_response=True,
        response_timeout=5.0,
    )


def _print_startup(
    *,
    config: EnvironmentConfig,
    market: MarketConfig,
    wallet: Wallet,
    key_source: str,
    mark: Decimal,
    edge_bps: Decimal,
    fixed_price: Decimal | None,
    max_quantity: Decimal,
    max_outstanding_quantity: Decimal,
    maker_subaccount_nonce: int,
    quote_validity_ms: int,
    price_tick: Decimal | None,
    qty_tick: Decimal | None,
    taker_address: str | None,
) -> None:
    chain_id, contract_address = config.signing_context
    evm_chain_id, _ = config.signing_context_v2
    print("RFQ mark quote loop")
    print(f"  env:      {config.environment}")
    print(f"  endpoint: {config.indexer.ws_endpoint}/MakerStream")
    print(f"  maker:    {wallet.inj_address}")
    print(f"  key:      {key_source}")
    print(f"  market:   {market.symbol} {market.id}")
    print(f"  chain:    {chain_id} / EVM {evm_chain_id}")
    print(f"  contract: {contract_address}")
    print(f"  mark:     {mark}")
    print(f"  edge:     {edge_bps} bps")
    if fixed_price is not None:
        print(f"  fixed:    {fixed_price}")
    print(f"  max qty:  {max_quantity} {market.base}")
    if max_outstanding_quantity > 0:
        print(f"  live cap: {max_outstanding_quantity} {market.base}")
    print(f"  subacct:  {maker_subaccount_nonce}")
    print(f"  expiry:   {quote_validity_ms} ms")
    print(f"  ticks:    price={price_tick} quantity={qty_tick}")
    if taker_address:
        print(f"  taker:    {taker_address}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Quote RFQs from mark +/- edge bps")
    parser.add_argument("--env", default=None, help="Override RFQ_ENV")
    parser.add_argument("--market-id", default=None, help="Configured market ID to quote")
    parser.add_argument(
        "--market-symbol",
        default=None,
        help='Configured market symbol, e.g. "DOGE/USDC PERP"',
    )
    parser.add_argument("--edge-bps", default="25", help="Quote edge in bps from mark")
    parser.add_argument(
        "--fixed-price",
        default=None,
        help="Quote this price instead of mark +/- edge",
    )
    parser.add_argument("--max-quantity", default="20", help="Max base quantity per quote")
    parser.add_argument(
        "--max-outstanding-quantity",
        default="0",
        help="Max base quantity across live, unexpired quotes; 0 disables",
    )
    parser.add_argument("--quote-validity-ms", type=int, default=3000)
    parser.add_argument("--mark-cache-seconds", type=float, default=60.0)
    parser.add_argument("--max-quotes", type=int, default=0, help="0 means unlimited")
    parser.add_argument("--max-runtime-seconds", type=float, default=0, help="0 means unlimited")
    parser.add_argument("--taker-address", default=None, help="Only quote RFQs from this taker")
    parser.add_argument("--maker-subaccount-nonce", type=int, default=0)
    parser.add_argument(
        "--ignore-worst-price",
        action="store_true",
        help="Do not clamp quotes to the taker's worst_price",
    )
    args = parser.parse_args()

    if args.env:
        os.environ["RFQ_ENV"] = args.env
        get_settings.cache_clear()
        get_environment_config.cache_clear()

    config = get_environment_config()
    if not config.contract.address:
        raise SystemExit("Set RFQ_CONTRACT_ADDRESS or contract.address before quoting.")

    market = _select_market(config, args.market_id, args.market_symbol)
    mm_private_key, key_source = _private_key_from_env()
    wallet = Wallet.from_private_key(mm_private_key)
    chain_id, contract_address = config.signing_context
    evm_chain_id, _ = config.signing_context_v2

    edge_bps = Decimal(args.edge_bps)
    fixed_price = Decimal(args.fixed_price) if args.fixed_price is not None else None
    max_quantity = Decimal(args.max_quantity)
    max_outstanding_quantity = Decimal(args.max_outstanding_quantity)
    if max_quantity <= 0:
        raise SystemExit("--max-quantity must be > 0")
    if max_outstanding_quantity < 0:
        raise SystemExit("--max-outstanding-quantity must be >= 0")
    if args.quote_validity_ms <= 0:
        raise SystemExit("--quote-validity-ms must be > 0")

    price_fetcher = PriceFetcher(config)
    price_fetcher._cache_ttl_seconds = args.mark_cache_seconds
    initial_mark = await price_fetcher.get_price(market)
    price_tick = price_fetcher.get_price_tick(market)
    qty_tick = price_fetcher.get_qty_tick(market)

    _print_startup(
        config=config,
        market=market,
        wallet=wallet,
        key_source=key_source,
        mark=initial_mark,
        edge_bps=edge_bps,
        fixed_price=fixed_price,
        max_quantity=max_quantity,
        max_outstanding_quantity=max_outstanding_quantity,
        maker_subaccount_nonce=args.maker_subaccount_nonce,
        quote_validity_ms=args.quote_validity_ms,
        price_tick=price_tick,
        qty_tick=qty_tick,
        taker_address=args.taker_address,
    )

    quotes_sent = 0
    outstanding_quotes: list[tuple[str, float, Decimal]] = []
    started_at = time.monotonic()

    async with MakerStreamClient(
        config.indexer.ws_endpoint,
        maker_address=wallet.inj_address,
        subscribe_to_quotes_updates=True,
        subscribe_to_settlement_updates=True,
        auth_private_key=wallet.private_key,
        auth_evm_chain_id=evm_chain_id,
        auth_contract_address=contract_address,
        timeout=30.0,
    ) as client:
        print("Connected. Listening for RFQs.")
        while True:
            runtime_elapsed = time.monotonic() - started_at
            if args.max_runtime_seconds and runtime_elapsed >= args.max_runtime_seconds:
                print("Max runtime reached; exiting.")
                return
            if args.max_quotes and quotes_sent >= args.max_quotes:
                print("Max quote count reached; exiting.")
                return

            now = time.monotonic()
            outstanding_quotes = [
                (rfq_id, expires_at, qty)
                for rfq_id, expires_at, qty in outstanding_quotes
                if expires_at > now
            ]

            try:
                event = await client.get_next_event(timeout=60.0)
            except IndexerTimeoutError:
                print("No RFQs in the last 60s; still listening.")
                continue
            if event is None:
                print("No RFQs in the last 60s; still listening.")
                continue

            msg_type, data = event
            if msg_type == "quote_update":
                update = client._processed_quote_to_dict(data)
                if update.get("maker") == wallet.inj_address:
                    print(
                        "Quote update:",
                        f"rfq_id={update['rfq_id']}",
                        f"status={update.get('status')}",
                        f"executed_qty={update.get('executed_quantity')}",
                        f"executed_margin={update.get('executed_margin')}",
                        f"error={update.get('error')}",
                    )
                    if update.get("status", "").lower() != "pending":
                        outstanding_quotes = [
                            item for item in outstanding_quotes if item[0] != update["rfq_id"]
                        ]
                continue

            if msg_type == "settlement_update":
                settlement = client._settlement_to_dict(data)
                print(
                    "Settlement update:",
                    f"rfq_id={settlement['rfq_id']}",
                    f"taker={settlement['taker']}",
                    f"direction={settlement['direction']}",
                    f"quantity={settlement['quantity']}",
                    f"margin={settlement['margin']}",
                    f"cid={settlement['cid']}",
                    f"quotes={settlement['quotes']}",
                )
                outstanding_quotes = [
                    item for item in outstanding_quotes if item[0] != settlement["rfq_id"]
                ]
                continue

            if msg_type == "error":
                print(f"Stream error: code={data.code} message={data.message_}")
                continue
            if msg_type != "request":
                continue

            request = client._request_to_dict(data)
            if request.get("market_id") != market.id:
                continue

            taker = request.get("taker") or request.get("request_address") or ""
            if args.taker_address and taker != args.taker_address:
                continue

            direction = _direction(request["direction"])
            if not direction:
                print(
                    f"Skipping RFQ#{request['rfq_id']}: "
                    f"unsupported direction={request['direction']}"
                )
                continue

            try:
                mark = await price_fetcher.get_price(market)
                price_tick = price_fetcher.get_price_tick(market)
                qty_tick = price_fetcher.get_qty_tick(market)
                price = _quote_price(
                    mark=mark,
                    direction=direction,
                    edge_bps=edge_bps,
                    tick=price_tick,
                    fixed_price=fixed_price,
                )
                raw_price = price
                if not args.ignore_worst_price:
                    price = _fit_price_to_worst(
                        price,
                        direction,
                        request["worst_price"],
                        price_tick,
                    )

                quantity, margin = _quote_size_and_margin(request, max_quantity, qty_tick)
                quote_qty = Decimal(quantity)
                outstanding_qty = sum(qty for _, _, qty in outstanding_quotes)
                if (
                    max_outstanding_quantity > 0
                    and outstanding_qty + quote_qty > max_outstanding_quantity
                ):
                    print(
                        "Skipping RFQ:",
                        f"rfq_id={request['rfq_id']}",
                        f"taker={taker}",
                        f"outstanding_qty={outstanding_qty}",
                        f"quote_qty={quote_qty}",
                        f"cap={max_outstanding_quantity}",
                    )
                    continue

                side = "sell" if direction == "long" else "buy"
                print(
                    "RFQ:",
                    f"rfq_id={request['rfq_id']}",
                    f"taker={taker}",
                    f"side={side}",
                    f"request_qty={request['quantity']}",
                    f"request_margin={request['margin']}",
                    f"quote_qty={quantity}",
                    f"quote_margin={margin}",
                    f"mark={mark}",
                    f"price={price}",
                    f"raw_price={raw_price}",
                    f"worst={request['worst_price']}",
                )

                send_started = time.monotonic()
                ack = await _send_quote(
                    client=client,
                    wallet=wallet,
                    request=request,
                    chain_id=chain_id,
                    contract_address=contract_address,
                    evm_chain_id=evm_chain_id,
                    price=price,
                    quantity=quantity,
                    margin=margin,
                    quote_validity_ms=args.quote_validity_ms,
                    maker_subaccount_nonce=args.maker_subaccount_nonce,
                )
                quotes_sent += 1
                outstanding_quotes.append(
                    (
                        str(request["rfq_id"]),
                        time.monotonic() + args.quote_validity_ms / 1000,
                        quote_qty,
                    )
                )
                ack_ms = (time.monotonic() - send_started) * 1000
                print(
                    "Quote ACK:",
                    f"rfq_id={request['rfq_id']}",
                    f"status={ack.get('status') if ack else 'unknown'}",
                    f"ack_ms={ack_ms:.1f}",
                )
            except IndexerValidationError as exc:
                print(f"Quote rejected: rfq_id={request['rfq_id']} error={exc}")
            except Exception as exc:
                print(f"Quote failed: rfq_id={request['rfq_id']} error={exc}")


if __name__ == "__main__":
    asyncio.run(main())
