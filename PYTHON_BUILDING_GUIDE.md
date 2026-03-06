# Python Guide: Building RFQ Market Making & Retail Tools

**For teams building standalone MM (Market Maker) or retail trading scripts.**  
This guide helps you avoid common pitfalls and build correctly from day one. You do **not** need the rfq-qa-python-tests framework—everything here is self-contained.

---

## Table of Contents

1. [Who This Is For](#who-this-is-for)
2. [Architecture Overview](#architecture-overview)
3. [Grant Creation (Critical)](#grant-creation-critical)
4. [Quote Signing](#quote-signing)
5. [Indexer Integration (WebSocket)](#indexer-integration-websocket)
6. [Contract Expectations](#contract-expectations)
7. [Error Handling](#error-handling)
8. [Production Tips](#production-tips)
9. [Quick Reference](#quick-reference)

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

The contract verifies the maker's signature by building a JSON payload and hashing it with **keccak256**. The payload must match exactly.

### Field Order and Keys

| Key | Field | Type |
|-----|-------|------|
| `c` | chain_id | string |
| `ca` | contract_address | string |
| `mi` | market_id | string |
| `id` | rfq_id | number |
| `t` | taker | string (Injective addr) |
| `td` | taker_direction | "long" or "short" |
| `tm` | taker_margin | string |
| `tq` | taker_quantity | string |
| `m` | maker | string |
| `mq` | maker_quantity | string |
| `mm` | maker_margin | string |
| `p` | price | string |
| `e` | expiry | object: `{"ts": <timestamp_ms>}` or `{"h": <block_height>}` |

**Order matters.** Serialize with `json.dumps(..., separators=(",", ":"))` and no spaces. Do not use `sort_keys=True`.

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
    direction: str,  # "long" or "short"
    taker: str,
    taker_margin: str,
    taker_quantity: str,
    maker: str,
    maker_margin: str,
    maker_quantity: str,
    price: str,
    expiry: int,  # timestamp in milliseconds
) -> str:
    """Sign a quote. Returns hex signature (without 0x prefix)."""
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
        "e": {"ts": expiry},  # expiry as nested object with timestamp
    }
    json_str = json.dumps(payload, separators=(",", ":"))
    message_hash = keccak(json_str.encode("utf-8"))

    if private_key.startswith("0x"):
        private_key = private_key[2:]
    account = Account.from_key(bytes.fromhex(private_key))
    sig = account.unsafe_sign_hash(message_hash)
    sig_bytes = sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v])
    return sig_bytes.hex()
```

### Price Format

- Use **human-readable decimal strings** (e.g. `"4.5"`, `"1.461"`).
- Do **not** use 1e6-scaled integers.
- The price in the signed payload must be **identical** to the price you send in `AcceptQuote`. If they differ, signature verification fails.

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
- **No metadata** required for connection
- **Receive:** Requests arrive as stream messages
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

Reference: [injective-indexer](https://github.com/InjectiveLabs/injective-indexer) `api/gen/grpc/injective_rfq_rpc/pb/injective_rfq_rpc.proto`.

---

## Contract Expectations

### FPDecimal

All numeric fields (margin, quantity, price, worst_price) use **FPDecimal**: human-readable decimal strings in JSON. Examples: `"5"`, `"5.1"`, `"1.461"`. Do not send 1e6-scaled integers.

### Worst Price vs Mark

- **Long:** `worst_price` must be ≤ `mark_price × 1.1` (10% slippage)
- **Short:** `worst_price` must be ≥ `mark_price × 0.9`

Fetch mark price from the chain (e.g. LCD derivative markets endpoint) and set worst_price accordingly.

### Direction

Use `"long"` or `"short"` (lowercase string) in JSON messages.

### Partial Fill and Unfilled Action

If the taker requests quantity X and the MM only quotes Y < X, the contract can:
1. Settle Y with the MM
2. Post (X − Y) to the orderbook if the taker provides `unfilled_action` (e.g. `{"market": {}}` or `{"limit": {"price": "..."}}`)

---

## Error Handling

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
| **Signing** | Field order c, ca, mi, id, t, td, tm, tq, m, mq, mm, p, e; keccak256; lowercase direction | Use sort_keys; different price in sign vs AcceptQuote; use 0/1 for direction |
| **Indexer** | request_address header for TakerStream; "long"/"short"; signature with 0x prefix; match proto field order | Use numeric direction; omit request_address; wrong proto field order |
| **Contract** | FPDecimal strings; worst_price within 10% of mark; check tx_response.code | Use 1e6 integers; assume tx success from hash only |
| **Errors** | Check code == 0; read rawLog on failure | Assume success from tx hash |

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
