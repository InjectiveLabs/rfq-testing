"""Full E2E test: Retail sends request → MM quotes → Retail accepts on-chain.

gRPC version: uses native gRPC APIs directly (no WebSocket).

The taker side avoids TakerStream metadata dependencies by creating RFQs via
the unary Request RPC and receiving quotes via StreamQuote. MakerStream
remains bidirectional because maker-scoped subscriptions still rely on stream
metadata.
"""
import asyncio
import logging
import os
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
# Required so the generated grpc stub can do `import injective_rfq_rpc_pb2`
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "rfq_test" / "proto"))

import dotenv
dotenv.load_dotenv()

import grpc
import grpc.aio

from rfq_test.config import get_settings, get_environment_config
from rfq_test.crypto.wallet import Wallet
from rfq_test.proto.injective_rfq_rpc_pb2 import (
    MakerStreamStreamingRequest,
    RFQRequestInputType,
    RFQExpiryType,
    RFQQuoteType,
    RequestRequest,
    StreamQuoteRequest,
)
from rfq_test.proto.injective_rfq_rpc_pb2_grpc import InjectiveRfqRPCStub
from rfq_test.clients.contract import ContractClient
from rfq_test.crypto.eip712 import sign_quote_v2
from rfq_test.models.types import Direction
from rfq_test.utils.price import PriceFetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("rfq_settlement_grpc_test")

PING_INTERVAL = 10.0

# Sentinel pushed into a send-queue to close the iterator / stream.
_STREAM_CLOSE = object()


def get_grpc_endpoint(config) -> str:
    """Return the native gRPC endpoint (host:port) from indexer config."""
    endpoint = config.indexer.grpc_endpoint
    if not endpoint:
        raise ValueError("indexer.grpc_endpoint not configured in testnet.yaml")
    return endpoint


async def _request_iter(send_queue: asyncio.Queue):
    """Async generator that yields gRPC request messages from a queue.

    Sending None (or _STREAM_CLOSE) into the queue closes the stream.
    """
    while True:
        msg = await send_queue.get()
        if msg is None or msg is _STREAM_CLOSE:
            return
        yield msg


async def read_maker_stream(call, queue: asyncio.Queue) -> None:
    """Background task: read MakerStream responses and dispatch typed events to queue."""
    try:
        while True:
            resp = await call.read()
            if resp == grpc.aio.EOF:
                logger.info("MakerStream EOF")
                break
            msg_type = resp.message_type
            if msg_type == "pong":
                pass
            elif msg_type == "request":
                logger.info(f"Maker: request RFQ#{resp.request.rfq_id}")
                await queue.put(("request", resp.request))
            elif msg_type == "quote_ack":
                logger.debug(f"Maker: quote_ack RFQ#{resp.quote_ack.rfq_id} status={resp.quote_ack.status}")
                await queue.put(("quote_ack", resp.quote_ack))
            elif msg_type == "quote_update":
                logger.info(f"Maker: quote_update RFQ#{resp.processed_quote.rfq_id} status={resp.processed_quote.status}")
                await queue.put(("quote_update", resp.processed_quote))
            elif msg_type == "settlement_update":
                logger.info(f"Maker: settlement_update RFQ#{resp.settlement.rfq_id} cid={resp.settlement.cid}")
                await queue.put(("settlement_update", resp.settlement))
            elif msg_type == "error":
                logger.error(f"Maker: error {resp.error.code}: {resp.error.message_}")
                await queue.put(("error", resp.error))
            else:
                logger.warning(f"Maker: unknown msg_type={msg_type!r}")
    except grpc.aio.AioRpcError as e:
        logger.error(f"MakerStream gRPC error: {e.code()}: {e.details()}")
        await queue.put(("error", e))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"MakerStream read loop error: {e}")


async def read_quote_stream(call, queue: asyncio.Queue) -> None:
    """Background task: read StreamQuote responses and dispatch quotes to queue."""
    try:
        async for resp in call:
            quote = resp.quote
            logger.info(
                "QuoteStream: rfq_id=%s price=%s from %s op=%s",
                quote.rfq_id,
                quote.price,
                quote.maker,
                resp.stream_operation,
            )
            await queue.put(("quote", quote))
    except grpc.aio.AioRpcError as e:
        logger.error(f"StreamQuote gRPC error: {e.code()}: {e.details()}")
        await queue.put(("error", e))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"StreamQuote read loop error: {e}")


async def ping_loop(send_queue: asyncio.Queue, make_ping, interval: float = PING_INTERVAL) -> None:
    """Periodically enqueue application-level pings to keep the stream alive."""
    while True:
        try:
            await asyncio.sleep(interval)
            await send_queue.put(make_ping(message_type="ping"))
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Ping error: {e}")
            break


async def drain_stale_requests(queue: asyncio.Queue) -> None:
    """Drain any stale requests already queued (non-blocking)."""
    count = 0
    while not queue.empty():
        try:
            event_type, data = queue.get_nowait()
            if event_type == "request":
                logger.info(f"Drained stale request: RFQ#{data.rfq_id}")
                count += 1
        except Exception:
            break
    if count:
        print(f"   🧹 Drained {count} stale request(s)")


async def mm_wait_and_quote(
    maker_send_q: asyncio.Queue,
    maker_recv_q: asyncio.Queue,
    mm_wallet,
    chain_id: str,
    contract_address: str,
    evm_chain_id: int,
    target_rfq_id: int,
    quote_price: Decimal,
) -> dict | None:
    """MM: wait for OUR request (by rfq_id), then sign and enqueue quote."""
    print(f"   ⏳ MM waiting for RFQ#{target_rfq_id}...")

    start = time.monotonic()
    received = None
    while (time.monotonic() - start) < 45:
        try:
            event_type, data = await asyncio.wait_for(maker_recv_q.get(), timeout=5.0)
            if event_type == "request":
                if int(data.rfq_id) == target_rfq_id:
                    received = data
                    break
                else:
                    logger.info(f"Skipping other request: RFQ#{data.rfq_id}")
            elif event_type == "error":
                raise RuntimeError(f"Maker stream error: {data}")
            else:
                await maker_recv_q.put((event_type, data))
        except asyncio.TimeoutError:
            continue

    if not received:
        print(f"   ❌ MM never received RFQ#{target_rfq_id}")
        return None

    print(f"   ✅ MM received RFQ#{received.rfq_id}")

    taker = received.request_address
    quote_expiry = int(time.time() * 1000) + 60_000
    maker_subaccount_nonce = 0
    min_fill_quantity = None

    signature = sign_quote_v2(
        private_key=mm_wallet.private_key,
        evm_chain_id=evm_chain_id,
        verifying_contract_bech32=contract_address,
        market_id=received.market_id,
        rfq_id=int(received.rfq_id),
        taker=taker,
        direction="long",
        taker_margin=received.margin,
        taker_quantity=received.quantity,
        maker=mm_wallet.inj_address,
        maker_margin=received.margin,
        maker_quantity=received.quantity,
        price=str(quote_price),
        expiry_ms=quote_expiry,
        maker_subaccount_nonce=maker_subaccount_nonce,
        min_fill_quantity=min_fill_quantity,
    )

    quote_kwargs = {
        "chain_id": chain_id,
        "contract_address": contract_address,
        "market_id": received.market_id,
        "rfq_id": received.rfq_id,
        "taker_direction": "long",
        "margin": received.margin,
        "quantity": received.quantity,
        "price": str(quote_price),
        "expiry": RFQExpiryType(timestamp=quote_expiry),
        "maker": mm_wallet.inj_address,
        "taker": taker,
        "signature": signature,
        "sign_mode": "v2",
        "maker_subaccount_nonce": maker_subaccount_nonce,
    }
    if min_fill_quantity is not None:
        quote_kwargs["min_fill_quantity"] = min_fill_quantity

    quote = RFQQuoteType(
        **quote_kwargs,
    )

    print(f"   📤 MM sending quote (price={quote_price})...")
    await maker_send_q.put(MakerStreamStreamingRequest(message_type="quote", quote=quote))

    # Wait for quote_ack
    ack_start = time.monotonic()
    while (time.monotonic() - ack_start) < 10.0:
        try:
            event_type, data = await asyncio.wait_for(maker_recv_q.get(), timeout=2.0)
            if event_type == "quote_ack":
                print(f"   📬 Indexer ACK: rfq_id={data.rfq_id} status={data.status}")
                break
            elif event_type == "error":
                raise RuntimeError(f"Maker stream error on quote ACK: {data}")
            else:
                await maker_recv_q.put((event_type, data))
        except asyncio.TimeoutError:
            continue

    return {
        "chain_id": chain_id,
        "contract_address": contract_address,
        "rfq_id": received.rfq_id,
        "market_id": received.market_id,
        "taker_direction": "long",
        "margin": received.margin,
        "quantity": received.quantity,
        "price": str(quote_price),
        "expiry": quote_expiry,
        "maker": mm_wallet.inj_address,
        "taker": taker,
        "signature": signature,
        "sign_mode": "v2",
        "maker_subaccount_nonce": maker_subaccount_nonce,
        "min_fill_quantity": min_fill_quantity,
    }


async def collect_quotes(
    quote_recv_q: asyncio.Queue,
    rfq_id: int,
    timeout: float = 45.0,
    min_quotes: int = 1,
) -> list[dict]:
    """Collect incoming quotes for a specific RFQ from the quote receive queue."""
    quotes = []
    start = time.monotonic()

    def _proto_to_dict(data) -> dict:
        expiry_ts = data.expiry.timestamp if data.HasField("expiry") else 0
        return {
            "rfq_id": str(data.rfq_id),
            "market_id": data.market_id,
            "taker_direction": data.taker_direction,
            "margin": data.margin,
            "quantity": data.quantity,
            "price": data.price,
            "expiry": expiry_ts,
            "maker": data.maker,
            "taker": data.taker,
            "signature": data.signature,
            "status": data.status,
            "maker_subaccount_nonce": data.maker_subaccount_nonce,
            "min_fill_quantity": data.min_fill_quantity,
        }

    while (time.monotonic() - start) < timeout:
        try:
            remaining = timeout - (time.monotonic() - start)
            event_type, data = await asyncio.wait_for(
                quote_recv_q.get(), timeout=min(remaining, 1.0)
            )
            if event_type == "quote" and int(data.rfq_id) == rfq_id:
                quotes.append(_proto_to_dict(data))
                if len(quotes) >= min_quotes:
                    # Give a short window for any additional quotes
                    await asyncio.sleep(0.5)
                    while not quote_recv_q.empty():
                        try:
                            et, d = quote_recv_q.get_nowait()
                            if et == "quote" and int(d.rfq_id) == rfq_id:
                                quotes.append(_proto_to_dict(d))
                        except asyncio.QueueEmpty:
                            break
                    break
            elif event_type == "error":
                if isinstance(data, grpc.aio.AioRpcError):
                    raise RuntimeError(f"Quote stream error: {data.code()}: {data.details()}") from data
                raise RuntimeError(f"Quote stream error: {data}")
            else:
                await quote_recv_q.put((event_type, data))
                await asyncio.sleep(0)
        except asyncio.TimeoutError:
            if quotes:
                break
            continue

    return quotes


async def mm_wait_for_post_settlement_updates(
    maker_recv_q: asyncio.Queue,
    target_rfq_id: int,
    timeout: float = 60.0,
):
    """Wait for quote_update and settlement_update for the target RFQ."""
    print(f"   ⏳ MM waiting for quote_update + settlement_update for RFQ#{target_rfq_id}...")
    start = time.monotonic()
    quote_update = None
    settlement_update = None

    while (time.monotonic() - start) < timeout:
        try:
            event_type, data = await asyncio.wait_for(maker_recv_q.get(), timeout=2.0)
        except asyncio.TimeoutError:
            continue

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
    evm_chain_id, _ = config.signing_context_v2
    price_fetcher = PriceFetcher(config)
    mark_price = await price_fetcher.get_price(market)
    maker_quote_price = (mark_price * Decimal("1.01")).quantize(Decimal("0.000000000000000001"))
    worst_price = (mark_price * Decimal("1.05")).quantize(Decimal("0.000000000000000001"))

    grpc_endpoint = get_grpc_endpoint(config)

    print("=" * 60)
    print("RFQ FULL SETTLEMENT TEST (TESTNET) — gRPC")
    print("=" * 60)
    print(f"🏦 MM:       {mm_wallet.inj_address}")
    print(f"👤 Retail:   {retail_wallet.inj_address}")
    print(f"📊 Market:   {market.symbol}")
    print(f"⛓️  Chain:    {chain_id}")
    print(f"📜 Contract: {contract_address}")
    print(f"💹 Mark:     {mark_price}")
    print(f"💬 Quote:    {maker_quote_price}")
    print(f"🛡️  Worst:    {worst_price}")
    print(f"🔌 gRPC:     {grpc_endpoint}")
    print("=" * 60)

    # ─── PHASE 1: gRPC Stream Round-Trip ───
    print("\n📡 PHASE 1: gRPC Stream Round-Trip")
    print("-" * 40)

    channel = grpc.aio.secure_channel(grpc_endpoint, grpc.ssl_channel_credentials())
    stub = InjectiveRfqRPCStub(channel)

    maker_metadata = (
        ("maker_address", mm_wallet.inj_address),
        ("subscribe_to_quotes_updates", "true"),
        ("subscribe_to_settlement_updates", "true"),
    )
    print(f"   [debug] maker metadata: {maker_metadata}")

    # Per-stream queues: _send for outbound messages, _recv for dispatched events.
    quote_recv_q: asyncio.Queue = asyncio.Queue()
    maker_send_q: asyncio.Queue = asyncio.Queue()
    maker_recv_q: asyncio.Queue = asyncio.Queue()

    # Pre-queue the initial ping so the first yielded maker message carries
    # data alongside the HEADERS frame.
    await maker_send_q.put(MakerStreamStreamingRequest(message_type="ping"))

    quote_call = stub.StreamQuote(
        StreamQuoteRequest(
            addresses=[retail_wallet.inj_address],
            market_ids=[market.id],
        )
    )
    maker_call = stub.MakerStream(_request_iter(maker_send_q), metadata=maker_metadata)

    # Start background reader tasks
    maker_reader = asyncio.create_task(read_maker_stream(maker_call, maker_recv_q))
    quote_reader = asyncio.create_task(read_quote_stream(quote_call, quote_recv_q))

    # Keep MakerStream alive for maker-scoped request/update delivery.
    maker_pinger = asyncio.create_task(ping_loop(maker_send_q, MakerStreamStreamingRequest))

    print("   ✅ MM connected to MakerStream (quote + settlement updates subscribed)")
    print("   ✅ Retail subscribed to StreamQuote")

    # Drain stale messages from live testnet traffic
    await asyncio.sleep(3)
    await drain_stale_requests(maker_recv_q)

    quantity = "1"
    margin = "10"
    client_id = str(uuid.uuid4())
    expiry_ms = int(time.time() * 1000) + 300_000

    create_rfq = RFQRequestInputType(
        client_id=client_id,
        market_id=market.id,
        direction="long",
        margin=margin,
        quantity=quantity,
        worst_price=str(worst_price),
        request_address=retail_wallet.inj_address,
        expiry=expiry_ms,
    )

    print(f"\n   📤 Retail sending request (client_id={client_id})...")
    try:
        request_resp = await stub.Request(RequestRequest(request=create_rfq))
        rfq_id = int(request_resp.rfq_id)
        print(f"   📬 Request ACK received: RFQ#{rfq_id} status={request_resp.status}")
    except grpc.aio.AioRpcError as e:
        print(f"   ❌ Request RPC failed: {e.code()}: {e.details()}")
        maker_pinger.cancel()
        maker_reader.cancel()
        quote_reader.cancel()
        await channel.close()
        return

    # Run MM and retail concurrently
    mm_task = asyncio.create_task(
        mm_wait_and_quote(
            maker_send_q,
            maker_recv_q,
            mm_wallet,
            chain_id,
            contract_address,
            evm_chain_id,
            rfq_id,
            maker_quote_price,
        )
    )

    print(f"   ⏳ Retail collecting quotes (45s window)...")
    quotes = await collect_quotes(quote_recv_q, rfq_id, timeout=45, min_quotes=1)

    sent_quote = await mm_task

    if not quotes or not sent_quote:
        print("   ❌ No quotes received. Aborting.")
        maker_pinger.cancel()
        maker_reader.cancel()
        quote_reader.cancel()
        await channel.close()
        return

    matching_quotes = [q for q in quotes if q["maker"] == mm_wallet.inj_address]
    if not matching_quotes:
        print(f"   ❌ Retail got {len(quotes)} quote(s), but none from MM {mm_wallet.inj_address}")
        maker_pinger.cancel()
        maker_reader.cancel()
        quote_reader.cancel()
        await channel.close()
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

    settlement_cid = f"tc-cli-{uuid.uuid4()}"
    print(f"   📝 Submitting AcceptQuote...")
    print(f"      RFQ ID:    {rfq_id}")
    print(f"      Direction: LONG")
    print(f"      Margin:    {margin}, Qty: {quantity}")
    print(f"      Price:     {best_quote['price']}")
    print(f"      Maker:     {best_quote['maker']}")
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
            maker_recv_q,
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
        maker_pinger.cancel()
        # Signal stream iterators to close
        await maker_send_q.put(_STREAM_CLOSE)
        maker_reader.cancel()
        quote_reader.cancel()
        await channel.close()
        print("   📡 gRPC channel closed")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
