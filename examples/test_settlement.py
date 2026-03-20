"""Full E2E test: Retail sends request → MM quotes → Retail accepts on-chain.

v4: Drains stale requests, matches rfq_id explicitly.
"""
import asyncio
import logging
import os
import sys
import time
import uuid
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
from rfq_test.utils.price import PriceFetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("rfq_settlement_test")


async def drain_stale_requests(mm_client):
    """Drain any stale requests already queued (non-blocking)."""
    count = 0
    while not mm_client._message_queue.empty():
        try:
            event_type, data = mm_client._message_queue.get_nowait()
            if event_type == "request":
                logger.info(f"Drained stale request: RFQ#{data.rfq_id}")
                count += 1
        except Exception:
            break
    if count:
        print(f"   🧹 Drained {count} stale request(s)")


async def mm_wait_and_quote(
    mm_client,
    mm_wallet,
    chain_id,
    contract_address,
    target_rfq_id,
    quote_price: Decimal,
):
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
        price=str(quote_price),
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
        "price": str(quote_price),
        "expiry": quote_expiry,
        "maker": mm_wallet.inj_address,
        "taker": taker,
        "signature": signature,
    }

    print(f"   📤 MM sending quote (price={quote_price})...")
    response = await mm_client.send_quote(quote_data, wait_for_response=True, response_timeout=10.0)
    print(f"   📬 Indexer ACK: {response}")
    return quote_data


async def mm_wait_for_post_settlement_updates(mm_client, target_rfq_id, timeout=60.0):
    """Wait for quote_update and settlement_update for the target RFQ."""
    print(f"   ⏳ MM waiting for quote_update + settlement_update for RFQ#{target_rfq_id}...")
    start = time.monotonic()
    quote_update = None
    settlement_update = None

    while (time.monotonic() - start) < timeout:
        event = await mm_client.get_next_event(timeout=2.0)
        if event is None:
            continue

        event_type, data = event
        event_rfq_id = getattr(data, "rfq_id", None)
        if event_rfq_id != target_rfq_id:
            logger.info(f"Skipping maker stream event {event_type} for RFQ#{event_rfq_id}")
            continue

        if event_type == "quote_update":
            quote_update = data
            status = (data.status or "").lower()
            if status == "accepted":
                print(
                    "   ✅ Quote update received: "
                    f"status={data.status} "
                    f"executed_qty={data.executed_quantity or data.quantity} "
                    f"executed_margin={data.executed_margin or data.margin}"
                )
            else:
                print(
                    "   ✅ Quote update received: "
                    f"status={data.status} "
                    f"qty={data.quantity} "
                    f"margin={data.margin}"
                )
        elif event_type == "settlement_update":
            settlement_update = data
            print(
                f"   ✅ Settlement update received: cid={data.cid} "
                f"height={data.height}"
            )
            for q in data.quotes:
                if q.status == "accepted":
                    print(
                        f"      quote: maker={q.maker} price={q.price} "
                        f"status={q.status} "
                        f"executed_qty={q.executed_quantity} "
                        f"executed_margin={q.executed_margin}"
                    )
                else:
                    print(f"      quote: maker={q.maker} price={q.price} status={q.status}")
        elif event_type == "error":
            raise RuntimeError(f"Maker stream error: {data.code}: {data.message_}")

        if quote_update and settlement_update:
            return quote_update, settlement_update

    raise TimeoutError(
        f"Timed out waiting for quote_update and settlement_update for RFQ#{target_rfq_id}"
    )


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
    price_fetcher = PriceFetcher(config)
    mark_price = await price_fetcher.get_price(market)
    maker_quote_price = (mark_price * Decimal("1.01")).quantize(Decimal("0.000000000000000001"))
    worst_price = (mark_price * Decimal("1.05")).quantize(Decimal("0.000000000000000001"))

    print("=" * 60)
    print("RFQ FULL SETTLEMENT TEST (TESTNET)")
    print("=" * 60)
    print(f"🏦 MM:       {mm_wallet.inj_address}")
    print(f"👤 Retail:   {retail_wallet.inj_address}")
    print(f"📊 Market:   {market.symbol}")
    print(f"⛓️  Chain:    {chain_id}")
    print(f"📜 Contract: {contract_address}")
    print(f"💹 Mark:     {mark_price}")
    print(f"💬 Quote:    {maker_quote_price}")
    print(f"🛡️  Worst:    {worst_price}")
    print("=" * 60)

    # ─── PHASE 1: WebSocket round-trip ───
    print("\n📡 PHASE 1: WebSocket Round-Trip")
    print("-" * 40)

    mm_client = MakerStreamClient(
        config.indexer.ws_endpoint,
        maker_address=mm_wallet.inj_address,
        subscribe_to_quotes_updates=True,
        subscribe_to_settlement_updates=True,
        timeout=10.0,
    )
    await mm_client.connect()
    print("   ✅ MM connected to MakerStream (quote + settlement updates subscribed)")

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

    quantity = "1"
    margin = "10"
    client_id = str(uuid.uuid4())
    expiry_ms = int(time.time() * 1000) + 300_000

    request_data = {
        "request_address": retail_wallet.inj_address,
        "client_id": client_id,
        "market_id": market.id,
        "direction": "long",
        "margin": margin,
        "quantity": quantity,
        "worst_price": str(worst_price),
        "expiry": {"ts": expiry_ms},
    }

    print(f"\n   📤 Retail sending request (client_id={client_id})...")
    request_ack = await retail_client.send_request(
        request_data,
        wait_for_response=True,
        response_timeout=10.0,
    )
    if not request_ack or request_ack.get("type") != "ack" or not request_ack.get("rfq_id"):
        print(f"   ❌ No request ACK received: {request_ack}")
        await mm_client.close()
        await retail_client.close()
        return

    rfq_id = int(request_ack["rfq_id"])
    print(f"   📬 Request ACK received: RFQ#{rfq_id} status={request_ack['status']}")

    # Run MM and retail concurrently; MM filters for our rfq_id
    mm_task = asyncio.create_task(
        mm_wait_and_quote(
            mm_client,
            mm_wallet,
            chain_id,
            contract_address,
            rfq_id,
            maker_quote_price,
        )
    )

    print(f"   ⏳ Retail collecting quotes (45s window)...")
    quotes = await retail_client.collect_quotes(rfq_id=rfq_id, timeout=45, min_quotes=1)

    sent_quote = await mm_task

    if not quotes or not sent_quote:
        print("   ❌ No quotes received. Aborting.")
        await mm_client.close()
        await retail_client.close()
        return

    matching_quotes = [quote for quote in quotes if quote["maker"] == mm_wallet.inj_address]
    if not matching_quotes:
        print(f"   ❌ Retail got {len(quotes)} quote(s), but none from MM {mm_wallet.inj_address}")
        await mm_client.close()
        await retail_client.close()
        return

    best_quote = matching_quotes[0]
    print(f"\n   ✅ Retail got {len(quotes)} quote(s), using MM quote")
    print(f"      Price: {best_quote['price']}")
    print(f"      Maker: {best_quote['maker']}")

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
    settlement_cid = f"tc-cli-{uuid.uuid4()}"
    print(f"      CID:       {settlement_cid}")

    try:
        tx_hash = await contract_client.accept_quote(
            private_key=retail_pk,
            quotes=[contract_quote],
            rfq_id=str(rfq_id),
            market_id=market.id,
            direction=Direction.LONG,
            margin=Decimal(margin),
            quantity=Decimal(quantity),
            worst_price=worst_price,
            unfilled_action={"market": {}},
            cid=settlement_cid,
        )
        print(f"\n   🎉 SETTLEMENT SUCCESSFUL!")
        print(f"   📜 TX Hash: {tx_hash}")
        print(f"   🔗 https://testnet.explorer.injective.network/transaction/{tx_hash}")

        quote_update, settlement_update = await mm_wait_for_post_settlement_updates(
            mm_client,
            rfq_id,
            timeout=60.0,
        )
        print(f"   📬 Final quote status: {quote_update.status}")
        print(f"   📬 Settlement CID: {settlement_update.cid}")
        if settlement_update.cid != settlement_cid:
            print(
                f"   ⚠️  Settlement CID mismatch: expected {settlement_cid}, "
                f"got {settlement_update.cid}"
            )
    except Exception as e:
        print(f"\n   ❌ SETTLEMENT FAILED: {e}")
        logger.exception("Settlement error:")
    finally:
        await mm_client.close()
        await retail_client.close()
        print("   📡 WS closed")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
