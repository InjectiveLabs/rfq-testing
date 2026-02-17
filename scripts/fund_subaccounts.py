#!/usr/bin/env python3
"""
Fund subaccounts for RFQ testing.

This script deposits INJ from bank accounts to exchange subaccounts
so that wallets can participate in derivative trading.

Usage:
    python scripts/fund_subaccounts.py [--env ENV] [--count COUNT] [--amount AMOUNT]
    
Examples:
    python scripts/fund_subaccounts.py --env testnet --count 10 --amount 1000
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv

from rfq_test.config import load_environment_config
from rfq_test.crypto.wallet import generate_wallets_from_seed

from pyinjective.composer_v2 import Composer
from pyinjective.core.broadcaster import MsgBroadcasterWithPk
from pyinjective.core.network import Network
from bech32 import bech32_decode, convertbits


def get_subaccount_id(address: str, nonce: int = 0) -> str:
    """Get subaccount ID from address and nonce."""
    _, data = bech32_decode(address)
    if data is None:
        raise ValueError(f"Invalid address: {address}")
    
    decoded = convertbits(data, 5, 8, False)
    if decoded is None:
        raise ValueError(f"Failed to decode address: {address}")
    
    address_hex = bytes(decoded).hex()
    nonce_hex = format(nonce, '024x')
    
    return "0x" + address_hex + nonce_hex


async def deposit_to_subaccount(
    network: Network,
    private_key: str,
    sender_address: str,
    amount: str,
    denom: str = "inj",
) -> str:
    """Deposit funds to exchange subaccount."""
    subaccount_id = get_subaccount_id(sender_address)
    
    broadcaster = MsgBroadcasterWithPk.new_using_simulation(
        network=network,
        private_key=private_key,
    )
    
    composer = Composer(network=network.string())
    
    # Create deposit message
    msg = composer.msg_deposit(
        sender=sender_address,
        subaccount_id=subaccount_id,
        amount=amount,
        denom=denom,
    )
    
    result = await broadcaster.broadcast([msg])
    
    # Check for transaction failure (code != 0 means error)
    tx_response = result.txResponse if hasattr(result, 'txResponse') else result
    code = getattr(tx_response, 'code', 0)
    if code != 0:
        raw_log = getattr(tx_response, 'rawLog', '') or getattr(tx_response, 'raw_log', '') or ''
        raise Exception(f"Deposit failed with code {code}: {raw_log}")
    
    return result.txhash


async def main():
    parser = argparse.ArgumentParser(description="Fund subaccounts for RFQ testing")
    parser.add_argument("--env", default="testnet", help="Environment (default: testnet)")
    parser.add_argument("--count", type=int, default=10, help="Number of wallets to fund (default: 10)")
    parser.add_argument("--amount", type=float, default=1000, help="Amount of INJ to deposit (default: 1000)")
    parser.add_argument("--mm-only", action="store_true", help="Only fund MM wallets")
    parser.add_argument("--retail-only", action="store_true", help="Only fund retail wallets")
    args = parser.parse_args()
    
    load_dotenv()
    
    env_name = args.env.upper()
    mm_seed = os.getenv(f"{env_name}_LOAD_TEST_MM_SEED_PHRASE")
    retail_seed = os.getenv(f"{env_name}_LOAD_TEST_RETAIL_SEED_PHRASE")
    
    if not mm_seed and not args.retail_only:
        print(f"Error: {env_name}_LOAD_TEST_MM_SEED_PHRASE not set in .env")
        sys.exit(1)
    if not retail_seed and not args.mm_only:
        print(f"Error: {env_name}_LOAD_TEST_RETAIL_SEED_PHRASE not set in .env")
        sys.exit(1)
    
    config = load_environment_config(args.env)
    
    # Convert amount to smallest unit (1 INJ = 10^18)
    amount_in_wei = str(int(args.amount * 1e18))
    
    print(f"\n{'='*60}")
    print(f"Funding Subaccounts - {args.env}")
    print(f"{'='*60}")
    print(f"Amount per wallet: {args.amount} INJ")
    print(f"Wallets to fund: {args.count} each (MM and Retail)")
    print()
    
    # Create network (use optional exchange/explorer/stream from config when set)
    grpc_main = config.chain.grpc_endpoint
    grpc_exchange = getattr(config.chain, "grpc_exchange_endpoint", None) or grpc_main
    grpc_explorer = getattr(config.chain, "grpc_explorer_endpoint", None) or grpc_main
    chain_stream = getattr(config.chain, "chain_stream_endpoint", None) or grpc_main
    network = Network.custom(
        lcd_endpoint=config.chain.lcd_endpoint,
        tm_websocket_endpoint="",
        grpc_endpoint=grpc_main,
        grpc_exchange_endpoint=grpc_exchange,
        grpc_explorer_endpoint=grpc_explorer,
        chain_id=config.chain.chain_id,
        env="testnet",
        chain_stream_endpoint=chain_stream,
        official_tokens_list_url="",
    )
    
    # Fund MM wallets
    if not args.retail_only and mm_seed:
        print(f"\n--- MM Wallets ({args.count}) ---")
        mm_wallets = generate_wallets_from_seed(mm_seed, count=args.count)
        
        for i, wallet in enumerate(mm_wallets):
            try:
                tx_hash = await deposit_to_subaccount(
                    network=network,
                    private_key=wallet.private_key,
                    sender_address=wallet.inj_address,
                    amount=amount_in_wei,
                )
                print(f"  [MM {i}] {wallet.inj_address}: Deposited {args.amount} INJ - TX: {tx_hash[:16]}...")
            except Exception as e:
                print(f"  [MM {i}] {wallet.inj_address}: FAILED - {str(e)[:80]}")
            
            await asyncio.sleep(0.5)
    
    # Fund Retail wallets
    if not args.mm_only and retail_seed:
        print(f"\n--- Retail Wallets ({args.count}) ---")
        retail_wallets = generate_wallets_from_seed(retail_seed, count=args.count)
        
        for i, wallet in enumerate(retail_wallets):
            try:
                tx_hash = await deposit_to_subaccount(
                    network=network,
                    private_key=wallet.private_key,
                    sender_address=wallet.inj_address,
                    amount=amount_in_wei,
                )
                print(f"  [Retail {i}] {wallet.inj_address}: Deposited {args.amount} INJ - TX: {tx_hash[:16]}...")
            except Exception as e:
                print(f"  [Retail {i}] {wallet.inj_address}: FAILED - {str(e)[:80]}")
            
            await asyncio.sleep(0.5)
    
    print(f"\n{'='*60}")
    print("Subaccount funding complete!")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
