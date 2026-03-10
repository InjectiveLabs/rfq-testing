"""Full E2E test: Retail sends request → MM quotes → Retail accepts on-chain.

v4: Drains stale requests, matches rfq_id explicitly.
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

from rfq_test.config import get_settings, get_environment_config
from rfq_test.crypto.wallet import Wallet
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient
from rfq_test.clients.contract import ContractClient
from rfq_test.crypto.signing import sign_quote
from rfq_test.models.types import Direction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("rfq_settlement_test")


async def drain_stale_requests(mm_client):
    """Drain any stale requests sitting in the MM queue."""
    count = 0
    while True:
        try:
            req = await mm_client.wait_for_request(timeout=2)
            count += 1
            logger.info(f"Drained stale request: RFQ#{req['rfq_id']}")
        except Exception:
            break
    if count:
        print(f"   🧹 Drained {count} stale request(s)")


async def mm_wait_and_quote(mm_client, mm_wallet, chain_id, contract_address, target_rfq_id):
    """MM: wait for OUR request (by rfq_id), then sign and send quote."""
    print(f"   ⏳ MM waiting for RFQ#{target_rfq_id}...")

    # Keep pulling requests until we find ours
    start = time.monotonic()
    received = None
    while (time.monotonic() - start) < 45:
        try:
            req = await mm_client.wait_for_request(timeout=5)
            if int(req["rfq_id"]) == target_rfq_id:
                received = req
                break
            else:
                logger.info(f"Skipping other request: RFQ#{req['rfq_id']}")
        except Exception:
            continue

    if not received:
        print(f"   ❌ MM never received RFQ#{target_rfq_id}")
        return None

    print(f"   ✅ MM received RFQ#{received['rfq_id']}")

    taker = received.get("taker") or received.get("request_address", "")
    quote_expiry = int(time.time() * 1000) + 60_000

    signature = sign_quote(
        private_key=mm_wallet.private_key,
        rfq_id=str(received["rfq_id"]),
        market_id=received["market_id"],
        direction="long",
        taker=taker,
        taker_margin=received["margin"],
        taker_quantity=received["quantity"],
        maker=mm_wallet.inj_address,
        maker_margin=received["margin"],
        maker_quantity=received["quantity"],
        price="4.5",
        expiry=quote_expiry,
        chain_id=chain_id,
        contract_address=contract_address,
    )

    quote_data = {
        "chain_id": chain_id,
        "contract_address": contract_address,
        "rfq_id": received["rfq_id"],
        "market_id": received["market_id"],
        "taker_direction": "long",
        "margin": received["margin"],
        "quantity": received["quantity"],
        "price": "4.5",
        "expiry": quote_expiry,
        "maker": mm_wallet.inj_address,
        "taker": taker,
        "signature": signature,
    }

    print(f"   📤 MM sending quote (price=4.5)...")
    response = await mm_client.send_quote(quote_data, wait_for_response=True, response_timeout=10.0)
    print(f"   📬 Indexer ACK: {response}")
    return quote_data


async def main():
    os.environ.setdefault("RFQ_ENV", "testnet")
    config = get_environment_config()
    settings = get_settings()

    mm_pk = settings.mm_private_key
    retail_pk = settings.retail_private_key
    if not mm_pk or not retail_pk:
        print("❌ Set TESTNET_MM_PRIVATE_KEY and TESTNET_RETAIL_PRIVATE_KEY in .env")
        return

    mm_wallet = Wallet.from_private_key(mm_pk)
    retail_wallet = Wallet.from_private_key(retail_pk)
    market = config.default_market
    chain_id, contract_address = config.signing_context

    print("=" * 60)
    print("RFQ FULL SETTLEMENT TEST (TESTNET)")
    print("=" * 60)
    print(f"🏦 MM:       {mm_wallet.inj_address}")
    print(f"👤 Retail:   {retail_wallet.inj_address}")
    print(f"📊 Market:   {market.symbol}")
    print(f"⛓️  Chain:    {chain_id}")
    print(f"📜 Contract: {contract_address}")
    print("=" * 60)

    # ─── PHASE 1: WebSocket round-trip ───
    print("\n📡 PHASE 1: WebSocket Round-Trip")
    print("-" * 40)

    mm_client = MakerStreamClient(
        config.indexer.ws_endpoint,
        maker_address=mm_wallet.inj_address,
        timeout=10.0,
    )
    await mm_client.connect()
    print("   ✅ MM connected to MakerStream")

    retail_client = TakerStreamClient(
        config.indexer.ws_endpoint,
        request_address=retail_wallet.inj_address,
        timeout=10.0,
    )
    await retail_client.connect()
    print("   ✅ Retail connected to TakerStream")

    # Drain stale messages from live testnet traffic
    await asyncio.sleep(3)
    await drain_stale_requests(mm_client)

    rfq_id = int(time.time() * 1000)
    quantity = "1"
    margin = "10"

    request_data = {
        "request_address": retail_wallet.inj_address,
        "rfq_id": rfq_id,
        "market_id": market.id,
        "direction": "long",
        "margin": margin,
        "quantity": quantity,
        "worst_price": "100",
        "expiry": rfq_id + 300_000,
    }

    print(f"\n   📤 Retail sending request (RFQ#{rfq_id})...")
    await retail_client.send_request(request_data)

    # Run MM and retail concurrently; MM filters for our rfq_id
    mm_task = asyncio.create_task(
        mm_wait_and_quote(mm_client, mm_wallet, chain_id, contract_address, rfq_id)
    )

    print(f"   ⏳ Retail collecting quotes (45s window)...")
    quotes = await retail_client.collect_quotes(rfq_id=rfq_id, timeout=45, min_quotes=1)

    sent_quote = await mm_task

    if not quotes:
        print("   ❌ No quotes received. Aborting.")
        await mm_client.close()
        await retail_client.close()
        return

    best_quote = quotes[0]
    print(f"\n   ✅ Retail got {len(quotes)} quote(s)")
    print(f"      Price: {best_quote['price']}")
    print(f"      Maker: {best_quote['maker']}")

    await mm_client.close()
    await retail_client.close()
    print("   📡 WS closed")

    # ─── PHASE 2: On-Chain Settlement ───
    print("\n⛓️  PHASE 2: On-Chain Settlement")
    print("-" * 40)

    contract_client = ContractClient(config.contract, config.chain)

    contract_quote = {
        "maker": best_quote["maker"],
        "margin": best_quote["margin"],
        "quantity": best_quote["quantity"],
        "price": best_quote["price"],
        "expiry": int(best_quote["expiry"]),
        "signature": best_quote["signature"],
    }

    print(f"   📝 Submitting AcceptQuote...")
    print(f"      RFQ ID:    {rfq_id}")
    print(f"      Direction: LONG")
    print(f"      Margin:    {margin}, Qty: {quantity}")
    print(f"      Price:     {best_quote['price']}")
    print(f"      Maker:     {best_quote['maker']}")

    try:
        tx_hash = await contract_client.accept_quote(
            private_key=retail_pk,
            quotes=[contract_quote],
            rfq_id=str(rfq_id),
            market_id=market.id,
            direction=Direction.LONG,
            margin=Decimal(margin),
            quantity=Decimal(quantity),
            worst_price=Decimal("100"),
            unfilled_action={"market": {}},
        )
        print(f"\n   🎉 SETTLEMENT SUCCESSFUL!")
        print(f"   📜 TX Hash: {tx_hash}")
        print(f"   🔗 https://testnet.explorer.injective.network/transaction/{tx_hash}")
    except Exception as e:
        print(f"\n   ❌ SETTLEMENT FAILED: {e}")
        logger.exception("Settlement error:")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
