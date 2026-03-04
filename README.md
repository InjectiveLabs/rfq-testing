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
examples/              # Standalone test scripts
```

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

## Testnet Configuration

| Item | Value |
|------|-------|
| Chain ID | `injective-888` |
| RFQ Contract | `inj1t8hyyle68vd0kzsdehxg0sywttrwmt58jzk29q` |
| MakerStream WSS | `wss://testnet.rfq.ws.injective.network/injective_rfq_rpc.InjectiveRfqRPC/MakerStream` |
| TakerStream WSS | `wss://testnet.rfq.ws.injective.network/injective_rfq_rpc.InjectiveRfqRPC/TakerStream` |
| Chain gRPC | `testnet-grpc.injective.dev:443` |
| Faucet | `https://testnet-faucet.injective.dev` |

## Protocol

The RFQ Indexer uses **gRPC-web over WebSocket** with protobuf framing:

- **Subprotocol:** `grpc-ws`
- **Framing:** `[1 byte flags][4 bytes length BE][protobuf payload]`
- **Keep-alive:** Send `ping` message every 1 second
- **Signing:** `keccak256(canonical_json) → secp256k1_sign` (raw hash, NO EIP-191 prefix)

### Supported Markets (Testnet)

| Symbol | Market ID |
|--------|-----------|
| INJ/USDT PERP | `0x17ef48032cb24375ba7c2e39f384e56433bcab20cbee9a7357e4cba2eb00abe6` |
| ATOM/USDT PERP | `0xd97d0da6f6c11710ef06315971250e4e9aed4b7d4cd02059c9477ec8cf243782` |

## License

See [LICENSE](LICENSE) for details.
