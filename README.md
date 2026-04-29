# RFQ Test Scripts

Python library and example scripts for interacting with the Injective RFQ system. Includes WebSocket streaming (gRPC-web), quote signing, wallet management, and on-chain settlement.

**[Python Guide: Building RFQ Market Making & Retail Tools](PYTHON_BUILDING_GUIDE.md)** — For teams building standalone MM or retail scripts. Covers grant creation, quote signing, indexer integration, contract expectations, and production tips. Use it as a guide and apply your own judgment.

## Overview

```
src/rfq_test/          # Core library
  ├── clients/         # WebSocket (MakerStream/TakerStream), Chain, Contract clients
  ├── crypto/          # Quote signing (keccak256 + secp256k1), wallet management
  ├── proto/           # Protobuf message definitions (gRPC-web framing)
  ├── actors/          # High-level MM, Retail, Admin actors
  ├── models/          # Type definitions and config models
  ├── factories/       # Quote, request, and wallet factories
  ├── utils/           # Helpers (price, formatting, retry, logging)
  ├── config.py        # Settings and environment config loader
  └── exceptions.py    # Custom exceptions

configs/               # Environment configs (testnet, local)
scripts/               # Setup scripts (authz grants, maker registration, funding)
                       # conditional_order_example.py — TP/SL order lifecycle example
examples/              # Standalone test scripts
```

### Key Features

- **RFQ quoting:** MakerStream WebSocket for receiving requests and sending quotes
- **Conditional orders (TP/SL):** TakerStream WebSocket + REST API for trigger-based orders that execute automatically when mark price crosses a threshold
- **On-chain settlement:** AcceptQuote, CancelIntentLane, CancelAllIntents
- **Quote signing:** keccak256 + secp256k1 with the exact field order required by the contract

## Regenerating Proto Code

The Python protobuf bindings in `src/rfq_test/proto/` are generated from `injective_rfq_rpc.proto`. After updating the `.proto` file, regenerate them using the venv:

```bash
.venv/bin/python -m grpc_tools.protoc \
  -I src/rfq_test/proto \
  --python_out=src/rfq_test/proto \
  --grpc_python_out=src/rfq_test/proto \
  src/rfq_test/proto/injective_rfq_rpc.proto
```

This overwrites `injective_rfq_rpc_pb2.py` and `injective_rfq_rpc_pb2_grpc.py`. After regenerating, review any field or type changes in the proto and update `src/rfq_test/clients/websocket.py` accordingly (especially message type names, field types for `expiry`, and nested message structures).

`grpcio-tools` is a dev dependency. Make sure it's installed in your venv (`pip install -e ".[dev]"`) before running the command.

## Quick Start

### 1. Install

```bash
pip install -e .
```

Or install dependencies directly:

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your private keys
```

Set the environment:
```bash
export RFQ_ENV=testnet
```

### 3. Setup (One-Time)

**Grant authz permissions** to the RFQ contract:
```bash
python scripts/setup_authz_grants.py
```

**Register as a maker** (requires admin):
```bash
python scripts/register_makers.py
```

### 4. Run Examples

**Derive wallet from mnemonic:**
```bash
python examples/derive_key.py your twelve word mnemonic phrase here
```

**Full WebSocket round-trip** (retail request → MM quote → retail receives quote):
```bash
python examples/test_roundtrip.py
```

**End-to-end settlement** (includes on-chain `AcceptQuote`):
```bash
python examples/test_settlement.py
```

## Environment Configuration

| Item | Testnet | Mainnet |
|------|---------|---------|
| Cosmos chain ID | `injective-888` | `injective-1` |
| EVM chain ID (EIP-712 domain) | `1439` | `1776` |
| RFQ Contract | `inj1qw7jk82hjvf79tnjykux6zacuh9gl0z0wl3ruk` | _TBA_ |
| MakerStream WSS | `wss://testnet.rfq.ws.injective.network/injective_rfq_rpc.InjectiveRfqRPC/MakerStream` | _TBA_ |
| TakerStream WSS | `wss://testnet.rfq.ws.injective.network/injective_rfq_rpc.InjectiveRfqRPC/TakerStream` | _TBA_ |
| Chain gRPC | `testnet-grpc.injective.dev:443` | `sentry.chain.grpc.injective.network:443` |
| Faucet | `https://testnet-faucet.injective.dev` | n/a |

## Protocol

The RFQ Indexer uses **gRPC-web over WebSocket** with protobuf framing:

- **Subprotocol:** `grpc-ws`
- **Framing:** `[1 byte flags][4 bytes length BE][protobuf payload]`
- **Keep-alive:** Send `ping` message every 1 second
- **Signing:** EIP-712 v2 (`SignQuote` / `SignedTakerIntent` typed-data digest → secp256k1 sign). Every quote and conditional order must carry `sign_mode: "v2"` — the indexer rejects empty values with `value of message.sign_mode must be one of "v1", "v2"`. v1 (raw-JSON keccak256) still exists in the contract / indexer for legacy clients but the rfq-testing repo signs v2 only. Spec lives in [`src/rfq_test/crypto/eip712.py`](src/rfq_test/crypto/eip712.py); the Go reference is in `injective-indexer` at `service/rfq/signature/eip712.go`.

### Maker Update Subscriptions

When a maker connects to `MakerStream`, it can set these WebSocket headers:

- `maker_address`: maker's Injective bech32 address
- `subscribe_to_quotes_updates: true`
- `subscribe_to_settlement_updates: true`

These subscriptions are maker-scoped:

- **Quote updates:** the maker receives `quote_update` events for quotes whose `maker` matches `maker_address`.
- **Settlement updates:** the maker receives `settlement_update` events when a settlement includes at least one quote from `maker_address`, even if that quote was not the one used for execution.

For quote updates:

- `status="accepted"` means that quote was used in settlement.
- `status="rejected"` means the quote was included in evaluation but not used.
- `executed_quantity` and `executed_margin` contain the actually executed portion for that quote.

Python client example:

```python
from rfq_test.clients.websocket import MakerStreamClient

mm_client = MakerStreamClient(
    ws_url,
    maker_address=maker_inj_address,
    subscribe_to_quotes_updates=True,
    subscribe_to_settlement_updates=True,
)
```

> **Quote signing note (v2 EIP-712):** Build the `SignQuote` typed-data digest (chainId / verifyingContract bound by the domain separator) and sign it with secp256k1 raw — no EIP-191 prefix, no JSON canonicalisation. Decimal fields are encoded as `keccak256(utf8(s))`, so the wire price MUST equal the signed price byte-for-byte (quantize to the market tick BEFORE signing). See [PYTHON_BUILDING_GUIDE.md — Quote Signing (v2)](PYTHON_BUILDING_GUIDE.md#quote-signing-v2) for the full recipe and `src/rfq_test/crypto/eip712.py` for the byte-compatible Python implementation.

### Conditional Orders (TP/SL)

Takers can submit trigger-based orders via the TakerStream WebSocket (`message_type: "conditional_order"`) or via the REST API (`POST /conditionalOrder`). The indexer monitors mark prices and executes the order automatically when the trigger price is crossed.

See [PYTHON_BUILDING_GUIDE.md — Conditional Orders](PYTHON_BUILDING_GUIDE.md#conditional-orders-tpsl) and `scripts/conditional_order_example.py` for a complete example.

### Supported Markets (Testnet)

| Symbol | Market ID | Quote denom |
|--------|-----------|-------------|
| INJ/USDC PERP  | `0xdc70164d7120529c3cd84278c98df4151210c0447a65a2aab03459cf328de41e` | `erc20:0x0C382e685bbeeFE5d3d9C29e29E341fEE8E84C5d` (USDC) |
| BTC/USDC PERP  | `0xfd704649cf3a516c0c145ab0111717c44640d8dbe52a462ae35cadf2f6df1515` | same |
| LINK/USDC PERP | `0xdbb9bb072015238096f6e821ee9aab7affd741f8662a71acc14ac30ee6b687a5` | same |
| ETH/USDC PERP  | `0x135de28700392fb1c17d40d5170a74f30055a4ad522feddafec42fbbbb780897` | same |

Tick: `0.01` on both price and quantity for all four.

## License

See [LICENSE](LICENSE) for details.
