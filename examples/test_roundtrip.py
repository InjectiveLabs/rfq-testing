"""Quick test: Retail sends request, MM responds with quote on testnet.
v2: Added quote ACK checking and verbose debug logging.
"""
import asyncio
import logging
import os
import sys
import time
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

import dotenv
dotenv.load_dotenv()

from rfq_test.config import get_environment_config
from rfq_test.crypto.wallet import Wallet
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient
from rfq_test.crypto.eip712 import sign_quote_v2
from rfq_test.models.types import Direction
from rfq_test.utils.price import PriceFetcher, quantize_to_tick

# Verbose logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("rfq_test_manual")

async def main():
    # Load testnet config
    os.environ.setdefault("RFQ_ENV", "testnet")
    config = get_environment_config()
    
    # MM wallet (your whitelisted key)
    mm_pk = os.getenv("TESTNET_MM_PRIVATE_KEY")
    if not mm_pk:
        print("❌ Set TESTNET_MM_PRIVATE_KEY in .env")
        return
    mm_wallet = Wallet.from_private_key(mm_pk)
    print(f"🏦 MM Address: {mm_wallet.inj_address}")

    # Random retail wallet
    retail_wallet = Wallet.generate()
    print(f"👤 Retail Address: {retail_wallet.inj_address}")
    
    market = config.default_market
    chain_id, contract_address = config.signing_context
    evm_chain_id, _ = config.signing_context_v2
    price_fetcher = PriceFetcher(config)
    mark_price = await price_fetcher.get_price(market)
    price_tick = price_fetcher.get_price_tick(market)
    quote_price = quantize_to_tick(mark_price * Decimal("1.01"), price_tick, rounding=ROUND_FLOOR)
    worst_price = quantize_to_tick(mark_price * Decimal("1.05"), price_tick, rounding=ROUND_CEILING)
    print(f"📊 Market: {market.symbol}")
    print(f"⛓️  Chain: {chain_id} (EIP-712 EVM chainId={evm_chain_id})")
    print(f"📜 Contract: {contract_address}\n")

    # Step 1: Connect MM to MakerStream
    mm_client = MakerStreamClient(
        config.indexer.ws_endpoint,
        maker_address=mm_wallet.inj_address,
        timeout=10.0,
    )
    await mm_client.connect()
    print("✅ MM connected to MakerStream")

    # Step 2: Connect Retail to TakerStream
    retail_client = TakerStreamClient(
        config.indexer.ws_endpoint,
        request_address=retail_wallet.inj_address,
        timeout=10.0,
    )
    await retail_client.connect()
    print("✅ Retail connected to TakerStream")

    # Let connections stabilize
    await asyncio.sleep(2)

    # Step 3: Retail sends RFQ request
    client_id = str(uuid.uuid4())
    expiry_ms = int(time.time() * 1000) + 300_000  # 5 min
    request_data = {
        "request_address": retail_wallet.inj_address,
        "client_id": client_id,
        "market_id": market.id,
        "direction": "long",
        "margin": "100",
        "quantity": "10",
        "worst_price": worst_price,
        "expiry": {"ts": expiry_ms},
    }
    print(f"\n📤 Retail sending request (client_id={client_id})...")
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

    # Step 4: MM waits for request
    print("\n⏳ MM waiting for request...")
    received = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            candidate = await mm_client.wait_for_request(timeout=2)
        except Exception:
            continue
        if int(candidate["rfq_id"]) != rfq_id:
            logger.info("Skipping unrelated RFQ#%s", candidate["rfq_id"])
            continue
        received = candidate
        break

    if received is None:
        print(f"   ❌ MM did not receive RFQ#{rfq_id}")
        await mm_client.close()
        await retail_client.close()
        return

    print(f"   ✅ MM received: RFQ#{received['rfq_id']}")
    print(f"   Direction: {received['direction']}, Qty: {received['quantity']}, Margin: {received['margin']}")
    print(f"   Taker: {received.get('taker') or received.get('request_address', 'unknown')}")

    # Step 5: MM builds and sends quote (with ACK wait)
    taker = received.get("taker") or received.get("request_address", "")
    expiry = int(time.time() * 1000) + 20_000
    maker_subaccount_nonce = 0
    min_fill_quantity = None

    signature = sign_quote_v2(
        private_key=mm_wallet.private_key,
        evm_chain_id=evm_chain_id,
        verifying_contract_bech32=contract_address,
        market_id=received["market_id"],
        rfq_id=int(received["rfq_id"]),
        taker=taker,
        direction=received["direction"],
        taker_margin=received["margin"],
        taker_quantity=received["quantity"],
        maker=mm_wallet.inj_address,
        maker_margin=received["margin"],
        maker_quantity=received["quantity"],
        price=quote_price,
        expiry_ms=expiry,
        maker_subaccount_nonce=maker_subaccount_nonce,
        min_fill_quantity=min_fill_quantity,
    )

    quote_data = {
        "chain_id": chain_id,
        "contract_address": contract_address,
        "rfq_id": received["rfq_id"],
        "market_id": received["market_id"],
        "taker_direction": received["direction"],
        "margin": received["margin"],
        "quantity": received["quantity"],
        "price": quote_price,
        "expiry": expiry,
        "maker": mm_wallet.inj_address,
        "taker": taker,
        "signature": signature,
        "sign_mode": "v2",
        "evm_chain_id": evm_chain_id,
        "maker_subaccount_nonce": maker_subaccount_nonce,
        "min_fill_quantity": min_fill_quantity,
    }

    print(f"\n📤 MM sending quote (price={quote_price}, expiry={expiry})...")
    # Send quote AND wait for ACK/error
    response = await mm_client.send_quote(quote_data, wait_for_response=True, response_timeout=5.0)
    print(f"   📬 Indexer response: {response}")

    # Step 6: Retail waits for quote
    print(f"\n⏳ Retail waiting for quote (rfq_id={rfq_id})...")
    try:
        quotes = await retail_client.collect_quotes(rfq_id=rfq_id, timeout=10, min_quotes=1)
        print(f"   ✅ Received {len(quotes)} quote(s)")
        for q in quotes:
            print(f"   Quote: price={q['price']}, maker={q['maker']}, sig={q['signature'][:20]}...")
    except Exception as e:
        print(f"   ❌ Error: {e}")

    # Also drain any remaining messages on retail side
    print("\n📥 Draining retail message queue...")
    while True:
        event = await retail_client.get_next_event(timeout=2.0)
        if event is None:
            break
        print(f"   Event: type={event[0]}, data={event[1]}")

    print("\n🏁 Done!")
    await mm_client.close()
    await retail_client.close()

if __name__ == "__main__":
    asyncio.run(main())
