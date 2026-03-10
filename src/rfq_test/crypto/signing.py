"""Quote signature verification (Keccak256 + ECDSA).

Matches the indexer's quote signature verification: makers must sign their quotes
using their private key. Signature payload includes: market ID, RFQ ID, taker/maker
addresses, quantities, margins, price, and expiry (field order: mi, id, t, td, tm,
tq, m, mq, mm, p, e).

Process:
1. Build SignQuote structure with abbreviated field names (matches indexer SignQuote)
2. JSON stringify (no spaces, field order preserved)
3. Hash with Keccak256
4. Sign the hash with ECDSA secp256k1 (Ethereum-style)
5. Return signature as 65 bytes (r + s + v) in hex; indexer expects hex with 0x prefix when sending
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Union

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_hash.auto import keccak


@dataclass
class SignQuoteData:
    """Data structure for quote signing.

    Field names match the abbreviated format expected by the contract:
    - c: chain_id, ca: contract_address (required for contract verification)
    - mi: market_id, id: rfq_id, t: taker, td: direction, tm/tq/mm/mq: margins/quantities, p: price, e: expiry
    """
    rfq_id: str
    mi: str  # market_id
    d: str   # direction
    t: str   # taker
    tm: str  # taker_margin
    tq: str  # taker_quantity
    m: str   # maker
    mq: str  # maker_quantity
    mm: str  # maker_margin
    p: str   # price
    e: int   # expiry
    chain_id: str = ""
    contract_address: str = ""

    def to_dict(self) -> dict:
        """Convert to dict with exact field order for JSON.

        Contract order: c, ca, mi, id, t, td, tm, tq, m, mq, mm, p, e.
        Contract expects 'id' as number and 'e' as {"ts": <timestamp>} or {"h": <height>}.
        """
        rfq_id_int = int(self.rfq_id) if isinstance(self.rfq_id, str) else self.rfq_id
        out = {}
        if self.chain_id:
            out["c"] = self.chain_id
        if self.contract_address:
            out["ca"] = self.contract_address
        out["mi"] = self.mi
        out["id"] = rfq_id_int
        out["t"] = self.t
        out["td"] = self.d
        out["tm"] = self.tm
        out["tq"] = self.tq
        out["m"] = self.m
        out["mq"] = self.mq
        out["mm"] = self.mm
        out["p"] = self.p
        out["e"] = {"ts": self.e}
        return out


def sign_quote(
    private_key: str,
    rfq_id: str,
    market_id: str,
    direction: str,
    taker: str,
    taker_margin: Union[str, Decimal],
    taker_quantity: Union[str, Decimal],
    maker: str,
    maker_margin: Union[str, Decimal],
    maker_quantity: Union[str, Decimal],
    price: Union[str, Decimal],
    expiry: int,
    chain_id: Optional[str] = None,
    contract_address: Optional[str] = None,
) -> str:
    """Sign a quote with the maker's private key.

    For contract verification to pass, pass chain_id and contract_address
    (e.g. from env_config.signing_context()).

    Args:
        private_key: Maker's private key (hex, with or without 0x prefix)
        rfq_id: Request ID
        market_id: Market ID
        direction: Trade direction ("Long" or "Short")
        taker: Taker's Injective address
        taker_margin: Taker's margin amount
        taker_quantity: Taker's quantity
        maker: Maker's Injective address
        maker_margin: Maker's margin amount
        maker_quantity: Maker's quantity
        price: Quote price
        expiry: Expiry timestamp (unix ms or s)
        chain_id: Chain ID for contract verification (optional)
        contract_address: Contract address for contract verification (optional)

    Returns:
        Hex-encoded signature (without 0x prefix)
    """
    # Build sign quote data
    # Contract expects lowercase direction ("long" or "short") for signature verification
    direction_lower = direction.lower() if isinstance(direction, str) else direction.value.lower()
    
    # Preserve the exact price string that will be sent to the indexer.
    # Any normalization here can make the signed payload differ from the quote
    # payload (for example trailing zeros), which breaks signature recovery.
    price_str = str(price)
    
    sign_data = SignQuoteData(
        rfq_id=rfq_id,
        mi=market_id,
        d=direction_lower,
        t=taker,
        tm=str(taker_margin),
        tq=str(taker_quantity),
        m=maker,
        mq=str(maker_quantity),
        mm=str(maker_margin),
        p=price_str,  # Use normalized price string
        e=expiry,
        chain_id=chain_id or "",
        contract_address=contract_address or "",
    )
    
    # JSON stringify (must match TS exactly - no spaces, preserve field order)
    # DO NOT use sort_keys=True - contract expects specific field order: mi, id, t, td, tm, tq, m, mq, mm, p, e
    payload = json.dumps(sign_data.to_dict(), separators=(",", ":"))
    
    # Debug: log the exact JSON being signed (for troubleshooting signature verification)
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"Signing payload JSON: {payload}")
    
    # Hash with keccak256
    message_hash = keccak(payload.encode("utf-8"))
    
    # Sign with secp256k1
    # Normalize private key
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    account = Account.from_key(bytes.fromhex(private_key))
    
    # Sign the raw hash (not EIP-191 message)
    signature = account.unsafe_sign_hash(message_hash)
    
    # Return serialized signature (r + s + v, 65 bytes)
    # Format: 32 bytes r + 32 bytes s + 1 byte v
    sig_bytes = (
        signature.r.to_bytes(32, "big") +
        signature.s.to_bytes(32, "big") +
        bytes([signature.v])
    )
    
    return sig_bytes.hex()


def verify_signature(
    signature: str,
    rfq_id: str,
    market_id: str,
    direction: str,
    taker: str,
    taker_margin: str,
    taker_quantity: str,
    maker: str,
    maker_margin: str,
    maker_quantity: str,
    price: str,
    expiry: int,
    chain_id: Optional[str] = None,
    contract_address: Optional[str] = None,
) -> str:
    """Verify a quote signature and recover the signer address.
    
    Args:
        signature: Hex-encoded signature
        ... (same as sign_quote)
        
    Returns:
        Recovered Ethereum address (for verification against maker)
    """
    # Build sign quote data
    # Contract expects lowercase direction ("long" or "short") for signature verification
    direction_lower = direction.lower() if isinstance(direction, str) else direction.value.lower()
    
    sign_data = SignQuoteData(
        rfq_id=rfq_id,
        mi=market_id,
        d=direction_lower,
        t=taker,
        tm=taker_margin,
        tq=taker_quantity,
        m=maker,
        mq=maker_quantity,
        mm=maker_margin,
        p=price,
        e=expiry,
        chain_id=chain_id or "",
        contract_address=contract_address or "",
    )

    payload = json.dumps(sign_data.to_dict(), separators=(",", ":"))
    message_hash = keccak(payload.encode("utf-8"))
    
    # Recover signer
    sig_bytes = bytes.fromhex(signature.replace("0x", ""))
    r = int.from_bytes(sig_bytes[:32], "big")
    s = int.from_bytes(sig_bytes[32:64], "big")
    v = sig_bytes[64]
    
    # Create signature object
    from eth_account._utils.signing import to_standard_signature_bytes
    
    recovered = Account._recover_hash(
        message_hash,
        signature=sig_bytes,
    )
    
    return recovered
