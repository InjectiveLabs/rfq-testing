#!/usr/bin/env python3
"""Test canonical decimal form on testnet INJ/USDC PERP (tick 0.01, v2 EIP-712).

One fresh RFQ per variant so the "1 quote per maker per RFQ" rule doesn't
mask results. Each variant is signed with its exact wire string, so an ACK
means BOTH the canonical-form validator and the v2 signature verifier
accepted the value.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

import dotenv
dotenv.load_dotenv()

from rfq_test.config import get_environment_config
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient
from rfq_test.crypto.eip712 import sign_quote_v2
from rfq_test.crypto.wallet import Wallet
from rfq_test.exceptions import IndexerValidationError, IndexerTimeoutError

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

VARIANTS = [
    # (label, wire string)  — INJ/USDC PERP tick = 0.01
    ("integer-canonical",         "110"),
    ("fractional-at-tick",        "110.01"),
    ("fractional-half-tick",      "110.5"),
    ("integer-trailing-zero",     "110.0"),
    ("integer-double-zero",       "110.00"),
    ("fractional-trailing-zero",  "110.10"),
    ("very-precise-canonical",    "110.99"),
]
PRICE = "1.5"
MARGIN = "300"
REQUEST_QTY = "300"   # ≥ every variant


def classify(msg: str) -> str:
    m = msg.lower()
    if "canonical decimal form" in m:
        return "REJECT — canonical form"
    if "signature" in m and ("verify" in m or "match" in m or "recover" in m):
        return "REJECT — signature"
    if "already been submitted" in m:
        return "blocked — dup"
    return "OTHER"


async def fresh_rfq_and_quote(retail_ws, mm_ws, env, retail_addr, mm_pk, mm_addr,
                              market, q_str, evm_chain_id, contract, chain_id):
    """Open one new RFQ, MM picks it up, MM sends one quote with q_str."""
    request_data = {
        "request_address": retail_addr,
        "client_id": str(uuid.uuid4()),
        "market_id": market.id,
        "direction": "long",
        "margin": MARGIN,
        "quantity": REQUEST_QTY,
        "worst_price": "10",
        "expiry": {"ts": int(time.time() * 1000) + 300_000},
    }
    ack = await retail_ws.send_request(request_data, wait_for_response=True, response_timeout=10.0)
    if not ack or not ack.get("rfq_id"):
        return ("FAIL — no RFQ ack", str(ack))
    rfq_id = int(ack["rfq_id"])

    # Drain MM stream until we see THIS rfq_id (skip stale ones from prior runs)
    mm_received = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not mm_received:
        try:
            req = await mm_ws.wait_for_request(timeout=2.5)
            if int(req["rfq_id"]) == rfq_id:
                mm_received = req
                break
        except Exception:
            continue
    if not mm_received:
        return (f"FAIL — MM never saw RFQ#{rfq_id}", "")
    taker = mm_received.get("taker") or mm_received.get("request_address", retail_addr)

    expiry = int(time.time() * 1000) + 60_000
    sig = sign_quote_v2(
        private_key=mm_pk, evm_chain_id=evm_chain_id,
        verifying_contract_bech32=contract,
        market_id=market.id, rfq_id=rfq_id,
        taker=taker, direction="long",
        taker_margin=MARGIN, taker_quantity=REQUEST_QTY,
        maker=mm_addr, maker_margin=MARGIN, maker_quantity=q_str,
        price=PRICE, expiry_ms=expiry,
    )
    try:
        resp = await mm_ws.send_quote({
            "rfq_id": rfq_id, "market_id": market.id,
            "taker_direction": "long",
            "margin": MARGIN, "quantity": q_str, "price": PRICE,
            "expiry": expiry,
            "maker": mm_addr, "taker": taker,
            "signature": sig, "sign_mode": "v2",
            "chain_id": chain_id, "contract_address": contract,
        }, wait_for_response=True, response_timeout=8.0)
        return ("✓ ACCEPT", f"ACK status={resp.get('status', '?')}" if resp else "no response")
    except IndexerValidationError as e:
        msg = str(e)
        return (classify(msg), msg)
    except IndexerTimeoutError as e:
        return ("TIMEOUT", str(e))


async def main():
    os.environ.setdefault("RFQ_ENV", "testnet")
    env = get_environment_config()
    chain_id, contract = env.signing_context
    evm_chain_id, _ = env.signing_context_v2

    mm_pk = os.environ["TESTNET_MM_PRIVATE_KEY"]
    retail_pk = os.environ["TESTNET_RETAIL_PRIVATE_KEY"]
    mm = Wallet.from_private_key(mm_pk)
    retail = Wallet.from_private_key(retail_pk)
    market = env.markets[0]   # INJ/USDC PERP

    print(f"Env:      testnet ({chain_id}, evm_chain_id={evm_chain_id})")
    print(f"Market:   {market.symbol}  ({market.id[:18]}…)  tick=0.01\n")

    retail_ws = TakerStreamClient(env.indexer.ws_endpoint,
                                  request_address=retail.inj_address, timeout=15.0)
    await retail_ws.connect()
    mm_ws = MakerStreamClient(env.indexer.ws_endpoint,
                              maker_address=mm.inj_address, timeout=15.0)
    await mm_ws.connect()
    await asyncio.sleep(1.5)

    print(f"Variant                     wire      │ Result")
    print(f"────────────────────────────────────────┼────────────────────────────────────────")
    for label, q_str in VARIANTS:
        tag, detail = await fresh_rfq_and_quote(
            retail_ws, mm_ws, env, retail.inj_address, mm_pk, mm.inj_address,
            market, q_str, evm_chain_id, contract, chain_id,
        )
        print(f"{label:<27}{q_str!r:>10} │ {tag}")
        if detail:
            print(f"{'':<37} │   ↳ {detail[:110]}")
        await asyncio.sleep(0.4)

    await mm_ws.close()
    await retail_ws.close()


if __name__ == "__main__":
    asyncio.run(main())
