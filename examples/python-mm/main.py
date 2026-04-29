'''
 * RFQ Market Maker Main Flow (v2 EIP-712 signing)
 *
 * Standalone reference. The signing primitive is inlined so partners can
 * vendor this file without depending on rfq_test.
 *
 * v2 binds chainId + verifyingContract via the EIP-712 domain separator.
 * Wire payloads MUST carry `sign_mode: "v2"` — empty values are rejected
 * with `value of message.sign_mode must be one of "v1", "v2"`. The full
 * spec lives in PYTHON_BUILDING_GUIDE.md and on rfq.inj.so/onboarding.html#sign.
 *
 * Flow:
 * 0. MM has already granted permissions to RFQ contract
 * 1. MM connects to WebSocket & subscribes to RFQ requests
 * 2. MM receives RFQ request from retail
 * 3. MM builds quotes, for blind quote, MM will choose nonce
 * 4. MM signs quotes (see example)
 * 5. MM sends quotes back via WebSocket
'''
import asyncio
import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

import dotenv
import websockets
from eth_utils import keccak
from bech32 import bech32_encode, bech32_decode, convertbits
from eth_keys import keys

dotenv.load_dotenv()

WS_URL = "ws://localhost:4464/ws"
COMETBFT_WS_URL = "ws://localhost:26657/websocket"

def must_env(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"{key} not set")
    return v

CONTRACT_ADDRESS = must_env("CONTRACT_ADDRESS")
CHAIN_ID         = must_env("CHAIN_ID")
EVM_CHAIN_ID     = int(os.getenv("EVM_CHAIN_ID", "1439"))   # 1439 testnet, 1776 mainnet
INJUSDT_MARKET_ID = "0x7cc8b10d7deb61e744ef83bdec2bbcf4a056867e89b062c6a453020ca82bd4e4"

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

def trim_0x(s: str) -> str:
    return s[2:] if s.startswith("0x") else s

def bech32_to_evm(addr: str) -> bytes:
    """`inj1...` bech32 address → 20 raw bytes (the EVM form)."""
    hrp, data = bech32_decode(addr)
    if hrp != "inj" or data is None:
        raise ValueError(f"bad inj address: {addr!r}")
    raw = convertbits(data, 5, 8, False)
    if raw is None or len(raw) != 20:
        raise ValueError(f"expected 20-byte address, got {len(raw) if raw else 0}")
    return bytes(raw)

def _u(n: int, width: int) -> bytes:
    """Right-aligned big-endian int in 32 bytes."""
    return b"\x00" * (32 - width) + n.to_bytes(width, "big")
def _s(s: str) -> bytes:    return keccak(text=s)
def _addr(b20: bytes) -> bytes: return b"\x00" * 12 + b20

def domain_separator() -> bytes:
    return keccak(primitive=(
        keccak(primitive=EIP712_DOMAIN_TYPE)
        + _s("RFQ") + _s("1")
        + _u(EVM_CHAIN_ID, 8)
        + _addr(bech32_to_evm(CONTRACT_ADDRESS))
    ))

def sign_quote_v2(
    *,
    private_key: str,
    market_id: str,
    rfq_id: int,
    taker_inj: str,
    direction: str,                     # "long" or "short"
    taker_margin: str, taker_quantity: str,
    maker_inj: str,    maker_subaccount_nonce: int,
    maker_quantity: str, maker_margin: str,
    price: str,
    expiry_ms: Optional[int] = None,
    expiry_height: Optional[int] = None,
    min_fill_quantity: Optional[str] = None,
) -> str:
    """Sign a quote with EIP-712 v2. Returns "0x"+r||s||v hex."""
    if (expiry_ms is None) == (expiry_height is None):
        raise ValueError("provide exactly one of expiry_ms or expiry_height")
    expiry_kind, expiry_value = (0, expiry_ms) if expiry_ms is not None else (1, expiry_height)
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
        _u(expiry_kind, 1), _u(int(expiry_value), 8),
        _s(mfq),
        _u(1, 1),                                          # bindingKind = 1
    ))
    digest = keccak(primitive=b"\x19\x01" + domain_separator() + keccak(primitive=msg))

    pk = keys.PrivateKey(bytes.fromhex(trim_0x(private_key)))
    sig = pk.sign_msg_hash(digest)
    v = sig.v + 27 if sig.v < 27 else sig.v
    return "0x" + (sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([v])).hex()

def eth_address_from_private_key(pk_hex: str) -> str:
    pk_bytes = bytes.fromhex(pk_hex[2:] if pk_hex.startswith("0x") else pk_hex)
    priv = keys.PrivateKey(pk_bytes)

    # checksummed Ethereum address
    return priv.public_key.to_checksum_address()

def eth_to_inj_address(eth_addr: str) -> str:
    # eth addr hex -> inj bech32 (same as Go ethsecp256k1)
    raw = bytes.fromhex(eth_addr[2:])
    five_bit = convertbits(raw, 8, 5)
    return bech32_encode("inj", five_bit)


@dataclass
class Expiry:
    ts: int = 0
    h: int = 0

@dataclass
class RfqRequest:
    rfq_id: int
    market_id: str
    margin: str
    quantity: str
    worst_price: str
    request_address: str
    expiry: int
    created_at: int
    updated_at: int
    event_time: int
    transaction_time: int
    direction: int = 0 # 0 = long (default), 1 = short

@dataclass
class Quote:
    market_id: str
    rfq_id: int
    direction: int
    margin: str
    quantity: str
    price: str
    expiry: Expiry
    chain_id: str
    contract_address: str
    maker: Optional[str] = None
    taker: Optional[str] = None
    signature: Optional[str] = None
    nonce: Optional[int] = None
    maker_subaccount_nonce: int = 0
    min_fill_quantity: Optional[str] = None

async def send_quote(
    ws,
    req: Optional[RfqRequest],
    price: float,
    maker_addr: str,
    mm_pk: str,
    expiry: Expiry,
    maker_subaccount_nonce: int = 0,
    min_fill_quantity: Optional[str] = None,
):
    
    if req is None:
        # Blind quotes (no live RFQ) bind via a maker-chosen nonce. v2 EIP-712
        # requires a real taker address in the digest; this example only ports
        # the live-RFQ path. Blind-quote signing for v2 is not yet supported.
        raise NotImplementedError(
            "Blind-quote v2 signing not implemented in this example — "
            "use the live-RFQ branch (req != None) or consult the indexer "
            "reference for the blind path."
        )

    quote = Quote(
        rfq_id=req.rfq_id,
        market_id=req.market_id,
        direction=(1 if req.direction == 0 else 0),
        margin=req.margin,
        quantity=str(int(float(req.quantity) / 2)),
        price=f"{price:.1f}",
        expiry=expiry,
        maker=maker_addr,
        taker=req.request_address,
        chain_id=CHAIN_ID,
        contract_address=CONTRACT_ADDRESS,
        nonce=0,
        maker_subaccount_nonce=maker_subaccount_nonce,
        min_fill_quantity=min_fill_quantity,
    )

    # v2 EIP-712 — pass exactly one of expiry_ms / expiry_height
    sig = sign_quote_v2(
        private_key=mm_pk,
        market_id=quote.market_id,
        rfq_id=quote.rfq_id,
        taker_inj=quote.taker,
        direction="long" if quote.direction == 0 else "short",
        taker_margin=req.margin,
        taker_quantity=req.quantity,
        maker_inj=quote.maker,
        maker_subaccount_nonce=quote.maker_subaccount_nonce,
        maker_quantity=quote.quantity,
        maker_margin=quote.margin,
        price=quote.price,
        expiry_ms=expiry.ts if expiry.ts > 0 else None,
        expiry_height=expiry.h if expiry.h > 0 else None,
        min_fill_quantity=quote.min_fill_quantity,
    )
    quote.signature = sig
    quote_dict = asdict(quote)
    quote_dict["sign_mode"] = "v2"           # required by indexer
    msg = {
        "jsonrpc": "2.0",
        "method": "quote",
        "id": int(time.time() * 1000),
        "params": {
            "quote": quote_dict,
        },
    }

    print('msg:', msg)
    print("\n📤 Sending quote")
    print(json.dumps(msg, indent=2))
    await ws.send(json.dumps(msg))

async def quote_loop(mm_addr, mm_pk):
    RFQ_STREAM_ID = 1
    async with websockets.connect(WS_URL) as ws:
        print("🔌 MM WebSocket connected")

        sub = {
            "jsonrpc": "2.0",
            "method": "subscribe",
            "id": RFQ_STREAM_ID,
            "params": {
                "query": {
                    "stream": "request",
                    "market_ids": [INJUSDT_MARKET_ID],
                }
            },
        }

        await ws.send(json.dumps(sub))
        print("📡 Subscribed to RFQ request stream")

        async for raw in ws:
            msg = json.loads(raw)
            if msg['id'] == RFQ_STREAM_ID:
                if msg["result"] == "subscribed":
                    continue

                req_data = msg.get("result", {}).get("request")
                if not req_data:
                    continue

                req = RfqRequest(**req_data)

                print("\n📩 RFQ request received")
                print(f"Current block: {latest_block_height}")
                print(req)

                # RFQ supports expiry by timestamp OR height, not both
                expiry_ts = Expiry(ts=int((time.time() + 8)*1000))
                expiry_height = Expiry(h=latest_block_height + 10)

                msn = 0  # TODO: fetch maker subaccount nonce from chain
                await send_quote(ws, req, 1.2, mm_addr, mm_pk, expiry=expiry_ts, maker_subaccount_nonce=msn)
                await send_quote(ws, req, 1.4, mm_addr, mm_pk, expiry=expiry_height, maker_subaccount_nonce=msn)
                await send_quote(ws, None, 1.3, mm_addr, mm_pk, expiry=expiry_ts, maker_subaccount_nonce=msn)
            else:
                print('response msgs:', msg)

# for latency sensitive strategy, it's best to listen for block changes in case MM wants to set expiry block height
async def block_sync_loop():
    global latest_block_height
    while True:
        try:
            async with websockets.connect(COMETBFT_WS_URL) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "method": "subscribe",
                    "id": 1,
                    "params": {"query": "tm.event='NewBlockHeader'"}
                }))
                await ws.recv()
                print("📦 Subscribed to NewBlockHeader events")

                async for message in ws:
                    data = json.loads(message)
                    header = data.get("result", {}).get("data", {}).get("value", {}).get("header", {})
                    if header is None:
                        continue
                    latest_block_height = int(header["height"])

        except (websockets.ConnectionClosed, ConnectionRefusedError) as e:
            print(f"📦 Block listener lost connection: {e}, reconnecting in 3s...")
            await asyncio.sleep(3)
    
async def main():
    mm_pk = must_env("MM_PRIVATE_KEY")
    addr = eth_address_from_private_key(mm_pk)
    mm_addr = eth_to_inj_address(addr)
    await asyncio.gather(
        block_sync_loop(),
        quote_loop(mm_addr, mm_pk),
    )
if __name__ == "__main__":
    asyncio.run(main())
