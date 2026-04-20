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

## Quote Signing

### SignQuote Payload (Contract Verification)

The contract verifies the maker's signature by building a JSON payload and hashing it with **keccak256**. The payload must match exactly — wrong field order or missing fields will cause signature rejection.

### Field Order and Keys

> **IMPORTANT:** The field `ms` (maker_subaccount_nonce) is required and must appear between `m` and `mq`. Omitting it or placing it elsewhere will cause the contract to reject the signature.

| Key | Field | Type | Notes |
|-----|-------|------|-------|
| `c` | chain_id | string | |
| `ca` | contract_address | string | |
| `mi` | market_id | string | |
| `id` | rfq_id | number | |
| `t` | taker | string | Injective address |
| `td` | taker_direction | string | `"long"` or `"short"` |
| `tm` | taker_margin | string | FPDecimal |
| `tq` | taker_quantity | string | FPDecimal |
| `m` | maker | string | Injective address |
| `ms` | maker_subaccount_nonce | number | Required. Use `0` if not using subaccounts. |
| `mq` | maker_quantity | string | FPDecimal |
| `mm` | maker_margin | string | FPDecimal |
| `p` | price | string | FPDecimal |
| `e` | expiry | object | `{"ts": <unix_ms>}` or `{"h": <block_height>}` |
| `mfq` | min_fill_quantity | string | Optional V2 field. Append at end only when needed. |

**Order matters.** Serialize with `json.dumps(..., separators=(",", ":"))` — no spaces, no `sort_keys`. The contract hashes this exact JSON string.

### Standalone Signing Example

```python
import json
from eth_account import Account
from eth_hash.auto import keccak

def sign_quote(
    private_key: str,
    chain_id: str,
    contract_address: str,
    rfq_id: int,
    market_id: str,
    direction: str,           # "long" or "short"
    taker: str,
    taker_margin: str,
    taker_quantity: str,
    maker: str,
    maker_margin: str,
    maker_quantity: str,
    price: str,
    expiry: int,              # Unix timestamp in milliseconds
    min_fill_quantity: str = None,     # optional V2 field
) -> str:
    """Sign a quote. Returns hex signature (without 0x prefix).

    Field order matches ws-client's canonical buildSignQuote:
      c, ca, mi, id, t, td, tm, tq, m, mq, mm, p, e [, mfq]
    Do NOT add an `ms` (maker_subaccount_nonce) field — the indexer's
    legacy + v2 verifiers both reject payloads that include it.
    """
    payload = {
        "c": chain_id,
        "ca": contract_address,
        "mi": market_id,
        "id": rfq_id,
        "t": taker,
        "td": direction.lower(),
        "tm": taker_margin,
        "tq": taker_quantity,
        "m": maker,
        "mq": maker_quantity,
        "mm": maker_margin,
        "p": price,
        "e": {"ts": expiry},
    }
    if min_fill_quantity is not None:
        payload["mfq"] = min_fill_quantity   # append at end, only when needed

    json_str = json.dumps(payload, separators=(",", ":"))
    message_hash = keccak(json_str.encode("utf-8"))

    if private_key.startswith("0x"):
        private_key = private_key[2:]
    account = Account.from_key(bytes.fromhex(private_key))
    sig = account.unsafe_sign_hash(message_hash)
    sig_bytes = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v])
    return sig_bytes.hex()
```

### Price and Decimal Format

- Use **human-readable decimal strings** (e.g. `"4.5"`, `"1.461"`). No trailing zeros — `"4.50"` and `"4.5"` produce different hashes.
- Do **not** use 1e6-scaled integers.
- The price in the signed payload must be **byte-for-byte identical** to the price you send in `AcceptQuote`. If they differ, signature verification fails.

### maker_subaccount_nonce

`maker_subaccount_nonce` is the subaccount index used when your address was registered as a maker. If registration was done with the default subaccount (nonce 0), use `0`. Contact the platform operator if you are unsure what nonce was used.

### Signature for Indexer

When sending a quote to the **indexer** (MakerStream), the indexer expects the signature in **hex with `0x` prefix**. If your signing function returns hex without `0x`, prepend it:

```python
sig_hex = sign_quote(...)
if not sig_hex.startswith("0x"):
    sig_hex = "0x" + sig_hex
# Use sig_hex when building the quote for the indexer
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

| Field # | Name | Type |
|---------|------|------|
| 1 | chain_id | string |
| 2 | contract_address | string |
| 3 | market_id | string |
| 4 | rfq_id | uint64 |
| 5 | taker_direction | string |
| 6 | margin | string |
| 7 | quantity | string |
| 8 | price | string |
| 9 | expiry | RFQExpiryType (nested: timestamp, height) |
| 10 | maker | string |
| 11 | taker | string |
| 12 | signature | string (hex with 0x) |
| ... | (status, timestamps, etc.) | |

The field table above is the canonical reference for quote encoding.

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
signature = sign_quote(
    price=quote_price,      # ← quantized
    maker_quantity=quantity, # ← quantized
    ...
)
# Send quote_price and quantity unchanged in the RFQQuoteType proto message
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
| `version` | int | Protocol version — use `1` |
| `chain_id` | string | Chain ID (e.g. `"injective-888"`) |
| `contract_address` | string | RFQ contract address |
| `taker` | string | Taker's Injective address |
| `epoch` | int | Incremented by `CancelAllIntents`. Start at `1`; increment after each global cancel. |
| `rfq_id` | int | Unique order ID — use current Unix timestamp in ms |
| `market_id` | string | Derivative market ID (0x hex) |
| `subaccount_nonce` | int | Subaccount index (default `0`) |
| `lane_version` | int | Incremented by `CancelIntentLane`. Start at `1`; increment after each per-market cancel. |
| `deadline_ms` | int | Order expiry as Unix ms timestamp. Maximum 30 days from creation. |
| `direction` | string | `"long"` or `"short"` |
| `quantity` | string | Order quantity (FPDecimal) |
| `margin` | string | Must be `"0"` for v1 — these are reduce-only (close-position) orders |
| `worst_price` | string | Worst acceptable fill price |
| `min_total_fill_quantity` | string | Minimum quantity that must be filled for the order to settle |
| `trigger_type` | string | `"mark_price_gte"` or `"mark_price_lte"` |
| `trigger_price` | string | Price threshold that triggers the order |
| `unfilled_action` | string/null | Optional: action for any unfilled quantity after trigger |
| `cid` | string/null | Optional client ID for tracking |
| `allowed_relayer` | string/null | Optional: restrict which address can relay this order |

### Signing a Conditional Order

The canonical JSON field order is fixed — do not reorder. The `"trigger"` field is a **nested object** where the key is the trigger type and the value is the price string.

```python
import json
import time
from decimal import Decimal
from eth_account import Account
from eth_hash.auto import keccak

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
    direction: str,           # "long" or "short"
    quantity: str,
    worst_price: str,
    min_total_fill_quantity: str,
    trigger_type: str,        # "mark_price_gte" or "mark_price_lte"
    trigger_price: str,
    margin: str = "0",        # must be "0" for v1 orders
    unfilled_action=None,
    cid=None,
    allowed_relayer=None,
) -> str:
    """Sign a conditional order. Returns "0x" + hex signature (65 bytes)."""
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
        "direction": direction.lower(),
        "quantity": quantity,
        "margin": margin,
        "worst_price": worst_price,
        "min_total_fill_quantity": min_total_fill_quantity,
        "trigger": {trigger_type: trigger_price},   # nested object, not flat fields
        "unfilled_action": unfilled_action,
        "cid": cid,
        "allowed_relayer": allowed_relayer,
    }
    payload = json.dumps(canonical, separators=(",", ":"))
    message_hash = keccak(payload.encode("utf-8"))

    if private_key.startswith("0x"):
        private_key = private_key[2:]
    account = Account.from_key(bytes.fromhex(private_key))
    sig = account.unsafe_sign_hash(message_hash)
    sig_bytes = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v])
    return "0x" + sig_bytes.hex()
```

### Submitting via TakerStream (WebSocket)

Use `message_type = "conditional_order"` with the order in field 3 and signature in field 4. The server responds with a `conditional_order_ack` message.

```python
from rfq_test.clients.websocket import TakerStreamClient
from rfq_test.crypto.signing import sign_conditional_order

rfq_id = int(time.time() * 1000)
deadline_ms = rfq_id + 24 * 60 * 60 * 1000   # 24 hours from now

signature = sign_conditional_order(
    private_key=PRIVATE_KEY,
    version=1,
    chain_id=CHAIN_ID,
    contract_address=CONTRACT_ADDRESS,
    taker=taker_address,
    epoch=1,
    rfq_id=rfq_id,
    market_id=MARKET_ID,
    subaccount_nonce=0,
    lane_version=1,
    deadline_ms=deadline_ms,
    direction="short",
    quantity="1",
    worst_price="132",
    min_total_fill_quantity="1",
    trigger_type="mark_price_gte",
    trigger_price="120",
    margin="0",
)

order_body = {
    "version": 1, "chain_id": CHAIN_ID, "contract_address": CONTRACT_ADDRESS,
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
        signature=signature,
        wait_for_ack=True,
    )
    print(f"ACK: rfq_id={result['rfq_id']} status={result['status']}")
```

### Submitting via REST API

As an alternative to the WebSocket stream, you can submit via HTTP:

```python
import httpx

async with httpx.AsyncClient() as http:
    resp = await http.post(
        f"{indexer_http_endpoint}/conditionalOrder",
        json={"order": order_body, "signature": signature},
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
| **Signing** | Field order `c, ca, mi, id, t, td, tm, tq, m, ms, mq, mm, p, e`; include `ms` (maker_subaccount_nonce); keccak256; lowercase direction; quantize price before signing | Omit `ms`; use old field order without `ms`; use sort_keys; sign unquantized price |
| **Indexer** | request_address header for TakerStream; maker_address + optional subscription headers for MakerStream; "long"/"short"; signature with 0x prefix; include maker_subaccount_nonce + min_fill_quantity in quote proto | Use numeric direction; omit required headers; skip new proto fields 19/20 |
| **Contract** | FPDecimal strings; worst_price within 10% of mark; prices quantized to min_price_tick_size before signing; check tx_response.code | Use 1e6 integers; ignore tick sizes; sign then quantize; assume tx success from hash only |
| **Errors** | Check code == 0; read rawLog on failure | Assume success from tx hash |
| **Conditional Orders** | Sign with exact field order; use `"trigger": {type: price}` nested object; `margin="0"` for v1; track epoch and lane_version | Flat trigger fields; non-zero margin; reuse stale epoch/lane_version after cancel |

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
