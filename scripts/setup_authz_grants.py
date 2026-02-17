#!/usr/bin/env python3
"""
Setup authz grants for MM and retail wallets.

This script grants the necessary authz permissions for the first N wallets
to interact with the RFQ smart contract.

Required grants:
- MM: MsgSend, MsgPrivilegedExecuteContract
- Retail: MsgSend, MsgPrivilegedExecuteContract, MsgBatchUpdateOrders, MsgCreateDerivativeMarketOrder

Usage:
    python scripts/setup_authz_grants.py [--env ENV] [--count COUNT]
    
Examples:
    python scripts/setup_authz_grants.py --env testnet --count 10
"""

# Suppress gRPC noise before any imports
import os
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GRPC_TRACE"] = ""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv

from rfq_test.config import load_environment_config
from rfq_test.crypto.wallet import generate_wallets_from_seed
from rfq_test.clients.chain import ChainClient
from rfq_test.utils.setup import (
    MM_AUTHZ_GRANTS,
    RETAIL_AUTHZ_GRANTS,
    setup_authz_grants,
)


async def grant_authz_for_wallet(
    chain_client: ChainClient,
    wallet,
    contract_address: str,
    msg_types: list[str],
    wallet_type: str,
    index: int,
) -> None:
    """Grant authz permissions for a single wallet using shared setup."""
    try:
        await setup_authz_grants(chain_client, wallet, contract_address, msg_types)
        print(f"  [{wallet_type} {index}] Grants completed for {wallet.inj_address[:24]}...")
    except Exception as e:
        print(f"  [{wallet_type} {index}] FAILED - {e}")
        raise


async def main():
    parser = argparse.ArgumentParser(description="Setup authz grants for RFQ wallets")
    parser.add_argument("--env", default="testnet", help="Environment name (default: testnet)")
    parser.add_argument("--count", type=int, default=10, help="Number of wallets to setup (default: 10)")
    parser.add_argument("--mm-only", action="store_true", help="Only setup MM wallets")
    parser.add_argument("--retail-only", action="store_true", help="Only setup retail wallets")
    args = parser.parse_args()
    
    # Load environment
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
    
    # Load config
    config = load_environment_config(args.env)
    contract_address = config.contract.address
    
    print(f"\n{'='*60}")
    print(f"Setting up authz grants for {args.env}")
    print(f"{'='*60}")
    print(f"Contract: {contract_address}")
    print(f"Wallets:  {args.count} each (MM and Retail)")
    print()
    
    # Create chain client
    chain_client = ChainClient(config.chain)
    
    # Setup MM wallets
    if not args.retail_only and mm_seed:
        print(f"\n--- MM Wallets ({args.count}) ---")
        print(f"Grants per wallet: {', '.join(g.split('.')[-1] for g in MM_AUTHZ_GRANTS)}")
        print()
        
        mm_wallets = generate_wallets_from_seed(mm_seed, count=args.count)
        
        for i, wallet in enumerate(mm_wallets):
            print(f"MM Wallet {i}: {wallet.inj_address}")
            await grant_authz_for_wallet(
                chain_client, wallet, contract_address, MM_AUTHZ_GRANTS, "MM", i
            )
            print()
    
    # Setup Retail wallets
    if not args.mm_only and retail_seed:
        print(f"\n--- Retail Wallets ({args.count}) ---")
        print(f"Grants per wallet: {', '.join(g.split('.')[-1] for g in RETAIL_AUTHZ_GRANTS)}")
        print()
        
        retail_wallets = generate_wallets_from_seed(retail_seed, count=args.count)
        
        for i, wallet in enumerate(retail_wallets):
            print(f"Retail Wallet {i}: {wallet.inj_address}")
            await grant_authz_for_wallet(
                chain_client, wallet, contract_address, RETAIL_AUTHZ_GRANTS, "Retail", i
            )
            print()
    
    print(f"\n{'='*60}")
    print("Authz grant setup complete!")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
