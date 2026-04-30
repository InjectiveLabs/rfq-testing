"""
RFQ Market Maker Main Flow (gRPC, v2 EIP-712 signing)

Standalone reference. The signing primitive is inlined so partners can
vendor this file without depending on rfq_test.

Wire payloads MUST carry `sign_mode: "v2"` — empty values are rejected
with `value of message.sign_mode must be one of "v1", "v2"`. Full spec:
PYTHON_BUILDING_GUIDE.md and rfq.inj.so/onboarding.html#sign.

Uses native gRPC MakerStream (bidirectional) instead of WebSocket.

Flow:
0. MM has already granted permissions to RFQ contract (see setup.py)
1. MM connects to gRPC MakerStream with maker metadata
2. MM receives RFQ requests from the stream
3. MM builds and signs quotes (v2 EIP-712, see sign_quote_v2 below)
4. MM sends quotes back via the same stream with sign_mode="v2"
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
from bech32 import bech32_encode, bech32_decode, convertbits
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


EVM_CHAIN_ID = int(os.getenv("EVM_CHAIN_ID", "1439"))   # 1439 testnet, 1776 mainnet


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


# --- EIP-712 v2 signing primitives (mirrors the indexer reference) -------------

EIP712_DOMAIN_TYPE = (
    b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
SIGN_QUOTE_TYPE = (
    b"SignQuote(string marketId,uint64 rfqId,address taker,uint8 takerDirection,"
    b"string takerMargin,string takerQuantity,address maker,"
    b"uint32 makerSubaccountNonce,string makerQuantity,string makerMargin,"
    b"string price,uint8 expiryKind,uint64 expiryValue,string minFillQuantity,"
    b"uint8 bindingKind)"
)


def bech32_to_evm(addr: str) -> bytes:
    """`inj1...` bech32 → 20 raw bytes (EVM form)."""
    hrp, data = bech32_decode(addr)
    if hrp != "inj" or data is None:
        raise ValueError(f"bad inj address: {addr!r}")
    raw = convertbits(data, 5, 8, False)
    if raw is None or len(raw) != 20:
        raise ValueError(f"expected 20-byte address, got {len(raw) if raw else 0}")
    return bytes(raw)


def _u(n: int, width: int) -> bytes:
    return b"\x00" * (32 - width) + n.to_bytes(width, "big")
def _s(s: str) -> bytes:        return keccak(text=s)
def _addr(b20: bytes) -> bytes: return b"\x00" * 12 + b20


def _domain_separator(contract_address: str) -> bytes:
    return keccak(primitive=(
        keccak(primitive=EIP712_DOMAIN_TYPE)
        + _s("RFQ") + _s("1")
        + _u(EVM_CHAIN_ID, 8)
        + _addr(bech32_to_evm(contract_address))
    ))


def sign_quote_v2(
    *,
    private_key: str,
    contract_address: str,
    market_id: str,
    rfq_id: int,
    taker_inj: str,
    direction: str,                     # "long" or "short"
    taker_margin: str, taker_quantity: str,
    maker_inj: str, maker_subaccount_nonce: int,
    maker_quantity: str, maker_margin: str,
    price: str,
    expiry_ms: int,
    min_fill_quantity: Optional[str] = None,
) -> str:
    """Sign a quote with EIP-712 v2 (timestamp-expiry only)."""
    direction_byte = 0 if direction.lower() == "long" else 1
    mfq = "0" if min_fill_quantity is None else min_fill_quantity
    msg = b"".join((
        keccak(primitive=SIGN_QUOTE_TYPE),
        _s(market_id), _u(int(rfq_id), 8),
        _addr(bech32_to_evm(taker_inj)), _u(direction_byte, 1),
        _s(taker_margin), _s(taker_quantity),
        _addr(bech32_to_evm(maker_inj)), _u(int(maker_subaccount_nonce), 4),
        _s(maker_quantity), _s(maker_margin),
        _s(price),
        _u(0, 1), _u(int(expiry_ms), 8),                   # expiryKind=timestamp
        _s(mfq),
        _u(1, 1),                                          # bindingKind = 1 (taker-specific)
    ))
    digest = keccak(primitive=b"\x19\x01" + _domain_separator(contract_address) + keccak(primitive=msg))
    pk = keys.PrivateKey(bytes.fromhex(trim_0x(private_key)))
    sig = pk.sign_msg_hash(digest)
    v = sig.v - 27 if sig.v >= 27 else sig.v
    return "0x" + (sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([v])).hex()


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
    """Build, sign, and enqueue a quote for the given request (v2 EIP-712)."""
    expiry_ms = int(time.time() * 1000) + 20_000
    price_str = f"{price:.1f}"

    sig = sign_quote_v2(
        private_key=mm_pk,
        contract_address=contract_address,
        market_id=request.market_id,
        rfq_id=int(request.rfq_id),
        taker_inj=request.request_address,
        direction=request.direction,
        taker_margin=request.margin,
        taker_quantity=request.quantity,
        maker_inj=maker_addr,
        maker_subaccount_nonce=0,
        maker_quantity=request.quantity,
        maker_margin=request.margin,
        price=price_str,
        expiry_ms=expiry_ms,
    )

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
        sign_mode="v2",                      # required by indexer
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
