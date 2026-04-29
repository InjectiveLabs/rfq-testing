"""
RFQ Market Maker Main Flow (gRPC)

!!! v1 SIGNING — NEEDS PORT TO v2 (EIP-712) !!!
The indexer requires `sign_mode` ("v1" or "v2") on every quote as of
2026-04-29. The inline `to_sign_quote` + `sign_quote` below build the
legacy v1 raw-JSON payload. The rfq-testing standard is v2 (EIP-712).
Canonical v2 reference: src/rfq_test/crypto/eip712.py +
PYTHON_BUILDING_GUIDE.md.

To port: replace those two helpers with
    from rfq_test.crypto.eip712 import sign_quote_v2
and add `quote.sign_mode = "v2"` before sending.

Uses native gRPC MakerStream (bidirectional) instead of WebSocket.

Flow:
0. MM has already granted permissions to RFQ contract (see setup.py)
1. MM connects to gRPC MakerStream with maker metadata
2. MM receives RFQ requests from the stream
3. MM builds and signs quotes
4. MM sends quotes back via the same stream
5. MM receives quote_ack, quote_update, settlement_update events

Prerequisites:
  pip install grpcio grpcio-tools eth-keys eth-utils python-dotenv bech32
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import dotenv
from eth_utils import keccak
from bech32 import bech32_encode, convertbits
from eth_keys import keys

# Add src to path so we can import the generated proto stubs
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "rfq_test" / "proto"))

import grpc
import grpc.aio

from rfq_test.proto.injective_rfq_rpc_pb2 import (
    MakerStreamStreamingRequest,
    RFQExpiryType,
    RFQQuoteType,
)
from rfq_test.proto.injective_rfq_rpc_pb2_grpc import InjectiveRfqRPCStub

dotenv.load_dotenv()

PING_INTERVAL = 10.0

# Sentinel for closing the stream
_STREAM_CLOSE = object()


def must_env(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"{key} not set")
    return v


def trim_0x(s: str) -> str:
    return s[2:] if s.startswith("0x") else s


def eth_address_from_private_key(pk_hex: str) -> str:
    pk_bytes = bytes.fromhex(trim_0x(pk_hex))
    priv = keys.PrivateKey(pk_bytes)
    return priv.public_key.to_checksum_address()


def eth_to_inj_address(eth_addr: str) -> str:
    raw = bytes.fromhex(eth_addr[2:])
    five_bit = convertbits(raw, 8, 5)
    return bech32_encode("inj", five_bit)


def to_sign_quote(
    chain_id: str,
    contract_address: str,
    market_id: str,
    rfq_id: int,
    taker: str,
    taker_direction: str,
    taker_margin: str,
    taker_quantity: str,
    maker: str,
    price: str,
    expiry_ms: int,
    maker_subaccount_nonce: int = 0,
    min_fill_quantity: Optional[str] = None,
) -> dict:
    """Build the compact sign payload matching the contract's SignQuote struct."""
    payload = {
        "c": chain_id,
        "ca": contract_address,
        "mi": market_id,
        "id": rfq_id,
        "t": taker,
        "td": taker_direction,
        "tm": taker_margin,
        "tq": taker_quantity,
        "m": maker,
        "ms": maker_subaccount_nonce,
        "mq": taker_quantity,
        "mm": taker_margin,
        "p": price,
        "e": {"ts": expiry_ms},
    }
    if min_fill_quantity is not None:
        payload["mfq"] = min_fill_quantity
    return payload


def sign_quote(sign_obj: dict, mm_pk_hex: str) -> str:
    """Sign keccak256(JSON(payload)) with secp256k1. Returns 0x-prefixed hex."""
    payload = json.dumps(sign_obj, separators=(",", ":"), ensure_ascii=False)
    msg_hash = keccak(text=payload)

    pk_bytes = bytes.fromhex(trim_0x(mm_pk_hex))
    priv_key = keys.PrivateKey(pk_bytes)
    signature = priv_key.sign_msg_hash(msg_hash)

    v = signature.v
    if v < 27:
        v += 27

    sig_bytes = (
        signature.r.to_bytes(32, "big")
        + signature.s.to_bytes(32, "big")
        + bytes([v])
    )
    return "0x" + sig_bytes.hex()


async def _request_iter(send_queue: asyncio.Queue):
    """Async generator that yields gRPC request messages from a queue."""
    while True:
        msg = await send_queue.get()
        if msg is None or msg is _STREAM_CLOSE:
            return
        yield msg


async def ping_loop(send_queue: asyncio.Queue, interval: float = PING_INTERVAL):
    """Periodically enqueue pings to keep the stream alive."""
    while True:
        try:
            await asyncio.sleep(interval)
            await send_queue.put(
                MakerStreamStreamingRequest(message_type="ping")
            )
        except asyncio.CancelledError:
            break


async def send_quote(
    send_queue: asyncio.Queue,
    request,
    price: float,
    maker_addr: str,
    mm_pk: str,
    chain_id: str,
    contract_address: str,
):
    """Build, sign, and enqueue a quote for the given request."""
    expiry_ms = int(time.time() * 1000) + 20_000
    price_str = f"{price:.1f}"

    sign_obj = to_sign_quote(
        chain_id=chain_id,
        contract_address=contract_address,
        market_id=request.market_id,
        rfq_id=int(request.rfq_id),
        taker=request.request_address,
        taker_direction=request.direction,
        taker_margin=request.margin,
        taker_quantity=request.quantity,
        maker=maker_addr,
        price=price_str,
        expiry_ms=expiry_ms,
    )
    sig = sign_quote(sign_obj, mm_pk)

    quote = RFQQuoteType(
        chain_id=chain_id,
        contract_address=contract_address,
        rfq_id=int(request.rfq_id),
        market_id=request.market_id,
        taker_direction=request.direction,
        margin=request.margin,
        quantity=request.quantity,
        price=price_str,
        expiry=RFQExpiryType(timestamp=expiry_ms),
        maker=maker_addr,
        taker=request.request_address,
        signature=sig,
        maker_subaccount_nonce=0,
    )

    print(f"\n📤 Sending quote (price={price_str})")
    await send_queue.put(
        MakerStreamStreamingRequest(message_type="quote", quote=quote)
    )


async def main():
    grpc_endpoint = must_env("GRPC_ENDPOINT")
    mm_pk = must_env("MM_PRIVATE_KEY")
    contract_address = must_env("CONTRACT_ADDRESS")
    chain_id = must_env("CHAIN_ID")

    eth_addr = eth_address_from_private_key(mm_pk)
    maker_addr = eth_to_inj_address(eth_addr)

    print("🔌 Connecting to gRPC MakerStream...")
    print(f"   Endpoint: {grpc_endpoint}")
    print(f"   Maker:    {maker_addr}")

    # Connect — use SSL for remote endpoints, insecure for localhost
    if grpc_endpoint.startswith("localhost") or grpc_endpoint.startswith("127."):
        channel = grpc.aio.insecure_channel(grpc_endpoint)
    else:
        channel = grpc.aio.secure_channel(
            grpc_endpoint, grpc.ssl_channel_credentials()
        )

    stub = InjectiveRfqRPCStub(channel)

    maker_metadata = (
        ("maker_address", maker_addr),
        ("subscribe_to_quotes_updates", "true"),
        ("subscribe_to_settlement_updates", "true"),
    )

    send_queue: asyncio.Queue = asyncio.Queue()

    # Pre-queue initial ping (first message carries data alongside HEADERS)
    await send_queue.put(MakerStreamStreamingRequest(message_type="ping"))

    stream = stub.MakerStream(
        _request_iter(send_queue), metadata=maker_metadata
    )

    # Start background ping loop
    pinger = asyncio.create_task(ping_loop(send_queue))

    print("📡 MM connected — listening for RFQ requests...\n")

    try:
        while True:
            resp = await stream.read()
            if resp == grpc.aio.EOF:
                print("🔌 MakerStream EOF")
                break

            msg_type = resp.message_type

            if msg_type == "pong":
                continue

            elif msg_type == "request":
                req = resp.request
                print(
                    f"\n📩 RFQ request: RFQ#{req.rfq_id} market={req.market_id}"
                )
                print(
                    f"   direction={req.direction} margin={req.margin} "
                    f"qty={req.quantity}"
                )

                # Demo pricing ladder
                for p in [1.3, 1.4, 1.5]:
                    await send_quote(
                        send_queue,
                        req,
                        p,
                        maker_addr,
                        mm_pk,
                        chain_id,
                        contract_address,
                    )

            elif msg_type == "quote_ack":
                ack = resp.quote_ack
                print(
                    f"📬 Quote ACK: rfq_id={ack.rfq_id} status={ack.status}"
                )

            elif msg_type == "quote_update":
                qu = resp.processed_quote
                print(
                    f"📊 Quote update: rfq_id={qu.rfq_id} status={qu.status}"
                )

            elif msg_type == "settlement_update":
                s = resp.settlement
                print(f"⚖️  Settlement: rfq_id={s.rfq_id} cid={s.cid}")
                for q in s.quotes:
                    print(
                        f"   quote: maker={q.maker} price={q.price} "
                        f"status={q.status}"
                    )

            elif msg_type == "error":
                e = resp.error
                print(f"❌ Stream error: {e.code}: {e.message_}")

            else:
                print(f"Unknown message type: {msg_type}")

    except grpc.aio.AioRpcError as e:
        print(f"❌ MakerStream gRPC error: {e.code()}: {e.details()}")
    except asyncio.CancelledError:
        pass
    finally:
        pinger.cancel()
        await send_queue.put(_STREAM_CLOSE)
        await channel.close()
        print("🔌 gRPC channel closed")


if __name__ == "__main__":
    asyncio.run(main())
