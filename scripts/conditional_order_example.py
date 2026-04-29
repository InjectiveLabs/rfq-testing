#!/usr/bin/env python3
"""
Example: create and cancel a conditional order (TP/SL) on the RFQ indexer.

This script demonstrates the full lifecycle of a conditional order:
  1. Sign the order with the taker's private key
  2. Submit it via the TakerStream WebSocket
  3. List active orders via the REST API
  4. Cancel via CancelIntentLane (on-chain)

Usage:
    # Submit a take-profit order via WebSocket:
    python scripts/conditional_order_example.py --env testnet submit

    # List your active conditional orders:
    python scripts/conditional_order_example.py --env testnet list

    # Cancel all orders on a market lane:
    python scripts/conditional_order_example.py --env testnet cancel

Environment variables (or set in .env):
    RETAIL_PRIVATE_KEY  — taker's hex private key (with or without 0x)
    RFQ_ENV             — environment name (default: testnet)
"""

import asyncio
import os
import sys
import time
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import argparse
import httpx

from rfq_test.config import get_environment_config, Settings
from rfq_test.crypto.eip712 import sign_conditional_order_v2
from rfq_test.crypto.wallet import Wallet
from rfq_test.clients.contract import ContractClient
from rfq_test.clients.websocket import TakerStreamClient
from rfq_test.utils.price import get_market_tick_sizes, quantize_to_tick, quantize_quantity


def _load_env() -> tuple[str, object]:
    """Load private key from environment and return (private_key, env_config)."""
    from dotenv import load_dotenv
    load_dotenv()

    private_key = os.environ.get("RETAIL_PRIVATE_KEY", "").strip()
    if not private_key:
        sys.exit("RETAIL_PRIVATE_KEY is not set. Copy .env.example to .env and fill it in.")

    env_config = get_environment_config()
    return private_key, env_config


async def _build_order(private_key: str, env_config, market_id: str) -> tuple[dict, str]:
    """Build and sign a take-profit (mark_price_gte) conditional order.

    Returns (order_body, signature).

    IMPORTANT — Tick Size Quantization
    ------------------------------------
    All prices and quantities MUST be quantized to the market's tick sizes
    BEFORE signing. The signed strings and the strings sent to the indexer
    must be byte-for-byte identical. Signing an unquantized price and then
    quantizing afterward will cause signature verification to fail.

    This function fetches the market's tick sizes from the chain, applies
    them to all price/quantity fields, and only then signs.

    Customize the parameters below for your strategy:
      - trigger_type / trigger_price: when the order fires
      - direction: "long" or "short"
      - quantity / worst_price: size and price protection
      - deadline_ms: expiry (here: 24 hours from now; max is 30 days)
    """
    wallet = Wallet.from_private_key(private_key)
    chain_id, contract_address = env_config.signing_context
    evm_chain_id, _ = env_config.signing_context_v2

    # Step 1 — Fetch market tick sizes from the chain.
    # Cache and reuse in production; this is fine for a script.
    ticks = await get_market_tick_sizes(env_config.chain.lcd_endpoint, market_id)
    price_tick = ticks.get("min_price_tick")   # e.g. Decimal("0.001")
    qty_tick   = ticks.get("min_qty_tick")     # e.g. Decimal("0.001")

    if not price_tick or not qty_tick:
        print(
            f"  Warning: could not fetch tick sizes for {market_id[:20]}… "
            f"(got {ticks}). Prices may be rejected by the exchange."
        )

    rfq_id = int(time.time() * 1000)            # unique ID — use ms timestamp
    deadline_ms = rfq_id + 24 * 60 * 60 * 1000  # 24 hours from now

    # Conditional order parameters — adjust to your needs
    epoch = 1               # increment after each CancelAllIntents call
    lane_version = 1        # increment after each CancelIntentLane call
    subaccount_nonce = 0    # subaccount index (0 = default subaccount)
    direction = "short"

    raw_quantity    = Decimal("1")
    raw_worst_price = Decimal("132")       # worst acceptable fill price
    raw_trigger     = Decimal("120")       # fires when mark price >= this
    trigger_type    = "mark_price_gte"     # take-profit for short

    # Step 2 — Quantize BEFORE signing.
    #
    # worst_price for SHORT direction (taker sells) = minimum price they'll accept.
    # Round DOWN (ROUND_FLOOR) so the quantized floor is slightly lower → gives the
    # MM more room to quote above it → order is more likely to be filled.
    # (The difference is one tick, so protection is nearly identical.)
    #
    # For LONG direction (taker buys), worst_price = maximum they'll pay → ROUND_CEILING.
    quantity    = quantize_quantity(raw_quantity, qty_tick)
    worst_price = quantize_to_tick(raw_worst_price, price_tick, rounding=ROUND_FLOOR)
    trigger_price_str = quantize_to_tick(raw_trigger, price_tick)

    # Step 3 — Sign using the quantized strings (not the raw Decimals).
    # v2 (EIP-712) signing: the signature binds (evm_chain_id, contract) via the
    # domain separator, so the wire-payload `chain_id` / `contract_address`
    # fields are informational only.
    signature = sign_conditional_order_v2(
        private_key=private_key,
        evm_chain_id=evm_chain_id,
        verifying_contract_bech32=contract_address,
        version=1,
        taker=wallet.inj_address,
        epoch=epoch,
        rfq_id=rfq_id,
        market_id=market_id,
        subaccount_nonce=subaccount_nonce,
        lane_version=lane_version,
        deadline_ms=deadline_ms,
        direction=direction,
        quantity=quantity,
        worst_price=worst_price,
        min_total_fill_quantity=quantity,
        trigger_type=trigger_type,
        trigger_price=trigger_price_str,
        margin="0",  # must be "0" for reduce-only (close-position) orders
    )

    # Step 4 — Build the order body using the SAME quantized strings as signed.
    order_body = {
        "version": 1,
        "chain_id": chain_id,
        "contract_address": contract_address,
        "taker": wallet.inj_address,
        "epoch": epoch,
        "rfq_id": rfq_id,
        "market_id": market_id,
        "subaccount_nonce": subaccount_nonce,
        "lane_version": lane_version,
        "deadline_ms": deadline_ms,
        "direction": direction,
        "quantity": quantity,
        "margin": "0",
        "worst_price": worst_price,
        "min_total_fill_quantity": quantity,
        "trigger_type": trigger_type,
        "trigger_price": trigger_price_str,
        "unfilled_action": None,
        "cid": None,
        "allowed_relayer": None,
    }

    return order_body, signature


async def cmd_submit(args) -> None:
    """Sign and submit a conditional order via TakerStream WebSocket."""
    private_key, env_config = _load_env()
    market = env_config.default_market
    wallet = Wallet.from_private_key(private_key)

    order_body, signature = await _build_order(private_key, env_config, market.id)

    print(f"\nSubmitting conditional order via WebSocket")
    print(f"  Taker     : {wallet.inj_address}")
    print(f"  Market    : {market.symbol} ({market.id[:20]}…)")
    print(f"  Direction : {order_body['direction']}")
    print(f"  Trigger   : {order_body['trigger_type']} @ {order_body['trigger_price']}")
    print(f"  Quantity  : {order_body['quantity']}")
    print(f"  RFQ ID    : {order_body['rfq_id']}")

    async with TakerStreamClient(
        base_url=env_config.indexer.ws_endpoint,
        request_address=wallet.inj_address,
    ) as client:
        result = await client.send_conditional_order(
            order_body=order_body,
            signature=signature,
            wait_for_ack=True,
            response_timeout=10.0,
        )

    print(f"\nACK received:")
    print(f"  rfq_id : {result['rfq_id']}")
    print(f"  status : {result['status']}")
    print(f"\nUse '{sys.argv[0]} list' to see the order, or '{sys.argv[0]} cancel' to cancel it.")


async def cmd_list(args) -> None:
    """List active conditional orders via the REST API."""
    private_key, env_config = _load_env()
    wallet = Wallet.from_private_key(private_key)

    url = f"{env_config.indexer.http_endpoint.rstrip('/')}/conditionalOrders"
    params = {"taker": wallet.inj_address}

    print(f"\nListing conditional orders for {wallet.inj_address}")
    print(f"  GET {url}")

    async with httpx.AsyncClient(timeout=15.0) as http:
        resp = await http.get(url, params=params)

    if not resp.is_success:
        print(f"\nHTTP {resp.status_code}: {resp.text[:300]}")
        return

    data = resp.json()
    orders = data if isinstance(data, list) else data.get("orders", [data])

    if not orders:
        print("\nNo active conditional orders found.")
        return

    print(f"\nFound {len(orders)} order(s):\n")
    for o in orders:
        rfq_id = o.get("rfq_id", "?")
        status = o.get("status", "?")
        trigger_type = o.get("trigger_type", "?")
        trigger_price = o.get("trigger_price", "?")
        direction = o.get("direction", "?")
        quantity = o.get("quantity", "?")
        print(f"  RFQ#{rfq_id}  status={status}  {direction}  qty={quantity}  {trigger_type}@{trigger_price}")


async def cmd_cancel(args) -> None:
    """Cancel all conditional orders on the default market lane (CancelIntentLane)."""
    private_key, env_config = _load_env()
    wallet = Wallet.from_private_key(private_key)
    market = env_config.default_market

    print(f"\nCancelIntentLane")
    print(f"  Taker          : {wallet.inj_address}")
    print(f"  Market         : {market.symbol} ({market.id[:20]}…)")
    print(f"  subaccount_nonce: 0")
    print(f"\nThis cancels ALL active conditional orders for this taker+market lane.")
    print(f"Broadcasting transaction…")

    contract_client = ContractClient(env_config.contract, env_config.chain)
    tx_hash = await contract_client.cancel_intent_lane(
        private_key=private_key,
        market_id=market.id,
        subaccount_nonce=0,
    )

    print(f"\nTransaction confirmed: {tx_hash}")
    print(f"\nActive orders for this lane have been cancelled.")
    print(f"Note: increment lane_version in future orders to avoid reuse.")


async def cmd_cancel_all(args) -> None:
    """Cancel ALL conditional orders across all markets (CancelAllIntents)."""
    private_key, env_config = _load_env()
    wallet = Wallet.from_private_key(private_key)

    print(f"\nCancelAllIntents")
    print(f"  Taker: {wallet.inj_address}")
    print(f"\nThis cancels ALL active conditional orders across all markets.")
    print(f"Broadcasting transaction…")

    contract_client = ContractClient(env_config.contract, env_config.chain)
    tx_hash = await contract_client.cancel_all_intents(private_key=private_key)

    print(f"\nTransaction confirmed: {tx_hash}")
    print(f"\nAll conditional orders cancelled.")
    print(f"Note: increment epoch in future orders.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Conditional order (TP/SL) example",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env", default=os.environ.get("RFQ_ENV", "testnet"),
                        help="Environment (default: testnet)")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("submit", help="Sign and submit a conditional order via WebSocket")
    subparsers.add_parser("list", help="List active conditional orders via REST API")
    subparsers.add_parser("cancel", help="Cancel orders on default market lane (CancelIntentLane)")
    subparsers.add_parser("cancel-all", help="Cancel all orders across all markets (CancelAllIntents)")

    args = parser.parse_args()

    os.environ["RFQ_ENV"] = args.env

    commands = {
        "submit": cmd_submit,
        "list": cmd_list,
        "cancel": cmd_cancel,
        "cancel-all": cmd_cancel_all,
    }

    if not args.command or args.command not in commands:
        parser.print_help()
        sys.exit(1)

    asyncio.run(commands[args.command](args))


if __name__ == "__main__":
    main()
