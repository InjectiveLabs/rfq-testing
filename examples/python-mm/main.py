'''
 * RFQ Market Maker Main Flow
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
from bech32 import bech32_encode, convertbits
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
CHAIN_ID = must_env("CHAIN_ID")
INJUSDT_MARKET_ID = "0x7cc8b10d7deb61e744ef83bdec2bbcf4a056867e89b062c6a453020ca82bd4e4"

def trim_0x(s: str) -> str:
    return s[2:] if s.startswith("0x") else s

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

def to_sign_quote(chain_id: str, contract_address: str, req: Optional[RfqRequest], q: Quote) -> dict:
    # specify either type of expiry (timestamp / block_height, not both)
    expiry = {}
    if q.expiry.h > 0:
        expiry["h"] = q.expiry.h
    elif q.expiry.ts > 0:
        expiry["ts"] = q.expiry.ts

    if req is None:
        payload = {
            "c": chain_id,
            "ca": contract_address,
            "mi": q.market_id,
            "id": q.nonce, # NOTE: this maker's nonce now become the rfq_id for blind quote, since we have no specific taker to initiate id
            "td": ("long" if q.direction == 1 else "short"), # NOTE: taker direction will be oppsite direction
            "m": q.maker,
            "ms": q.maker_subaccount_nonce,
            "mq": q.quantity,
            "mm": q.margin,
            "p": q.price,
            "e": expiry,
        }
        if q.min_fill_quantity is not None:
            payload["mfq"] = q.min_fill_quantity
        return payload

    payload = {
        "c": chain_id,
        "ca": contract_address,
        "mi": q.market_id,
        "id": q.rfq_id,
        "t": req.request_address,
        "td": ("long" if q.direction == 1 else "short"),
        "tm": req.margin,
        "tq": req.quantity,
        "m": q.maker,
        "ms": q.maker_subaccount_nonce,
        "mq": q.quantity,
        "mm": q.margin,
        "p": q.price,
        "e": expiry,
    }
    if q.min_fill_quantity is not None:
        payload["mfq"] = q.min_fill_quantity
    return payload

"""
Sign an RFQ quote using raw secp256k1 (Ethereum-style) signing.
IMPORTANT:
- This signs keccak256(JSON(payload)) directly.
- NO EIP-191 prefix added.
- Signature format is 65 bytes: r(32) || s(32) || v(1)
- v is normalized to 0 / 1 (or 27 / 28) (ethers-style), which Injective expects.
"""
def sign_quote(sign_obj: dict, mm_pk_hex: str) -> str:
    # 1. Canonical JSON (MUST match Go)
    payload = json.dumps(sign_obj, separators=(",", ":"), ensure_ascii=False)
    # 2. Raw keccak256(payload)
    msg_hash = keccak(text=payload)

    # 3. Load private key
    pk_bytes = bytes.fromhex(trim_0x(mm_pk_hex))
    priv_key = keys.PrivateKey(pk_bytes)

    # 4. Sign hash (raw secp256k1, NO prefix)
    signature = priv_key.sign_msg_hash(msg_hash)

    # 5. Ethereum-style v (27 / 28)
    v = signature.v
    if v < 27:
        v += 27

    sig_bytes = (
        signature.r.to_bytes(32, "big") +
        signature.s.to_bytes(32, "big") +
        bytes([v])
    )

    return "0x" + sig_bytes.hex()

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
        # blind nonce is freely chosen between (t-15; t+15), duplicated nonce will be rejected on-chain to prevent relay attack
        nonce = int(time.time() * 1000)
        # direction
        direction = 1 # 0 for long, 1 for short
        # choose margin base on MM risk management
        margin = "100"
        # quantity
        quantity = "5"

        quote = Quote(
            rfq_id=0,
            market_id=INJUSDT_MARKET_ID,
            direction=direction,
            margin=margin,
            quantity=quantity,
            price=f"{price:.1f}",
            expiry=expiry,
            maker=maker_addr,
            taker="",
            chain_id=CHAIN_ID,
            contract_address=CONTRACT_ADDRESS,
            nonce=nonce,
            maker_subaccount_nonce=maker_subaccount_nonce,
            min_fill_quantity=min_fill_quantity,
        )
    else:
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

    sig = sign_quote(to_sign_quote(CHAIN_ID, CONTRACT_ADDRESS, req, quote), mm_pk)
    quote.signature = sig
    msg = {
        "jsonrpc": "2.0",
        "method": "quote",
        "id": int(time.time() * 1000),
        "params": {
            "quote": asdict(quote),
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
