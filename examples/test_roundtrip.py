"""Quick test: Retail sends request, MM responds with quote on testnet.
v2: Added quote ACK checking and verbose debug logging.
"""
import asyncio
import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

import dotenv
dotenv.load_dotenv()

from rfq_test.config import get_environment_config
from rfq_test.crypto.wallet import Wallet
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient
from rfq_test.crypto.signing import sign_quote
from rfq_test.models.types import Direction

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
    print(f"📊 Market: {market.symbol}")
    print(f"⛓️  Chain: {chain_id}")
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
    rfq_id = int(time.time() * 1000)
    expiry_ms = rfq_id + 300_000  # 5 min
    request_data = {
        "request_address": retail_wallet.inj_address,
        "rfq_id": rfq_id,
        "market_id": market.id,
        "direction": "long",
        "margin": "100",
        "quantity": "10",
        "worst_price": "100",
        "expiry": expiry_ms,
    }
    print(f"\n📤 Retail sending request (RFQ#{rfq_id})...")
    await retail_client.send_request(request_data)
    print("   Request sent!")

    # Step 4: MM waits for request
    print("\n⏳ MM waiting for request...")
    try:
        received = await mm_client.wait_for_request(timeout=10)
        print(f"   ✅ MM received: RFQ#{received['rfq_id']}")
        print(f"   Direction: {received['direction']}, Qty: {received['quantity']}, Margin: {received['margin']}")
        print(f"   Taker: {received.get('taker') or received.get('request_address', 'unknown')}")
    except Exception as e:
        print(f"   ❌ MM did not receive request: {e}")
        await mm_client.close()
        await retail_client.close()
        return

    # Step 5: MM builds and sends quote (with ACK wait)
    taker = received.get("taker") or received.get("request_address", "")
    expiry = int(time.time() * 1000) + 20_000

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
        price="1.5",
        expiry=expiry,
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
        "price": "1.5",
        "expiry": expiry,
        "maker": mm_wallet.inj_address,
        "taker": taker,
        "signature": signature,
    }

    print(f"\n📤 MM sending quote (price=1.5, expiry={expiry})...")
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
