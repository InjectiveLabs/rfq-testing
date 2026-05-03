"""EIP-712 v2 signing for RFQ quotes and conditional orders (TP/SL).

Mirrors the indexer's reference implementation at
`service/rfq/signature/eip712.go` byte-for-byte. The contract and indexer
verify v2 signatures by recomputing the same digest; any drift here will
cause `ErrSignatureMismatch`.

The protocol does NOT use `eth_signTypedData_v4`. It defines a custom
EIP-712 layout: a fixed `EIP712Domain(string,string,uint256,address)`
domain separator combined with a hand-rolled `SignQuote` /
`SignedTakerIntent` type hash. Encoding rules:

  * `string` and decimal fields -> `keccak256(utf8(s))` (32 bytes).
  * `address` -> 20 bytes from `bech32_to_evm`, left-padded to 32 bytes.
  * `uint8` / `uint32` / `uint64` -> big-endian, right-aligned in 32 bytes.
  * `SignQuote.bindingKind` is `1` when a taker address is present and `0`
    for blind quotes.
  * Fixed terminators on `SignedTakerIntent`:
      - `unfilledActionKind = 0` (none) and `unfilledActionPrice = "0"`.
      - `cid` is `keccak256("")` when null.
      - `allowedRelayer` is the zero address (32 zero bytes) when null.

Final digest = `keccak256(0x19 || 0x01 || domainSeparator || msgHash)`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

import bech32
from eth_account import Account
from eth_hash.auto import keccak

# --- Domain ------------------------------------------------------------

EIP712_DOMAIN_NAME = "RFQ"
EIP712_DOMAIN_VERSION = "1"
EIP712_DOMAIN_TYPE = (
    b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)

# --- Type hashes (must match indexer) ----------------------------------

SIGN_QUOTE_TYPE = (
    b"SignQuote("
    b"uint64 evmChainId,string marketId,uint64 rfqId,address taker,uint8 takerDirection,"
    b"string takerMargin,string takerQuantity,address maker,"
    b"uint32 makerSubaccountNonce,string makerQuantity,string makerMargin,"
    b"string price,uint8 expiryKind,uint64 expiryValue,string minFillQuantity,"
    b"uint8 bindingKind"
    b")"
)

SIGNED_TAKER_INTENT_TYPE = (
    b"SignedTakerIntent("
    b"uint8 version,uint64 evmChainId,address taker,uint64 epoch,uint64 rfqId,string marketId,"
    b"uint32 subaccountNonce,uint64 laneVersion,uint64 deadlineMs,"
    b"uint8 direction,string quantity,string margin,string worstPrice,"
    b"string minTotalFillQuantity,uint8 triggerKind,string triggerPrice,"
    b"uint8 unfilledActionKind,string unfilledActionPrice,string cid,"
    b"address allowedRelayer"
    b")"
)

# --- Constants ---------------------------------------------------------

DIRECTION_LONG = 0
DIRECTION_SHORT = 1

EXPIRY_KIND_TIMESTAMP = 0
EXPIRY_KIND_HEIGHT = 1

TRIGGER_KIND_IMMEDIATE = 0
TRIGGER_KIND_MARK_PRICE_GTE = 1
TRIGGER_KIND_MARK_PRICE_LTE = 2

UNFILLED_ACTION_KIND_NONE = 0  # only kind that's part of the v2 digest

BINDING_KIND_BLIND = 0
BINDING_KIND_TAKER_SPECIFIC = 1


# --- Address helpers ---------------------------------------------------

def bech32_to_evm(addr: str) -> bytes:
    """Decode an `inj1...` bech32 address into its 20-byte EVM form.

    Mirrors `signature.AddrToEVM` in the indexer.
    """
    hrp, data = bech32.bech32_decode(addr)
    if hrp != "inj":
        raise ValueError(f"unexpected bech32 hrp {hrp!r}, want 'inj'")
    if data is None:
        raise ValueError(f"invalid bech32: {addr!r}")
    raw = bech32.convertbits(data, 5, 8, False)
    if raw is None or len(raw) != 20:
        raise ValueError(f"expected 20-byte address, got {len(raw) if raw else 0}")
    return bytes(raw)


# --- Word encoders (return 32 bytes each) ------------------------------

def _enc_u8(v: int) -> bytes:
    if not 0 <= v <= 0xFF:
        raise ValueError(f"uint8 out of range: {v}")
    return b"\x00" * 31 + bytes([v])


def _enc_u32(v: int) -> bytes:
    if not 0 <= v <= 0xFFFFFFFF:
        raise ValueError(f"uint32 out of range: {v}")
    return b"\x00" * 28 + v.to_bytes(4, "big")


def _enc_u64(v: int) -> bytes:
    if not 0 <= v <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"uint64 out of range: {v}")
    return b"\x00" * 24 + v.to_bytes(8, "big")


def _enc_addr(addr20: bytes) -> bytes:
    if len(addr20) != 20:
        raise ValueError(f"expected 20-byte address, got {len(addr20)}")
    return b"\x00" * 12 + addr20


def _enc_zero_addr() -> bytes:
    return b"\x00" * 32


def _enc_string(s: str) -> bytes:
    return keccak(s.encode("utf-8"))


def _enc_decimal(s: str) -> bytes:
    """Decimals are encoded as `keccak256(utf8(s))`. The string must be the
    same one sent on the wire (no formatting drift)."""
    return _enc_string(s)


def _enc_opt_string(s: Optional[str]) -> bytes:
    return _enc_string(s if s is not None else "")


def _enc_opt_addr(bech: Optional[str]) -> bytes:
    if not bech:
        return _enc_zero_addr()
    return _enc_addr(bech32_to_evm(bech))


def _direction_word(direction: str) -> int:
    d = direction.lower()
    if d == "long":
        return DIRECTION_LONG
    if d == "short":
        return DIRECTION_SHORT
    raise ValueError(f"invalid direction {direction!r}")


def _trigger_words(trigger_type: str, trigger_price: Optional[str]) -> tuple[int, str]:
    t = trigger_type.lower() if trigger_type else "immediate"
    if t == "immediate":
        return TRIGGER_KIND_IMMEDIATE, "0"
    if t == "mark_price_gte":
        if not trigger_price:
            raise ValueError("mark_price_gte trigger requires a price")
        return TRIGGER_KIND_MARK_PRICE_GTE, trigger_price
    if t == "mark_price_lte":
        if not trigger_price:
            raise ValueError("mark_price_lte trigger requires a price")
        return TRIGGER_KIND_MARK_PRICE_LTE, trigger_price
    raise ValueError(f"unknown trigger type {trigger_type!r}")


# --- Domain separator + final digest -----------------------------------

def domain_separator(evm_chain_id: int, verifying_contract_bech32: str) -> bytes:
    """Compute the EIP-712 domain separator for the RFQ contract."""
    addr20 = bech32_to_evm(verifying_contract_bech32)
    type_hash = keccak(EIP712_DOMAIN_TYPE)
    buf = b"".join((
        type_hash,
        _enc_string(EIP712_DOMAIN_NAME),
        _enc_string(EIP712_DOMAIN_VERSION),
        _enc_u64(evm_chain_id),  # chainId is uint256 in the type, but EVM chain
                                  # IDs fit in uint64 (Injective is 1439/1776/etc).
                                  # Indexer code uses big.Int.FillBytes(32) which
                                  # is byte-identical to right-aligned u64 here.
        _enc_addr(addr20),
    ))
    return keccak(buf)


def _final_digest(domain_sep: bytes, msg_hash: bytes) -> bytes:
    return keccak(b"\x19\x01" + domain_sep + msg_hash)


# --- Quote digest (SignQuote) ------------------------------------------

def sign_quote_digest(
    *,
    evm_chain_id: int,
    verifying_contract_bech32: str,
    market_id: str,
    rfq_id: int,
    taker_bech32: Optional[str],
    direction: str,
    taker_margin: str,
    taker_quantity: str,
    maker_bech32: str,
    maker_subaccount_nonce: int,
    maker_quantity: str,
    maker_margin: str,
    price: str,
    expiry_kind: int,           # 0 = timestamp_ms, 1 = block height
    expiry_value: int,
    min_fill_quantity: Optional[str] = None,
) -> bytes:
    """Compute the 32-byte EIP-712 v2 digest for a SignQuote."""
    type_hash = keccak(SIGN_QUOTE_TYPE)
    msg = b"".join((
        type_hash,
        _enc_u64(evm_chain_id),
        _enc_string(market_id),
        _enc_u64(rfq_id),
        _enc_opt_addr(taker_bech32),
        _enc_u8(_direction_word(direction)),
        _enc_decimal(taker_margin),
        _enc_decimal(taker_quantity),
        _enc_addr(bech32_to_evm(maker_bech32)),
        _enc_u32(maker_subaccount_nonce),
        _enc_decimal(maker_quantity),
        _enc_decimal(maker_margin),
        _enc_decimal(price),
        _enc_u8(expiry_kind),
        _enc_u64(expiry_value),
        _enc_decimal(min_fill_quantity if min_fill_quantity is not None else "0"),
        _enc_u8(
            BINDING_KIND_TAKER_SPECIFIC if taker_bech32 else BINDING_KIND_BLIND
        ),
    ))
    domain_sep = domain_separator(evm_chain_id, verifying_contract_bech32)
    return _final_digest(domain_sep, keccak(msg))


# --- Conditional order digest (SignedTakerIntent) ----------------------

def signed_taker_intent_digest(
    *,
    evm_chain_id: int,
    verifying_contract_bech32: str,
    version: int,
    taker_bech32: str,
    epoch: int,
    rfq_id: int,
    market_id: str,
    subaccount_nonce: int,
    lane_version: int,
    deadline_ms: int,
    direction: str,
    quantity: str,
    margin: str,
    worst_price: str,
    min_total_fill_quantity: str,
    trigger_type: str,
    trigger_price: Optional[str],
    cid: Optional[str] = None,
    allowed_relayer: Optional[str] = None,
) -> bytes:
    """Compute the 32-byte EIP-712 v2 digest for a SignedTakerIntent.

    `unfilled_action` is intentionally not a parameter — v2 digests fix
    `unfilledActionKind = 0` and `unfilledActionPrice = "0"`. The wire
    payload may still carry `unfilled_action` (the indexer uses it
    post-trigger), but it is not bound by the signature.
    """
    trigger_kind, encoded_trigger_price = _trigger_words(trigger_type, trigger_price)

    type_hash = keccak(SIGNED_TAKER_INTENT_TYPE)
    msg = b"".join((
        type_hash,
        _enc_u8(version),
        _enc_u64(evm_chain_id),
        _enc_addr(bech32_to_evm(taker_bech32)),
        _enc_u64(epoch),
        _enc_u64(rfq_id),
        _enc_string(market_id),
        _enc_u32(subaccount_nonce),
        _enc_u64(lane_version),
        _enc_u64(deadline_ms),
        _enc_u8(_direction_word(direction)),
        _enc_decimal(quantity),
        _enc_decimal(margin),
        _enc_decimal(worst_price),
        _enc_decimal(min_total_fill_quantity),
        _enc_u8(trigger_kind),
        _enc_decimal(encoded_trigger_price),
        _enc_u8(UNFILLED_ACTION_KIND_NONE),
        _enc_decimal("0"),
        _enc_opt_string(cid),
        _enc_opt_addr(allowed_relayer),
    ))
    domain_sep = domain_separator(evm_chain_id, verifying_contract_bech32)
    return _final_digest(domain_sep, keccak(msg))


# --- Signing -----------------------------------------------------------

def _normalize_pk(private_key: str) -> bytes:
    pk = private_key[2:] if private_key.startswith("0x") else private_key
    return bytes.fromhex(pk)


def _sign_digest(digest: bytes, private_key: str) -> str:
    """Sign a 32-byte digest with secp256k1 and return `0x` + r||s||v hex.

    `v` is serialized as compact y-parity (`0` or `1`), matching
    ws-client/feat/changes-for-eip-712.
    """
    if len(digest) != 32:
        raise ValueError(f"digest must be 32 bytes, got {len(digest)}")
    account = Account.from_key(_normalize_pk(private_key))
    sig = account.unsafe_sign_hash(digest)
    v = sig.v - 27 if sig.v >= 27 else sig.v
    sig_bytes = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([v])
    return "0x" + sig_bytes.hex()


def sign_quote_v2(
    *,
    private_key: str,
    evm_chain_id: int,
    verifying_contract_bech32: str,
    market_id: str,
    rfq_id: int,
    taker: Optional[str],
    direction: str,
    taker_margin: Union[str, Decimal],
    taker_quantity: Union[str, Decimal],
    maker: str,
    maker_margin: Union[str, Decimal],
    maker_quantity: Union[str, Decimal],
    price: Union[str, Decimal],
    expiry_ms: Optional[int] = None,
    expiry_height: Optional[int] = None,
    maker_subaccount_nonce: int = 0,
    min_fill_quantity: Optional[Union[str, Decimal]] = None,
) -> str:
    """Sign a quote with EIP-712 v2 and return `0x` + r||s||v hex.

    Pass exactly one of `expiry_ms` or `expiry_height`. All decimal fields
    must be the exact string sent on the wire (pre-quantized to the
    market tick size — see `utils.price.quantize_to_tick`).
    """
    if (expiry_ms is None) == (expiry_height is None):
        raise ValueError("provide exactly one of expiry_ms or expiry_height")

    if expiry_ms is not None:
        expiry_kind, expiry_value = EXPIRY_KIND_TIMESTAMP, int(expiry_ms)
    else:
        expiry_kind, expiry_value = EXPIRY_KIND_HEIGHT, int(expiry_height)

    digest = sign_quote_digest(
        evm_chain_id=evm_chain_id,
        verifying_contract_bech32=verifying_contract_bech32,
        market_id=market_id,
        rfq_id=int(rfq_id),
        taker_bech32=taker,
        direction=direction,
        taker_margin=str(taker_margin),
        taker_quantity=str(taker_quantity),
        maker_bech32=maker,
        maker_subaccount_nonce=int(maker_subaccount_nonce),
        maker_quantity=str(maker_quantity),
        maker_margin=str(maker_margin),
        price=str(price),
        expiry_kind=expiry_kind,
        expiry_value=expiry_value,
        min_fill_quantity=None if min_fill_quantity is None else str(min_fill_quantity),
    )
    return _sign_digest(digest, private_key)


def sign_conditional_order_v2(
    *,
    private_key: str,
    evm_chain_id: int,
    verifying_contract_bech32: str,
    version: int,
    taker: str,
    epoch: int,
    rfq_id: int,
    market_id: str,
    subaccount_nonce: int,
    lane_version: int,
    deadline_ms: int,
    direction: str,
    quantity: Union[str, Decimal],
    margin: Union[str, Decimal],
    worst_price: Union[str, Decimal],
    min_total_fill_quantity: Union[str, Decimal],
    trigger_type: str,
    trigger_price: Optional[Union[str, Decimal]] = None,
    cid: Optional[str] = None,
    allowed_relayer: Optional[str] = None,
) -> str:
    """Sign a conditional order (TP/SL) with EIP-712 v2.

    Returns `0x` + r||s||v hex.
    """
    digest = signed_taker_intent_digest(
        evm_chain_id=evm_chain_id,
        verifying_contract_bech32=verifying_contract_bech32,
        version=int(version),
        taker_bech32=taker,
        epoch=int(epoch),
        rfq_id=int(rfq_id),
        market_id=market_id,
        subaccount_nonce=int(subaccount_nonce),
        lane_version=int(lane_version),
        deadline_ms=int(deadline_ms),
        direction=direction,
        quantity=str(quantity),
        margin=str(margin),
        worst_price=str(worst_price),
        min_total_fill_quantity=str(min_total_fill_quantity),
        trigger_type=trigger_type,
        trigger_price=None if trigger_price is None else str(trigger_price),
        cid=cid,
        allowed_relayer=allowed_relayer,
    )
    return _sign_digest(digest, private_key)
