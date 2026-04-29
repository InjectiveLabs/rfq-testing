#!/usr/bin/env python3
"""Sanity-check EIP-712 v2 signing against an indexer-style verifier.

The indexer recovers an `inj1` address from the signature and checks it
against the expected maker / taker. We mirror that verification here so
the round-trip can run without contacting devnet — useful for catching
encoding drift before pushing.

Set RFQ_TEST_MM_PRIVATE_KEY (and optionally RFQ_TEST_MM_INJ for an
explicit expected address). If the env var is unset we generate a fresh
ephemeral key and roundtrip against ourselves.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import bech32
from eth_account import Account

from rfq_test.crypto.eip712 import (
    sign_conditional_order_v2,
    sign_quote_digest,
    sign_quote_v2,
    signed_taker_intent_digest,
)

# Devnet defaults (from Slack #rfq-eng 2026-04-29 and configs/devnet.yaml).
EVM_CHAIN_ID = 1439
CONTRACT_BECH32 = "inj19g43wyj843ydkc845dcdea6su4mgfjwnpjz6h5"
MARKET_ID = (
    "0xdc70164d7120529c3cd84278c98df4151210c0447a65a2aab03459cf328de41e"
)


def eth_to_inj(eth_addr_hex: str) -> str:
    raw = bytes.fromhex(eth_addr_hex.removeprefix("0x"))
    return bech32.bech32_encode("inj", bech32.convertbits(raw, 8, 5))


def recover_inj(digest: bytes, signature_hex: str) -> str:
    sig = bytes.fromhex(signature_hex.removeprefix("0x"))
    if len(sig) != 65:
        raise ValueError(f"signature must be 65 bytes, got {len(sig)}")
    if sig[64] >= 27:
        sig = sig[:64] + bytes([sig[64] - 27])
    eth = Account._recover_hash(digest, signature=sig)
    return eth_to_inj(eth)


def load_or_generate() -> tuple[str, str]:
    pk = os.environ.get("RFQ_TEST_MM_PRIVATE_KEY")
    if pk:
        pk = pk.removeprefix("0x")
        acct = Account.from_key(bytes.fromhex(pk))
        expected = os.environ.get("RFQ_TEST_MM_INJ") or eth_to_inj(acct.address)
        return pk, expected
    acct = Account.create()
    return acct.key.hex(), eth_to_inj(acct.address)


def test_sign_quote_roundtrip(pk_hex: str, expected_inj: str) -> None:
    sig = sign_quote_v2(
        private_key=pk_hex,
        evm_chain_id=EVM_CHAIN_ID,
        verifying_contract_bech32=CONTRACT_BECH32,
        market_id=MARKET_ID,
        rfq_id=42,
        taker=expected_inj,
        direction="long",
        taker_margin="100",
        taker_quantity="1",
        maker=expected_inj,
        maker_margin="100",
        maker_quantity="1",
        price="4.5",
        expiry_ms=1_700_000_000_000,
    )
    digest = sign_quote_digest(
        evm_chain_id=EVM_CHAIN_ID,
        verifying_contract_bech32=CONTRACT_BECH32,
        market_id=MARKET_ID,
        rfq_id=42,
        taker_bech32=expected_inj,
        direction="long",
        taker_margin="100",
        taker_quantity="1",
        maker_bech32=expected_inj,
        maker_subaccount_nonce=0,
        maker_quantity="1",
        maker_margin="100",
        price="4.5",
        expiry_kind=0,
        expiry_value=1_700_000_000_000,
        min_fill_quantity=None,
    )
    recovered = recover_inj(digest, sig)
    assert recovered == expected_inj, f"{recovered} != {expected_inj}"
    print(f"  signature (65B): {sig}")
    print(f"  recovered      : {recovered}")
    print("[OK] SignQuote v2 roundtrip\n")


def test_signed_taker_intent_roundtrip(pk_hex: str, expected_inj: str) -> None:
    sig = sign_conditional_order_v2(
        private_key=pk_hex,
        evm_chain_id=EVM_CHAIN_ID,
        verifying_contract_bech32=CONTRACT_BECH32,
        version=1,
        taker=expected_inj,
        epoch=1,
        rfq_id=12345,
        market_id=MARKET_ID,
        subaccount_nonce=0,
        lane_version=1,
        deadline_ms=1_700_001_000_000,
        direction="short",
        quantity="1",
        margin="0",
        worst_price="132",
        min_total_fill_quantity="1",
        trigger_type="mark_price_gte",
        trigger_price="120",
    )
    digest = signed_taker_intent_digest(
        evm_chain_id=EVM_CHAIN_ID,
        verifying_contract_bech32=CONTRACT_BECH32,
        version=1,
        taker_bech32=expected_inj,
        epoch=1,
        rfq_id=12345,
        market_id=MARKET_ID,
        subaccount_nonce=0,
        lane_version=1,
        deadline_ms=1_700_001_000_000,
        direction="short",
        quantity="1",
        margin="0",
        worst_price="132",
        min_total_fill_quantity="1",
        trigger_type="mark_price_gte",
        trigger_price="120",
    )
    recovered = recover_inj(digest, sig)
    assert recovered == expected_inj, f"{recovered} != {expected_inj}"
    print(f"  signature (65B): {sig}")
    print(f"  recovered      : {recovered}")
    print("[OK] SignedTakerIntent v2 roundtrip\n")


if __name__ == "__main__":
    print("=" * 60)
    print("Sanity-checking EIP-712 v2 signing")
    print("=" * 60)
    pk, expected = load_or_generate()
    print(f"maker/taker = {expected}\n")
    test_sign_quote_roundtrip(pk, expected)
    test_signed_taker_intent_roundtrip(pk, expected)
    print("=" * 60)
    print("All v2 round-trips passed.")
    print("=" * 60)
