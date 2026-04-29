"""Cryptographic utilities for RFQ.

Public API is v2 (EIP-712). The legacy v1 raw-JSON helpers
(`sign_quote`, `sign_conditional_order`) are still importable from
`rfq_test.crypto.signing` for forensic / regression testing, but every
new caller should use the v2 entry points below.
"""

from rfq_test.crypto.eip712 import (
    bech32_to_evm,
    sign_conditional_order_v2,
    sign_quote_v2,
)
from rfq_test.crypto.wallet import Wallet, generate_wallets_from_seed

__all__ = [
    "bech32_to_evm",
    "sign_quote_v2",
    "sign_conditional_order_v2",
    "Wallet",
    "generate_wallets_from_seed",
]
