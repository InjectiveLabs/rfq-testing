"""Quote and conditional-order signing (Keccak256 + secp256k1 ECDSA).

Makers sign their quotes with their private key. The contract verifies the
signature by rebuilding the same JSON payload and checking against the maker's
registered address.

Process:
1. Build a JSON payload with abbreviated field names in the exact required order
2. Serialize with json.dumps(separators=(",", ":")) — no spaces, no sort_keys
3. Hash with keccak256
4. Sign the raw hash with secp256k1 (unsafe_sign_hash — no EIP-191 prefix)
5. Return 65-byte signature (r + s + v) as hex

Quote signing field order (CRITICAL — wrong order = rejected signature):
  c, ca, mi, id, t, td, tm, tq, m, ms, mq, mm, p, e [, mfq]

  c   = chain_id
  ca  = contract_address
  mi  = market_id
  id  = rfq_id (integer)
  t   = taker address
  td  = direction ("long" or "short")
  tm  = taker_margin
  tq  = taker_quantity
  m   = maker address
  ms  = maker_subaccount_nonce (integer, required — use 0 if not using subaccounts)
  mq  = maker_quantity
  mm  = maker_margin
  p   = price
  e   = expiry as {"ts": <unix_ms>} or {"h": <block_height>}
  mfq = min_fill_quantity (optional V2 field, omit if not needed)

Note: the "ms" field was added after the initial release. Any existing
implementation using the old order (m → mq → mm) must be updated.

Conditional order signing uses a separate function (sign_conditional_order)
with a different payload structure — see below.
"""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Union

from eth_account import Account
from eth_hash.auto import keccak

logger = logging.getLogger(__name__)


def _normalize_decimal_str(value: Union[str, Decimal, None]) -> str:
    """Normalize a decimal value to a consistent string without trailing zeros."""
    if value is None:
        return ""
    d = Decimal(str(value))
    formatted = format(d, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


@dataclass
class SignQuoteData:
    """Data structure for quote signing.

    Field names match the abbreviated format expected by the contract:
    c=chain_id, ca=contract_address, mi=market_id, id=rfq_id, t=taker,
    td=direction, tm=taker_margin, tq=taker_quantity, m=maker,
    ms=maker_subaccount_nonce, mq=maker_quantity, mm=maker_margin,
    p=price, e=expiry, mfq=min_fill_quantity (optional V2).
    """
    rfq_id: str
    mi: str   # market_id
    d: str    # direction
    t: str    # taker
    tm: str   # taker_margin
    tq: str   # taker_quantity
    m: str    # maker
    mq: str   # maker_quantity
    mm: str   # maker_margin
    p: str    # price
    e: int    # expiry (unix ms)
    ms: int = 0  # maker_subaccount_nonce
    chain_id: str = ""
    contract_address: str = ""
    min_fill_quantity: Optional[Union[str, Decimal]] = None  # mfq — V2 optional field

    def to_dict(self) -> dict:
        """Convert to dict with exact field order required by the contract.

        Order: c, ca, mi, id, t, td, tm, tq, m, ms, mq, mm, p, e [, mfq].
        Do NOT reorder — the contract hashes this exact JSON string.
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
        out["ms"] = self.ms
        out["mq"] = self.mq
        out["mm"] = self.mm
        out["p"] = self.p
        out["e"] = {"ts": self.e}
        if self.min_fill_quantity is not None:
            out["mfq"] = self.min_fill_quantity
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
    maker_subaccount_nonce: int = 0,
    min_fill_quantity: Optional[Union[str, Decimal]] = None,
) -> str:
    """Sign a quote with the maker's private key.

    Args:
        private_key: Maker's private key (hex, with or without 0x prefix)
        rfq_id: Request ID (integer as string)
        market_id: Derivative market ID (0x hex string)
        direction: Trade direction ("long" or "short")
        taker: Taker's Injective address
        taker_margin: Taker's margin amount (human-readable decimal string)
        taker_quantity: Taker's quantity
        maker: Maker's Injective address
        maker_margin: Maker's margin amount
        maker_quantity: Maker's quantity
        price: Quote price (human-readable decimal string, e.g. "4.5"). Must be
            pre-quantized to the market tick size — see quantize_to_tick().
        expiry: Quote expiry as Unix timestamp in milliseconds
        chain_id: Chain ID (e.g. "injective-888"). Required for contract verification.
        contract_address: RFQ contract address. Required for contract verification.
        maker_subaccount_nonce: Subaccount index used when the maker was registered
            (default 0). This is the "ms" field in the signed payload and MUST be
            included — omitting it will cause signature verification to fail.
        min_fill_quantity: Minimum quantity the maker will accept as a partial fill
            (optional V2 field). When set, appended as "mfq" at the end of the payload.

    Returns:
        Hex-encoded signature without 0x prefix (65 bytes: r + s + v)

    Note:
        The indexer expects the signature with a "0x" prefix. Prepend it before
        sending over the MakerStream WebSocket.
    """
    # Contract expects lowercase direction
    direction_lower = direction.lower() if isinstance(direction, str) else direction

    # Pass price and quantities through _normalize_decimal_str so that the
    # signed JSON is consistent regardless of whether the caller passes a
    # Decimal or a pre-formatted string. Always quantize prices to the market
    # tick size BEFORE calling this function — see quantize_to_tick().
    sign_data = SignQuoteData(
        rfq_id=rfq_id,
        mi=market_id,
        d=direction_lower,
        t=taker,
        tm=_normalize_decimal_str(taker_margin),
        tq=_normalize_decimal_str(taker_quantity),
        m=maker,
        mq=_normalize_decimal_str(maker_quantity),
        mm=_normalize_decimal_str(maker_margin),
        p=_normalize_decimal_str(price),
        e=expiry,
        ms=maker_subaccount_nonce,
        chain_id=chain_id or "",
        contract_address=contract_address or "",
        min_fill_quantity=_normalize_decimal_str(min_fill_quantity) if min_fill_quantity is not None else None,
    )

    payload = json.dumps(sign_data.to_dict(), separators=(",", ":"))
    logger.debug("Signing quote payload: %s", payload)

    message_hash = keccak(payload.encode("utf-8"))

    if private_key.startswith("0x"):
        private_key = private_key[2:]

    account = Account.from_key(bytes.fromhex(private_key))
    signature = account.unsafe_sign_hash(message_hash)

    sig_bytes = (
        signature.r.to_bytes(32, "big") +
        signature.s.to_bytes(32, "big") +
        bytes([signature.v])
    )
    return sig_bytes.hex()


def sign_conditional_order(
    private_key: str,
    version: int,
    chain_id: str,
    contract_address: str,
    taker: str,
    epoch: int,
    rfq_id: int,
    market_id: str,
    subaccount_nonce: int,
    lane_version: int,
    deadline_ms: int,
    direction: str,
    quantity: Union[str, Decimal],
    worst_price: Union[str, Decimal],
    min_total_fill_quantity: Union[str, Decimal],
    trigger_type: str,
    trigger_price: Union[str, Decimal],
    unfilled_action: Optional[str] = None,
    cid: Optional[str] = None,
    allowed_relayer: Optional[str] = None,
    margin: Union[str, Decimal] = "0",
) -> str:
    """Sign a conditional order (TP/SL) with the taker's private key.

    The payload field order is fixed — do NOT reorder.

    Args:
        private_key: Taker's private key (hex, with or without 0x prefix)
        version: Protocol version (use 1)
        chain_id: Chain ID (e.g. "injective-888")
        contract_address: RFQ contract address
        taker: Taker's Injective address
        epoch: Current epoch for this taker (from CancelAllIntents tracking; start at 1)
        rfq_id: Unique order ID — use current Unix timestamp in ms
        market_id: Derivative market ID (0x hex string)
        subaccount_nonce: Subaccount index (default 0)
        lane_version: Current lane version for this taker+market+subaccount_nonce
            (from CancelIntentLane tracking; start at 1)
        deadline_ms: Order expiry as Unix timestamp in ms (max 30 days from now)
        direction: "long" or "short"
        quantity: Order quantity (human-readable decimal string)
        worst_price: Worst acceptable fill price
        min_total_fill_quantity: Minimum total quantity that must be filled
        trigger_type: "mark_price_gte" (fires when mark >= trigger_price) or
                      "mark_price_lte" (fires when mark <= trigger_price)
        trigger_price: Price level that triggers the order
        unfilled_action: Optional post-fill action (None, "limit", or "market")
        cid: Optional client ID for tracking
        allowed_relayer: Optional address of the only relayer permitted to execute
        margin: Collateral margin — must be "0" for v1 reduce-only (close) orders

    Returns:
        Hex-encoded signature with "0x" prefix (65 bytes: r + s + v)
    """
    direction_lower = direction.lower() if isinstance(direction, str) else direction

    canonical = {
        "version": version,
        "chain_id": chain_id,
        "contract_address": contract_address,
        "taker": taker,
        "epoch": epoch,
        "rfq_id": rfq_id,
        "market_id": market_id,
        "subaccount_nonce": subaccount_nonce,
        "lane_version": lane_version,
        "deadline_ms": deadline_ms,
        "direction": direction_lower,
        "quantity": _normalize_decimal_str(quantity),
        "margin": _normalize_decimal_str(margin),
        "worst_price": _normalize_decimal_str(worst_price),
        "min_total_fill_quantity": _normalize_decimal_str(min_total_fill_quantity),
        "trigger": {trigger_type: _normalize_decimal_str(trigger_price)},
        "unfilled_action": unfilled_action,
        "cid": cid,
        "allowed_relayer": allowed_relayer,
    }

    payload = json.dumps(canonical, separators=(",", ":"))
    logger.debug("Signing conditional order payload: %s", payload)

    message_hash = keccak(payload.encode("utf-8"))

    if private_key.startswith("0x"):
        private_key = private_key[2:]

    account = Account.from_key(bytes.fromhex(private_key))
    signature = account.unsafe_sign_hash(message_hash)

    sig_bytes = (
        signature.r.to_bytes(32, "big") +
        signature.s.to_bytes(32, "big") +
        bytes([signature.v])
    )
    return "0x" + sig_bytes.hex()


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
    maker_subaccount_nonce: int = 0,
) -> str:
    """Verify a quote signature and recover the signer address.

    Args:
        signature: Hex-encoded signature (with or without 0x prefix)
        (all other args same as sign_quote)

    Returns:
        Recovered Ethereum address (compare against the maker's ETH address)
    """
    direction_lower = direction.lower() if isinstance(direction, str) else direction

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
        ms=maker_subaccount_nonce,
        chain_id=chain_id or "",
        contract_address=contract_address or "",
    )

    payload = json.dumps(sign_data.to_dict(), separators=(",", ":"))
    message_hash = keccak(payload.encode("utf-8"))

    sig_bytes = bytes.fromhex(signature.replace("0x", ""))
    recovered = Account._recover_hash(message_hash, signature=sig_bytes)
    return recovered
