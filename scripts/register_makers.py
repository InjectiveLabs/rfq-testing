#!/usr/bin/env python3
"""Register market maker addresses on the RFQ contract.

This script whitelists MM wallet addresses so they can submit quotes.

Usage:
    # Whitelist first 10 MM wallets (indices 0-9)
    python scripts/register_makers.py
    
    # Whitelist specific range
    python scripts/register_makers.py --start 0 --count 100
    
    # Whitelist specific indicesclear
    python scripts/register_makers.py --indices 3,4,5,7,9
    
    # Dry run (show what would be registered)
    python scripts/register_makers.py --dry-run

Requires:
    - RFQ_ENV environment variable (e.g., testnet)
    - {ENV}_ADMIN_PRIVATE_KEY in .env
    - {ENV}_LOAD_TEST_MM_SEED_PHRASE in .env
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Reduce gRPC log noise (C++ layer writes to stderr; must set before importing pyinjective)
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GRPC_TRACE", "")

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from rfq_test.clients.contract import ContractClient
from rfq_test.config import get_settings, get_environment_config
from rfq_test.crypto.wallet import generate_wallets_from_seed
from rfq_test.exceptions import ContractValidationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def get_current_makers(contract_client: ContractClient) -> set[str]:
    """Query currently whitelisted makers."""
    try:
        return await contract_client.query_makers()
    except Exception as e:
        logger.error(f"Failed to query makers: {e}")
        return set()


async def register_maker(
    contract_client: ContractClient,
    admin_private_key: str,
    maker_address: str,
    index: int,
) -> tuple[bool, str]:
    """Register a single maker. Returns (success, message)."""
    try:
        tx_hash = await contract_client.register_maker(
            private_key=admin_private_key,
            maker_address=maker_address,
        )
        return True, f"TX: {tx_hash}"
    except ContractValidationError as e:
        if "already registered" in str(e).lower():
            return True, "Already registered"
        return False, str(e)
    except Exception as e:
        return False, str(e)


async def main(
    start_index: int = 0,
    count: int = 10,
    specific_indices: list[int] | None = None,
    dry_run: bool = False,
    delay_seconds: float = 3.0,
):
    """Main function to register makers."""
    settings = get_settings()
    env_config = get_environment_config()
    
    logger.info(f"Environment: {settings.rfq_env}")
    logger.info(f"Contract: {env_config.contract.address}")
    
    # Check required credentials
    if not settings.admin_private_key:
        logger.error("ADMIN_PRIVATE_KEY not set!")
        logger.error(f"Set {settings.rfq_env.upper()}_ADMIN_PRIVATE_KEY in .env")
        return 1
    
    if not settings.load_test_mm_seed_phrase:
        logger.error("LOAD_TEST_MM_SEED_PHRASE not set!")
        logger.error(f"Set {settings.rfq_env.upper()}_LOAD_TEST_MM_SEED_PHRASE in .env")
        return 1
    
    # Determine which indices to whitelist
    if specific_indices:
        indices = specific_indices
    else:
        indices = list(range(start_index, start_index + count))
    
    logger.info(f"Will process {len(indices)} MM wallets: indices {indices[0]}-{indices[-1]}")
    
    # Generate wallets
    max_index = max(indices) + 1
    all_wallets = generate_wallets_from_seed(
        settings.load_test_mm_seed_phrase,
        count=max_index,
        start_index=0,
    )
    
    # Filter to requested indices
    wallets_to_process = [(i, all_wallets[i]) for i in indices]
    
    # Create contract client
    contract_client = ContractClient(env_config.contract, env_config.chain)
    
    # Query current whitelist
    logger.info("Querying current whitelist...")
    current_makers = await get_current_makers(contract_client)
    logger.info(f"Currently whitelisted: {len(current_makers)} makers")
    
    # Determine which need registration
    to_register = []
    already_registered = []
    
    for idx, wallet in wallets_to_process:
        if wallet.inj_address in current_makers:
            already_registered.append((idx, wallet.inj_address))
        else:
            to_register.append((idx, wallet))
    
    logger.info(f"Already registered: {len(already_registered)}")
    logger.info(f"Need to register:   {len(to_register)}")
    
    if dry_run:
        logger.info("\n=== DRY RUN - No changes will be made ===\n")
        
        if already_registered:
            logger.info("Already whitelisted:")
            for idx, addr in already_registered:
                logger.info(f"  ✓ Index {idx}: {addr}")
        
        if to_register:
            logger.info("\nWould register:")
            for idx, wallet in to_register:
                logger.info(f"  → Index {idx}: {wallet.inj_address}")
        
        return 0
    
    # Register makers; track addresses the contract says are "already registered"
    # (they may not appear in list_makers due to a known contract query quirk)
    registered = []
    failed = []
    contract_says_registered: set[str] = set()

    for idx, wallet in to_register:
        logger.info(f"Registering index {idx}: {wallet.inj_address}")

        success, message = await register_maker(
            contract_client,
            settings.admin_private_key,
            wallet.inj_address,
            idx,
        )

        if success:
            registered.append((idx, wallet.inj_address, message))
            if "already registered" in message.lower():
                contract_says_registered.add(wallet.inj_address)
            logger.info(f"  ✓ {message}")
        else:
            failed.append((idx, wallet.inj_address, message))
            logger.error(f"  ✗ {message}")

        # Wait between registrations to avoid sequence mismatch
        if to_register.index((idx, wallet)) < len(to_register) - 1:
            await asyncio.sleep(delay_seconds)

    # Summary
    print("\n" + "=" * 60)
    print("REGISTRATION SUMMARY")
    print("=" * 60)
    print(f"Already registered: {len(already_registered)}")
    print(f"Newly registered:   {len(registered)}")
    print(f"Failed:             {len(failed)}")

    if failed:
        print("\nFailed registrations:")
        for idx, addr, error in failed:
            print(f"  Index {idx} ({addr}): {error}")
        return 1

    # Verify final state (list_makers query)
    logger.info("\nVerifying final whitelist...")
    final_makers = await get_current_makers(contract_client)

    verified_in_list = 0
    not_in_list = []
    only_contract_reported = []

    for idx, wallet in wallets_to_process:
        if wallet.inj_address in final_makers:
            verified_in_list += 1
        else:
            not_in_list.append((idx, wallet.inj_address))
            if wallet.inj_address in contract_says_registered:
                only_contract_reported.append((idx, wallet.inj_address))

    print(f"\nVerified in list_makers: {verified_in_list}/{len(wallets_to_process)}")

    if only_contract_reported:
        print(f"\nNote: {len(only_contract_reported)} maker(s) are reported as 'already registered' by the contract")
        print("      but do not appear in list_makers (known contract/list_makers quirk).")
        print("      They are treated as whitelisted for this script.")
        for idx, addr in only_contract_reported:
            print(f"        Index {idx}: {addr}")

    truly_missing = [(idx, addr) for idx, addr in not_in_list if addr not in contract_says_registered]
    if truly_missing:
        print("\nNOT in whitelist (check failed):")
        for idx, addr in truly_missing:
            print(f"  Index {idx}: {addr}")
        return 1

    total_ok = verified_in_list + len(only_contract_reported)
    if total_ok == len(wallets_to_process):
        print("\n✓ All requested MM wallets are whitelisted (or contract reports them registered).")
    else:
        print(f"\n⚠ Verified or contract-reported: {total_ok}/{len(wallets_to_process)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Register market maker addresses on the RFQ contract",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Whitelist indices 0-9
    python scripts/register_makers.py
    
    # Whitelist indices 0-99
    python scripts/register_makers.py --count 100
    
    # Whitelist indices 10-19
    python scripts/register_makers.py --start 10 --count 10
    
    # Whitelist specific indices
    python scripts/register_makers.py --indices 3,4,5,7,9
    
    # Dry run to see what would be registered
    python scripts/register_makers.py --dry-run --count 100
        """,
    )
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Starting wallet index (default: 0)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=10,
        help="Number of wallets to register (default: 10)",
    )
    parser.add_argument(
        "--indices",
        type=str,
        help="Comma-separated list of specific indices to register (overrides --start/--count)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be registered without making changes",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Delay between registrations in seconds (default: 3.0)",
    )
    
    args = parser.parse_args()
    
    # Parse specific indices if provided
    specific_indices = None
    if args.indices:
        specific_indices = [int(i.strip()) for i in args.indices.split(",")]
    
    exit_code = asyncio.run(
        main(
            start_index=args.start,
            count=args.count,
            specific_indices=specific_indices,
            dry_run=args.dry_run,
            delay_seconds=args.delay,
        )
    )
    sys.exit(exit_code)
