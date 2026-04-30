# Python Guide: Building RFQ Market Making & Retail Tools

**For teams building standalone MM (Market Maker) or retail trading scripts.**  
This guide helps you avoid common pitfalls and build correctly from day one. You do **not** need the `rfq-testing` framework—everything here is self-contained.

---

## Table of Contents

1. [Who This Is For](#who-this-is-for)
2. [Architecture Overview](#architecture-overview)
3. [Grant Creation (Critical)](#grant-creation-critical)
4. [Quote Signing](#quote-signing)
5. [Indexer Integration (WebSocket)](#indexer-integration-websocket)
6. [Contract Expectations](#contract-expectations)
7. [Conditional Orders (TP/SL)](#conditional-orders-tpsl)
8. [Error Handling](#error-handling)
9. [Production Tips](#production-tips)
10. [Quick Reference](#quick-reference)

---

## Who This Is For

- **Market makers** building Python bots that send quotes
- **Retail integrators** building request/accept flows
- **Anyone** implementing RFQ flows without using our test framework

**What this guide covers:** Grants, signing, indexer protocol, contract expectations, and lessons we learned the hard way.

**What it does not cover:** Full end-to-end examples (see [examples/](./examples/) for TypeScript, Go, and Python reference scripts).

---

## Architecture Overview

```
Retail (Taker)                    Indexer (WebSocket)              Contract (On-Chain)
    |                                    |                                  |
    |---- Create RFQ Request ----------->|                                  |
    |<--- Request ACK (rfq_id) ----------|                                  |
    |                                    |<--- Request (MakerStream) -------| (MMs receive)
    |                                    |                                  |
    |                                    |<--- Quote (MakerStream) ----------| (MM sends)
    |<--- Quote -------------------------|                                  |
    |                                    |                                  |
    |---- AcceptQuote (CosmWasm) ------------------------------------------>|
    |<--- Tx confirmation --------------------------------------------------|
```

- **Indexer:** WebSocket (gRPC-over-WebSocket). TakerStream (retail) and MakerStream (MM) are separate endpoints.
- **Contract:** CosmWasm. Retail calls `AcceptQuote` with signed quotes; contract verifies signatures and settles.

---

## Grant Creation (Critical)

### The Problem

Both **MM** and **Retail** must grant the RFQ contract permission to execute messages on their behalf. If you miss a grant, `accept_quote` fails with `authorization not found`.

### Required Grants

| Role  | Message Types |
|-------|----------------|
| **MM**   | `MsgSend`, `MsgPrivilegedExecuteContract` |
| **Retail** | `MsgSend`, `MsgPrivilegedExecuteContract` |

**Retail needs both.** A common mistake is granting only `MsgSend`—the contract also needs `MsgPrivilegedExecuteContract` to execute the trade.

### Use Gas Heuristics, Not Simulation

Gas simulation underestimates gas for grant transactions. On some chains this causes panic or "out of gas". **Always use gas heuristics for grant broadcasts:**

```python
from pyinjective.core.broadcaster import MsgBroadcasterWithPk

# DO: Use gas heuristics for grant transactions
broadcaster = MsgBroadcasterWithPk.new_using_gas_heuristics(
    network=network,
    private_key=private_key,
)

# DON'T: Use simulation for grant transactions
# broadcaster = MsgBroadcasterWithPk.new_using_simulation(...)  # Avoid!
```

### Use GenericAuthorization, Not SendAuthorization

The RFQ contract expects `GenericAuthorization` for all message types (including `MsgSend`). Do not use `SendAuthorization` with spend limits.

### Use Expiration: Null (Permanent Grants)

The contract expects grants with **no expiration** (`expiration: null`). The pyinjective `msg_grant_generic()` helper requires an expiration—so you must build the grant manually:

```python
from pyinjective.proto.cosmos.authz.v1beta1 import authz_pb2, tx_pb2 as authz_tx_pb2
from google.protobuf import any_pb2

def create_grant_msg(granter: str, grantee: str, msg_type: str):
    """Create MsgGrant with expiration: null (permanent grant)."""
    generic_authz = authz_pb2.GenericAuthorization()
    generic_authz.msg = msg_type  # e.g. "/cosmos.bank.v1beta1.MsgSend"

    authz_any = any_pb2.Any()
    authz_any.type_url = "/cosmos.authz.v1beta1.GenericAuthorization"
    authz_any.value = generic_authz.SerializeToString()

    grant = authz_pb2.Grant()
    grant.authorization.CopyFrom(authz_any)
    # Do NOT set grant.expiration — that creates expiration: null

    grant_msg = authz_tx_pb2.MsgGrant()
    grant_msg.granter = granter
    grant_msg.grantee = grantee
    grant_msg.grant.CopyFrom(grant)
    return grant_msg
```

### Grant Both Types for Each Role

```python
MSG_TYPES = [
    "/cosmos.bank.v1beta1.MsgSend",
    "/injective.exchange.v2.MsgPrivilegedExecuteContract",
]

for msg_type in MSG_TYPES:
    grant_msg = create_grant_msg(granter, contract_address, msg_type)
    result = await broadcaster.broadcast([grant_msg])
    # Always check tx_response.code == 0 (see Error Handling)
```

---

## Quote Signing (v2)

The rfq-testing repo standard is **EIP-712 v2** signing. Every quote and conditional order MUST carry `sign_mode: "v2"` on the wire; missing or empty signing modes are rejected by the indexer.

> **TL;DR:** Build the `SignQuote` typed-data digest, sign it with secp256k1 raw (no EIP-191 prefix), prepend `0x` to the signature, and put `sign_mode: "v2"` in the wire payload.

### What v2 binds

The signature does NOT bind the wire fields `chain_id` / `contract_address`. Those are bound by the **EIP-712 domain separator** instead:

```
domain = EIP712Domain(
  name              = "RFQ",
  version           = "1",
  chainId           = <EVM chain ID>,            # 1439 testnet, 1776 mainnet
  verifyingContract = <bech32_to_evm(contract)>, # 20-byte hex of the bech32 address
)
```

EVM chain ID is `1439` on testnet and `1776` on mainnet — bake it into your config, don't hardcode.

### Type and field encoding

The `SignQuote` typed-data is custom — **not** `eth_signTypedData_v4`. The indexer hand-rolls the digest. Every field is right-aligned in a 32-byte word:

```
SignQuote(
  string  marketId,
  uint64  rfqId,
  address taker,
  uint8   takerDirection,            // 0=long, 1=short
  string  takerMargin,
  string  takerQuantity,
  address maker,
  uint32  makerSubaccountNonce,
  string  makerQuantity,
  string  makerMargin,
  string  price,
  uint8   expiryKind,                // 0=timestamp_ms, 1=block_height
  uint64  expiryValue,
  string  minFillQuantity,           // "0" if absent
  uint8   bindingKind                // 1 if taker is present, 0 for blind quotes
)
```

| Field type | Encoding |
|---|---|
| `string` and decimal fields | `keccak256(utf8(s))` |
| `address` | 20 bytes from `bech32_to_evm`, left-padded to 32 bytes |
| `uint8` / `uint32` / `uint64` | big-endian, right-aligned in 32 bytes |
| `bindingKind` | `1` for taker-specific quotes, `0` for blind quotes |
| `minFillQuantity` | `"0"` when absent — never empty string |

The final digest is `keccak256(0x19 || 0x01 || domainSeparator || msgHash)`.

### Decimal-as-string trap

Decimals are hashed as the raw UTF-8 string. `"4.5"` and `"4.50"` produce different digests. The wire price MUST equal the signed price byte-for-byte:

> **Correct order:** compute price → quantize to tick → sign → send (wire = signed)
> **Wrong order:** compute price → sign → quantize → send  ← signature mismatch!

### Use the library

```python
from rfq_test.crypto.eip712 import sign_quote_v2

signature = sign_quote_v2(
    private_key=mm_private_key,
    evm_chain_id=1439,                              # config.signing_context_v2[0]
    verifying_contract_bech32="inj1qw7jk82h...",    # config.signing_context_v2[1]
    market_id="0xdc70...",
    rfq_id=int(rfq_id),
    taker=taker_inj_addr,
    direction="long",                               # or "short"
    taker_margin="100",
    taker_quantity="1",
    maker=mm_inj_addr,
    maker_margin="100",
    maker_quantity="1",
    price="4.5",                                    # quantized to tick beforehand
    expiry_ms=int(time.time() * 1000) + 20_000,
    maker_subaccount_nonce=0,
    min_fill_quantity=None,
)
# Already prefixed with "0x"; pass through to MakerStream + REST as-is.
# The helper derives bindingKind from `taker`: taker set -> taker-bound,
# taker empty/None -> blind. Do not pass binding_kind or nonce to this helper.
```

### Standalone v2 signing (no rfq-testing dep)

If you can't import `rfq_test`, this is the byte-compatible reference (mirrors `service/rfq/signature/eip712.go` in `injective-indexer`):

```python
import bech32
from eth_account import Account
from eth_hash.auto import keccak

EIP712_DOMAIN_TYPE = (
    b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
SIGN_QUOTE_TYPE = (
    b"SignQuote(string marketId,uint64 rfqId,address taker,uint8 takerDirection,"
    b"string takerMargin,string takerQuantity,address maker,uint32 makerSubaccountNonce,"
    b"string makerQuantity,string makerMargin,string price,uint8 expiryKind,"
    b"uint64 expiryValue,string minFillQuantity,uint8 bindingKind)"
)

def bech32_to_evm(addr: str) -> bytes:
    hrp, data = bech32.bech32_decode(addr)
    assert hrp == "inj" and data is not None
    raw = bech32.convertbits(data, 5, 8, False)
    assert raw is not None and len(raw) == 20
    return bytes(raw)

def _u(n: int, width: int) -> bytes:        # right-aligned big-endian in 32 bytes
    return b"\x00" * (32 - width) + n.to_bytes(width, "big")
def _s(s: str) -> bytes: return keccak(s.encode("utf-8"))
def _addr(a: bytes) -> bytes: return b"\x00" * 12 + a
def _direction(d: str) -> int: return {"long": 0, "short": 1}[d.lower()]

def domain_separator(evm_chain_id: int, contract_bech32: str) -> bytes:
    return keccak(b"".join((
        keccak(EIP712_DOMAIN_TYPE),
        _s("RFQ"),
        _s("1"),
        _u(evm_chain_id, 8),
        _addr(bech32_to_evm(contract_bech32)),
    )))

def sign_quote_v2(
    *, private_key, evm_chain_id, verifying_contract_bech32,
    market_id, rfq_id, taker, direction,
    taker_margin, taker_quantity, maker, maker_subaccount_nonce,
    maker_quantity, maker_margin, price, expiry_ms,
    min_fill_quantity=None,
) -> str:
    msg = b"".join((
        keccak(SIGN_QUOTE_TYPE),
        _s(market_id), _u(int(rfq_id), 8),
        _addr(bech32_to_evm(taker)), _u(_direction(direction), 1),
        _s(str(taker_margin)), _s(str(taker_quantity)),
        _addr(bech32_to_evm(maker)), _u(int(maker_subaccount_nonce), 4),
        _s(str(maker_quantity)), _s(str(maker_margin)),
        _s(str(price)),
        _u(0, 1),  # expiryKind=timestamp
        _u(int(expiry_ms), 8),
        _s(str(min_fill_quantity) if min_fill_quantity is not None else "0"),
        _u(1, 1),  # bindingKind = 1 because taker is present
    ))
    digest = keccak(b"\x19\x01" + domain_separator(evm_chain_id, verifying_contract_bech32) + keccak(msg))
    pk = bytes.fromhex(private_key.removeprefix("0x"))
    sig = Account.from_key(pk).unsafe_sign_hash(digest)
    v = sig.v - 27 if sig.v >= 27 else sig.v
    return "0x" + (sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([v])).hex()
```

For block-height expiries pass `_u(1, 1)` (expiryKind=height) and the height as `expiryValue`.

### maker_subaccount_nonce

The subaccount index used when your address was registered as a maker. Default `0`. Contact the platform operator if you registered with a non-default nonce.

### Wire payload — `sign_mode` is required

Whatever path you use to send the quote (REST `/v1/quote`, MakerStream WS, or gRPC), include both the signature and the sign-mode literal:

```python
quote = {
    # ...all the fields you signed above (chain_id/contract_address/market_id/etc)...
    "signature": signature,
    "sign_mode": "v2",
}
```

If `sign_mode` is missing or empty the indexer closes the stream with a signing-mode validation error:
```
ConnectionClosedError(Close(code=1011, reason='invalid request:
  missing or unsupported sign_mode'))
```

---

## Indexer Integration (WebSocket)

### Endpoints

| Environment | WebSocket Base URL |
|-------------|--------------------|
| Testnet | `wss://testnet.rfq.ws.injective.network/injective_rfq_rpc.InjectiveRfqRPC` |

Append `/TakerStream` or `/MakerStream` to the base URL.

### Protocol

- **gRPC-over-WebSocket** with subprotocol `grpc-ws`
- Messages are protobuf-encoded with gRPC-web framing: `[1 byte compression][4 bytes length BE][payload]`
- Send **ping** messages periodically (e.g. every 1–2 seconds) to keep the connection alive

### TakerStream (Retail)

- **URL:** `{base_url}/TakerStream`
- **Connection metadata:** Send `request_address` (taker's Injective address) as a header when connecting. The indexer uses this to associate requests with the correct taker.
- **Request:** Send `CreateRFQRequestType` with `rfq_id`, `market_id`, `direction`, `margin`, `quantity`, `worst_price`, `expiry`.
- **Direction:** Use `"long"` or `"short"` (string). Do not use `0`/`1` or numeric values.

### MakerStream (MM)

- **URL:** `{base_url}/MakerStream`
- **Connection metadata:** Send `maker_address` as a header when connecting if you want maker-scoped updates.
- **Optional subscriptions:** Set `subscribe_to_quotes_updates: true` and `subscribe_to_settlement_updates: true` as headers to receive those maker stream updates.
- **Receive:** Requests arrive as stream messages
- **Quote update scope:** `quote_update` events are sent for quotes whose `maker` matches `maker_address`.
- **Settlement update scope:** `settlement_update` events are sent when a settlement includes at least one quote from `maker_address`, whether that specific quote was executed or not.
- **Quote status meaning:** In `quote_update`, `status="accepted"` means the quote was used; `status="rejected"` means it was considered but not used.
- **Executed fields:** In `quote_update`, `executed_quantity` and `executed_margin` are the actually filled amount and margin for that quote.
- **Send:** Quotes as `RFQQuoteType` with fields: `chain_id`, `contract_address`, `market_id`, `rfq_id`, `taker_direction`, `margin`, `quantity`, `price`, `expiry`, `maker`, `taker`, `signature`

### Proto Field Order (Quote)

The indexer expects a specific field order for `RFQQuoteType`. If you encode in the wrong order, the indexer may reject with "rfq_id is required" or similar. Match the canonical order:

| Field # | Name | Type | Notes |
|---------|------|------|------|
| 1 | chain_id | string | Wire-only — not bound by v2 signature |
| 2 | contract_address | string | Wire-only — not bound by v2 signature |
| 3 | market_id | string | |
| 4 | rfq_id | uint64 | |
| 5 | taker_direction | string | `"long"` or `"short"` |
| 6 | margin | string | FPDecimal |
| 7 | quantity | string | FPDecimal |
| 8 | price | string | FPDecimal — must equal the signed price byte-for-byte |
| 9 | expiry | RFQExpiryType | nested: `timestamp` (uint64 ms) or `height` (uint64) |
| 10 | maker | string | |
| 11 | taker | string | |
| 12 | signature | string | hex with `0x`, 65 bytes (r‖s‖v) |
| 19 | maker_subaccount_nonce | uint32 | included in v2 digest as `makerSubaccountNonce` |
| 20 | min_fill_quantity | string | optional — included in v2 digest as `"0"` if absent |
| 23 | **sign_mode** | string | **Required.** `"v2"` for everything this client signs. |

---

## Contract Expectations

### FPDecimal

All numeric fields (margin, quantity, price, worst_price) use **FPDecimal**: human-readable decimal strings in JSON. Examples: `"5"`, `"5.1"`, `"1.461"`. Do not send 1e6-scaled integers.

### Worst Price vs Mark

- **Long:** `worst_price` must be ≤ `mark_price × 1.1` (10% slippage)
- **Short:** `worst_price` must be ≥ `mark_price × 0.9`

Fetch mark price from the chain LCD endpoint and set worst_price accordingly:

```
GET {lcd}/injective/exchange/v1beta1/derivative/markets/{market_id}
```

The response includes `perpetualMarketInfo.markPrice` (or similar field depending on the market type).

### Market Tick Sizes and Minimum Notional

> **Critical pitfall:** Every price and quantity used in a quote or conditional order MUST be quantized to the market's tick sizes **before** you sign it. The signed value and the value sent in AcceptQuote (and to the indexer) must be byte-for-byte identical. If you sign an unquantized price and then quantize before sending, the signature verification will fail. If you skip quantization entirely, the exchange will reject the synthetic trade with an error such as `"invalid price tick"` — even though the signature was valid.
>
> **Correct order:** compute price → quantize to tick → sign → send
> **Wrong order:**   compute price → sign → quantize → send  ← signature mismatch!

Each derivative market has tick size constraints:

| Parameter | Description |
|---|---|
| `min_price_tick_size` | Prices must be exact multiples of this value |
| `min_quantity_tick_size` | Quantities must be exact multiples of this value |
| Minimum notional | Some markets enforce `price × quantity ≥ min_notional` |

**Fetching tick sizes from the chain:**

```
GET {lcd}/injective/exchange/v2/derivative/markets/{market_id}
```

Inside the response look for `market.market.min_price_tick_size` and `market.market.min_quantity_tick_size`. These are returned as human-readable decimal strings (not scaled by 1e18).

**Using the SDK helpers (MM quote price):**

The rounding direction for the MM's quote price matters. The contract checks:
- For LONG direction (taker buys): `mm_price ≤ taker_worst_price` → MM should round **DOWN** (`ROUND_FLOOR`) to stay within the taker's limit
- For SHORT direction (taker sells): `mm_price ≥ taker_worst_price` → MM should round **UP** (`ROUND_CEILING`) to stay above the taker's floor

```python
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from rfq_test.utils.price import (
    get_market_tick_sizes,
    quantize_to_tick,
    quantize_quantity,
)

# Fetch once per session (PriceFetcher also caches this as a side-effect)
ticks = await get_market_tick_sizes(lcd_endpoint, market_id)
price_tick = ticks.get("min_price_tick")  # e.g. Decimal("0.001")
qty_tick   = ticks.get("min_qty_tick")    # e.g. Decimal("0.001")

# Apply before signing — NOT after
spread = mark_price * Decimal("0.005")  # 0.5% spread

# MM's quote price: direction-aware rounding
if direction == "long":   # taker buys → MM sells at higher price
    raw_price = mark_price + spread
    quote_price = quantize_to_tick(raw_price, price_tick, rounding=ROUND_FLOOR)   # stay ≤ worst_price
else:                     # taker sells → MM buys at lower price
    raw_price = mark_price - spread
    quote_price = quantize_to_tick(raw_price, price_tick, rounding=ROUND_CEILING)  # stay ≥ worst_price

# Quantities always floor-round (never exceed taker's requested amount)
quantity = quantize_quantity(raw_quantity, qty_tick)

# Now sign using these quantized strings — SAME strings sent in the proto message
signature = sign_quote_v2(
    price=quote_price,         # ← quantized, signed as keccak256(utf8(s))
    maker_quantity=quantity,   # ← quantized
    evm_chain_id=evm_chain_id,
    verifying_contract_bech32=contract_address,
    ...
)
# Send quote_price and quantity unchanged in the RFQQuoteType proto message
# along with sign_mode="v2".
```

**If you don't use the helpers**, implement the quantization yourself:

```python
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

def quantize_to_tick(value, tick_size, rounding=ROUND_FLOOR):
    d = Decimal(str(value))
    t = Decimal(str(tick_size))
    quantized = (d / t).to_integral_value(rounding=rounding) * t
    return format(quantized.normalize(), "f")

# LONG direction: round down so MM's price ≤ taker's worst_price
quote_price = quantize_to_tick(raw_price, "0.001", rounding=ROUND_FLOOR)
# sign quote_price, then send quote_price unchanged
```

**Minimum notional check:**

Some markets require `price × quantity ≥ min_notional`. Verify before sending:

```python
if Decimal(quote_price) * Decimal(quantity) < min_notional:
    raise ValueError("Order below minimum notional — increase price or quantity")
```

`min_notional` is available in the same market info endpoint under `market.market.minNotional` (may appear as `min_notional` in v2).

### Direction

Use `"long"` or `"short"` (lowercase string) in JSON messages.

### Partial Fill and Unfilled Action

If the taker requests quantity X and the MM only quotes Y < X, the contract can:
1. Settle Y with the MM
2. Post (X − Y) to the orderbook if the taker provides `unfilled_action` (e.g. `{"market": {}}` or `{"limit": {"price": "..."}}`)

---

## Conditional Orders (TP/SL)

Conditional orders let a taker pre-sign a trade that executes automatically when the mark price crosses a threshold. This enables take-profit and stop-loss strategies without requiring the taker to be online.

The RFQ indexer monitors mark prices and triggers the order when the condition is met — it then acts as the RFQ requester on the taker's behalf.

### Trigger Types

| Trigger type | Fires when | Typical use |
|---|---|---|
| `mark_price_gte` | mark price ≥ trigger_price | Take-profit for short, stop-loss for long |
| `mark_price_lte` | mark price ≤ trigger_price | Take-profit for long, stop-loss for short |

### Key Fields

| Field | Type | Description |
|---|---|---|
| `version` | uint8 | Protocol version — use `1` |
| `chain_id` | string | Cosmos chain ID (wire-only — not bound by v2 signature) |
| `contract_address` | string | RFQ contract address (wire-only — bound via the v2 domain separator) |
| `taker` | string | Taker's Injective address |
| `epoch` | uint64 | Incremented by `CancelAllIntents`. Start at `1`; increment after each global cancel. |
| `rfq_id` | uint64 | Unique order ID — use current Unix timestamp in ms |
| `market_id` | string | Derivative market ID (0x hex) |
| `subaccount_nonce` | uint32 | Subaccount index (default `0`) |
| `lane_version` | uint64 | Incremented by `CancelIntentLane`. Start at `1`; increment after each per-market cancel. |
| `deadline_ms` | uint64 | Order expiry as Unix ms timestamp. Maximum 30 days from creation. |
| `direction` | string | `"long"` or `"short"` |
| `quantity` | string | Order quantity (FPDecimal) |
| `margin` | string | Must be `"0"` for reduce-only (close-position) orders |
| `worst_price` | string | Worst acceptable fill price |
| `min_total_fill_quantity` | string | Minimum quantity that must be filled for the order to settle |
| `trigger_type` | string | `"mark_price_gte"`, `"mark_price_lte"`, or `"immediate"` |
| `trigger_price` | string | Price threshold (use `"0"` for `immediate`) |
| `unfilled_action` | string/null | Optional — wire-only, **not** bound by the v2 signature |
| `cid` | string/null | Optional client ID. Bound by the v2 signature. |
| `allowed_relayer` | string/null | Optional. Bound by the v2 signature. |
| `sign_mode` | string | **Required on the wire.** Use `"v2"`. |

### Signing a Conditional Order (v2)

`SignedTakerIntent` mirrors `SignQuote` — same domain separator, custom typed-data digest, secp256k1 sign of the digest. The wire `unfilled_action` field is **not** part of the digest (the indexer fixes `unfilledActionKind=0` and `unfilledActionPrice="0"` in the signed bytes), so post-trigger behaviour can change without invalidating the signature.

Use the library:

```python
from rfq_test.crypto.eip712 import sign_conditional_order_v2

signature = sign_conditional_order_v2(
    private_key=PRIVATE_KEY,
    evm_chain_id=evm_chain_id,
    verifying_contract_bech32=contract_address,
    version=1,
    taker=taker_address,
    epoch=1,
    rfq_id=rfq_id,
    market_id=MARKET_ID,
    subaccount_nonce=0,
    lane_version=1,
    deadline_ms=deadline_ms,
    direction="short",
    quantity="1",
    margin="0",                       # reduce-only
    worst_price="132",
    min_total_fill_quantity="1",
    trigger_type="mark_price_gte",    # or "mark_price_lte" / "immediate"
    trigger_price="120",
    cid=None,
    allowed_relayer=None,
)
```

The `SignedTakerIntent` typed-data layout (mirrors `eip712.go`):

```
SignedTakerIntent(
  uint8   version,
  address taker,
  uint64  epoch,
  uint64  rfqId,
  string  marketId,
  uint32  subaccountNonce,
  uint64  laneVersion,
  uint64  deadlineMs,
  uint8   direction,                 // 0=long, 1=short
  string  quantity,
  string  margin,
  string  worstPrice,
  string  minTotalFillQuantity,
  uint8   triggerKind,               // 0=immediate, 1=mark_price_gte, 2=mark_price_lte
  string  triggerPrice,              // "0" for immediate
  uint8   unfilledActionKind,        // hardcoded 0 (none)
  string  unfilledActionPrice,       // hardcoded "0"
  string  cid,                       // "" if null (still hashed)
  address allowedRelayer             // zero-address if null
)
```

### Submitting via TakerStream (WebSocket)

Use `message_type = "conditional_order"` with the order in field 3, signature in field 4, **and `conditional_order_sign_mode = "v2"` in field 5** (required since 2026-04-29). The library's `send_conditional_order` defaults to `sign_mode="v2"`.

```python
from rfq_test.clients.websocket import TakerStreamClient

order_body = {
    "version": 1, "chain_id": chain_id, "contract_address": contract_address,
    "taker": taker_address, "epoch": 1, "rfq_id": rfq_id,
    "market_id": MARKET_ID, "subaccount_nonce": 0, "lane_version": 1,
    "deadline_ms": deadline_ms, "direction": "short",
    "quantity": "1", "margin": "0", "worst_price": "132",
    "min_total_fill_quantity": "1",
    "trigger_type": "mark_price_gte", "trigger_price": "120",
    "unfilled_action": None, "cid": None, "allowed_relayer": None,
}

async with TakerStreamClient(ws_base_url, request_address=taker_address) as client:
    result = await client.send_conditional_order(
        order_body=order_body,
        signature=signature,           # from sign_conditional_order_v2 above
        wait_for_ack=True,
        # sign_mode="v2" is the default
    )
    print(f"ACK: rfq_id={result['rfq_id']} status={result['status']}")
```

### Submitting via REST API

```python
import httpx

async with httpx.AsyncClient() as http:
    resp = await http.post(
        f"{indexer_http_endpoint}/v1/conditionalOrder",
        json={
            "order": order_body,
            "signature": signature,
            "sign_mode": "v2",         # required — same string the digest expects
        },
    )
    resp.raise_for_status()
    print(resp.json())
```

### Listing Active Orders

```python
async with httpx.AsyncClient() as http:
    resp = await http.get(
        f"{indexer_http_endpoint}/conditionalOrders",
        params={"taker": taker_address},
    )
    orders = resp.json()
```

### Cancellation

There are two on-chain cancellation methods. Both work by incrementing a counter that invalidates any orders signed with the old counter value.

**CancelIntentLane** — cancels all orders for one `(taker, market_id, subaccount_nonce)` lane:

```python
from rfq_test.clients.contract import ContractClient

client = ContractClient(contract_config, chain_config)
tx_hash = await client.cancel_intent_lane(
    private_key=PRIVATE_KEY,
    market_id=MARKET_ID,
    subaccount_nonce=0,
)
```

After this call, increment `lane_version` by 1 in all future orders for this market lane.

**CancelAllIntents** — cancels all orders across every market for the taker:

```python
tx_hash = await client.cancel_all_intents(private_key=PRIVATE_KEY)
```

After this call, increment `epoch` by 1 in all future conditional orders.

### epoch and lane_version Tracking

- Start `epoch` at `1`. Increment by 1 after each `CancelAllIntents` call.
- Start `lane_version` at `1`. Increment by 1 after each `CancelIntentLane` call on that market.
- The indexer tracks the current values on-chain. Orders signed with a stale `epoch` or `lane_version` are rejected.

### See Also

`scripts/conditional_order_example.py` in this repo demonstrates the full create → list → cancel flow.

---

### Never Trust a Tx Hash Alone

After broadcasting a transaction, check the response:

```python
result = await broadcaster.broadcast([msg])
tx_response = result.txResponse  # or result.tx_response
code = getattr(tx_response, "code", 0)
if code != 0:
    raw_log = getattr(tx_response, "rawLog", "") or getattr(tx_response, "raw_log", "")
    raise Exception(f"Tx failed: code={code} raw_log={raw_log}")
```

### Validation Errors

- **Indexer:** Returns stream errors (e.g. `quote_failed: ...`) before closing the stream. Log the error message.
- **Contract:** Returns error in `rawLog` on non-zero code. Parse it for the cause (signature, slippage, maker not registered, etc.).

---

## Production Tips

- **Async I/O:** Use `asyncio` and `websockets` for WebSocket connections. Blocking calls will hurt latency.
- **Retries:** Implement retries for transient failures (timeouts, connection drops). Use exponential backoff.
- **Connection lifecycle:** Reconnect on close. Handle stream errors and re-establish the stream.
- **Logging:** Log the exact JSON you sign, and the exact payload you send. This helps debug signature and proto mismatches.
- **Rate limiting:** Respect indexer and chain rate limits. Don't blast requests.

---

## Quick Reference

| Topic | Do | Don't |
|-------|----|-------|
| **Grants** | Use gas heuristics; both MsgSend + MsgPrivilegedExecuteContract for MM and Retail; expiration: null; GenericAuthorization | Use simulation for grants; use SendAuthorization; grant only MsgSend for Retail |
| **v2 Signing** | Use `sign_quote_v2` / `sign_conditional_order_v2`; bind via EIP-712 domain (chainId=1439 testnet, 1776 mainnet, verifyingContract = bech32→evm); quantize prices BEFORE signing (decimals are hashed as `keccak256(utf8(s))`); lowercase direction | Reorder fields; sign unquantized prices; build `eth_signTypedData_v4` payloads (it's a custom typed-data layout) |
| **Wire payload** | Set `sign_mode: "v2"` on every quote and conditional order; include `maker_subaccount_nonce` + `min_fill_quantity` exactly as signed; signature with `0x` prefix | Omit `sign_mode` (indexer rejects empty values); send a different price/qty than you signed |
| **Indexer** | `request_address` header for TakerStream; `maker_address` + optional subscription headers for MakerStream; `"long"`/`"short"` strings | Use numeric direction; omit required headers |
| **Contract** | FPDecimal strings; worst_price within 10% of mark; prices quantized to `min_price_tick_size` before signing; check `tx_response.code` | Use 1e6 integers; ignore tick sizes; sign then quantize; assume tx success from hash only |
| **Errors** | Check `code == 0`; read `rawLog` on failure; treat signing-mode validation errors as missing or unsupported `sign_mode` in your client | Assume success from tx hash |
| **Conditional Orders** | Use `sign_conditional_order_v2`; `margin="0"` for reduce-only; track `epoch` / `lane_version`; pass `sign_mode="v2"` (default) on TakerStream + REST; `unfilled_action` is wire-only (not in digest) | Flat trigger fields; non-zero margin; reuse stale `epoch`/`lane_version` after cancel; assume `unfilled_action` is signed |

---

## Dependencies (Standalone)

```
pyinjective>=1.0.0
websockets>=12.0
eth-account>=0.11.0
eth-hash[pycryptodome]>=0.5.0
protobuf>=4.0
```

---
