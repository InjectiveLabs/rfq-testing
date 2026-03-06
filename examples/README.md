# RFQ Integration Examples

Code examples for integrating with the Injective RFQ system. Each example demonstrates the Market Maker (MM) and/or Retail (Taker) flow.

## Examples by Language

| Directory | Language | Role | Description |
|-----------|----------|------|-------------|
| `ts-mm/` | TypeScript | Market Maker | MM setup + quote streaming via WebSocket |
| `ts-retail/` | TypeScript | Retail (Taker) | RFQ request, quote aggregation, and on-chain acceptance |
| `go-mm/` | Go | Market Maker | MM setup + quote streaming via WebSocket |
| `python-mm/` | Python | Market Maker | MM setup + quote streaming via WebSocket |

## Existing Python Examples

| File | Description |
|------|-------------|
| `test_roundtrip.py` | End-to-end RFQ roundtrip test |
| `test_settlement.py` | Settlement flow test |
| `derive_key.py` | Key derivation utility |

## Getting Started

### TypeScript

```bash
cd examples/
cp .env.example .env
# Edit .env with your private keys and contract address
npm install
npm run ts-mm-setup    # One-time MM setup
npm run ts-mm          # Run MM quote bot
npm run ts-retail      # Run retail flow
```

### Go

```bash
cd examples/go-mm/
cp ../.env.example .env
# Edit .env with your private keys
go run setup/setup.go  # One-time MM setup
go run main/main.go    # Run MM quote bot
```

### Python (MM)

```bash
cd examples/python-mm/
cp ../.env.example .env
# Edit .env with your private keys
pip3 install injective-py eth-keys websockets eth-account python-dotenv
python3 setup.py       # One-time MM setup
python3 main.py        # Run MM quote bot
```

## Environment Variables

See `.env.example` for required configuration:

| Variable | Description |
|----------|-------------|
| `RETAIL_PRIVATE_KEY` | Retail user's private key (hex) |
| `ADMIN_PRIVATE_KEY` | Contract admin's private key (hex) |
| `MM_PRIVATE_KEY` | Market maker's private key (hex) |
| `CONTRACT_ADDRESS` | RFQ contract address |
| `CHAIN_ID` | Injective chain ID |

## Architecture

For a deeper dive into the MM SDK architecture and pricing engine patterns, see [MM_SDK_ARCHITECTURE.md](./MM_SDK_ARCHITECTURE.md).
