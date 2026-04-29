"""Multi-quote aggregation example: one taker collects quotes from N makers
and accepts several of them in a single AcceptQuote transaction.

The TrueCurrent contract's `quotes` field is a Vec<Quote>, and the handler
walks the list in submission order, filling from each until the taker's
total `quantity` is covered. This lets a taker aggregate partial fills
across multiple makers into a single atomic position.

This example spins up three simulated makers on the MakerStream, each
offering a partial fill at a different price. The retail taker submits
one RFQ, collects all three quotes, sorts them cheapest-first, and
settles against the full set in a single transaction.

Requires:
  - TESTNET_MM_PRIVATE_KEY_1 / _2 / _3   (three whitelisted MM wallets)
  - TESTNET_RETAIL_PRIVATE_KEY           (taker wallet, authz granted, subaccount funded)
  - RFQ_ENV=testnet
"""
import asyncio
import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import dotenv
dotenv.load_dotenv()

from rfq_test.config import get_environment_config
from rfq_test.crypto.wallet import Wallet
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient
from rfq_test.clients.contract import ContractClient
from rfq_test.crypto.eip712 import sign_quote_v2
from rfq_test.models.types import Direction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("rfq_multi_quote")

# Each maker's slice of the total
MAKER_SPECS = [
    {"env_key": "TESTNET_MM_PRIVATE_KEY_1", "quantity": "40", "price": "4.90"},
    {"env_key": "TESTNET_MM_PRIVATE_KEY_2", "quantity": "40", "price": "4.92"},
    {"env_key": "TESTNET_MM_PRIVATE_KEY_3", "quantity": "50", "price": "4.95"},
]

# Taker wants 100 INJ. Three makers together offer 130, so one partial fill happens.
TOTAL_QUANTITY = Decimal("100")
TOTAL_MARGIN = Decimal("200")
WORST_PRICE = Decimal("5.00")


async def mm_listen_and_quote(
    spec: dict,
    target_rfq_id: int,
    chain_id: str,
    contract_address: str,
    evm_chain_id: int,
    config,
) -> dict | None:
    """One maker: wait for the target RFQ, sign and return a quote."""
    pk = os.getenv(spec["env_key"])
    if not pk:
        logger.error(f"{spec['env_key']} not set; skipping this maker")
        return None

    mm_wallet = Wallet.from_private_key(pk)
    mm_ws = MakerStreamClient(config.indexer.ws_endpoint, timeout=30.0)
    await mm_ws.connect()
    logger.info(f"Maker {mm_wallet.inj_address[:15]}... connected")

    try:
        # Drain anything stale
        start = time.monotonic()
        received = None
        while (time.monotonic() - start) < 30:
            try:
                req = await mm_ws.wait_for_request(timeout=3)
                if int(req["rfq_id"]) == target_rfq_id:
                    received = req
                    break
            except Exception:
                continue

        if not received:
            logger.warning(f"Maker {mm_wallet.inj_address[:15]}... never saw RFQ#{target_rfq_id}")
            return None

        taker = received.get("taker") or received.get("request_address", "")
        quote_expiry = int(time.time() * 1000) + 60_000

        signature = sign_quote_v2(
            private_key=mm_wallet.private_key,
            evm_chain_id=evm_chain_id,
            verifying_contract_bech32=contract_address,
            market_id=received["market_id"],
            rfq_id=int(target_rfq_id),
            taker=taker,
            direction="long",
            taker_margin=received["margin"],
            taker_quantity=received["quantity"],
            maker=mm_wallet.inj_address,
            maker_margin=spec["quantity"],  # maker margin == maker quantity (1x leverage)
            maker_quantity=spec["quantity"],
            price=spec["price"],
            expiry_ms=quote_expiry,
        )

        quote_data = {
            "chain_id": chain_id,
            "contract_address": contract_address,
            "rfq_id": target_rfq_id,
            "market_id": received["market_id"],
            "taker_direction": "long",
            "margin": spec["quantity"],
            "quantity": spec["quantity"],
            "price": spec["price"],
            "expiry": quote_expiry,
            "maker": mm_wallet.inj_address,
            "taker": taker,
            "signature": signature,
            "sign_mode": "v2",
        }

        await mm_ws.send_quote(quote_data)
        logger.info(
            f"Maker {mm_wallet.inj_address[:15]}... sent quote "
            f"qty={spec['quantity']} price={spec['price']}"
        )
        return quote_data
    finally:
        await mm_ws.close()


async def main():
    os.environ.setdefault("RFQ_ENV", "testnet")
    config = get_environment_config()

    retail_pk = os.getenv("TESTNET_RETAIL_PRIVATE_KEY")
    if not retail_pk:
        print("❌ Set TESTNET_RETAIL_PRIVATE_KEY in .env")
        sys.exit(1)

    retail_wallet = Wallet.from_private_key(retail_pk)
    market = config.default_market
    chain_id, contract_address = config.signing_context
    evm_chain_id, _ = config.signing_context_v2

    print(f"👤 Taker: {retail_wallet.inj_address}")
    print(f"📊 Market: {market.symbol}")
    print(f"🎯 Request: {TOTAL_QUANTITY} @ worst_price {WORST_PRICE}")
    print(f"🏦 Makers: {len(MAKER_SPECS)} (aggregate supply "
          f"{sum(Decimal(s['quantity']) for s in MAKER_SPECS)})")
    print()

    rfq_id = int(time.time() * 1000)

    # Taker connects and submits request
    taker_ws = TakerStreamClient(
        endpoint=config.indexer.ws_endpoint,
        request_address=retail_wallet.inj_address,
        timeout=30.0,
    )
    await taker_ws.connect()
    print("✅ Taker connected to TakerStream")

    # Makers start listening in parallel (they'll see the broadcast)
    maker_tasks = [
        asyncio.create_task(
            mm_listen_and_quote(spec, rfq_id, chain_id, contract_address, evm_chain_id, config)
        )
        for spec in MAKER_SPECS
    ]

    # Give makers 2s to subscribe before we send the request
    await asyncio.sleep(2)

    request_data = {
        "request_address": retail_wallet.inj_address,
        "rfq_id": rfq_id,
        "market_id": market.id,
        "direction": "long",
        "margin": str(TOTAL_MARGIN),
        "quantity": str(TOTAL_QUANTITY),
        "worst_price": str(WORST_PRICE),
        "expiry": rfq_id + 300_000,
    }
    await taker_ws.send_request(request_data)
    print(f"📤 Taker sent RFQ#{rfq_id}")

    # Collect quotes from the stream (not from the maker tasks — those are just
    # there to produce the quotes; the indexer is the source of truth for taker)
    quotes = await taker_ws.collect_quotes(rfq_id=rfq_id, timeout=8.0, min_quotes=1)
    print(f"📩 Taker received {len(quotes)} quote(s) from the indexer")

    # Let the maker tasks finish (they'll close their own streams)
    await asyncio.gather(*maker_tasks, return_exceptions=True)

    if not quotes:
        print("❌ No quotes received — check that MMs are whitelisted and online")
        await taker_ws.close()
        return

    # SORT BY PRICE — critical for correct multi-quote matching.
    # For a long, cheapest first. The contract processes in submission order.
    quotes.sort(key=lambda q: Decimal(q["price"]))

    print("\n🏆 Quotes sorted cheapest-first:")
    for i, q in enumerate(quotes):
        print(f"   {i+1}. qty={q['quantity']:>5} @ {q['price']:>6}  from {q['maker'][:20]}...")

    # Build contract-format quotes (ContractClient handles hex→base64 + expiry wrap)
    contract_quotes = [
        {
            "maker": q["maker"],
            "margin": q["margin"],
            "quantity": q["quantity"],
            "price": q["price"],
            "expiry": int(q["expiry"]),
            "signature": q["signature"],
        }
        for q in quotes
    ]

    print(f"\n⛓️  Submitting AcceptQuote with {len(contract_quotes)} quotes...")
    contract = ContractClient(config.contract, config.chain)

    try:
        tx_hash = await contract.accept_quote(
            private_key=retail_pk,
            quotes=contract_quotes,
            rfq_id=str(rfq_id),
            market_id=market.id,
            direction=Direction.LONG,
            margin=TOTAL_MARGIN,
            quantity=TOTAL_QUANTITY,
            worst_price=WORST_PRICE,
            unfilled_action=None,  # strict RFQ fill, no orderbook fallback
        )
        print(f"\n✅ SETTLED. TxHash: {tx_hash}")
        print(f"   Expected fill: 40 @ 4.90 + 40 @ 4.92 + 20 @ 4.95 = 100 INJ")
        print(f"   Expected blended entry: {((40*4.90)+(40*4.92)+(20*4.95))/100:.4f}")
    except Exception as e:
        print(f"\n❌ AcceptQuote failed: {e}")
    finally:
        await taker_ws.close()


if __name__ == "__main__":
    asyncio.run(main())
