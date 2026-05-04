# RFQ Integration Examples

Code examples for integrating with the Injective RFQ system. Each example demonstrates the Market Maker (MM) and/or Retail (Taker) flow.

## Examples by Language

Each language has both **WebSocket** and **gRPC** variants of the MM/Retail flow. For the current public testnet deployment, the docs-verified end-to-end path is gRPC-web over WebSocket via `test_settlement.py`, including MakerStream auth.

| Directory | Language | Role | Transport | Description |
|-----------|----------|------|-----------|-------------|
| `ts-mm/main.ts` | TypeScript | Market Maker | WebSocket | MM quote streaming via WebSocket |
| `ts-mm/main-grpc.ts` | TypeScript | Market Maker | gRPC | MM quote streaming via gRPC MakerStream |
| `ts-retail/main.ts` | TypeScript | Retail (Taker) | WebSocket | RFQ request, quote aggregation, on-chain acceptance |
| `ts-retail/main-grpc.ts` | TypeScript | Retail (Taker) | gRPC | Same flow using gRPC Request + StreamQuote |
| `go-mm/main/main.go` | Go | Market Maker | WebSocket | MM quote streaming via WebSocket |
| `go-mm/main-grpc/main.go` | Go | Market Maker | gRPC | MM quote streaming via gRPC MakerStream |
| `python-mm/main.py` | Python | Market Maker | WebSocket | MM quote streaming via WebSocket |
| `python-mm/main-grpc.py` | Python | Market Maker | gRPC | MM quote streaming via gRPC MakerStream |

## Python Test Scripts

| File | Description |
|------|-------------|
| `test_roundtrip.py` | End-to-end RFQ roundtrip test (WebSocket) |
| `test_settlement.py` | Full E2E settlement test (WebSocket, MakerStream auth, on-chain settlement) |
| `test_settlement_grpc.py` | Full E2E settlement test (gRPC) |
| `derive_key.py` | Key derivation utility |

## Getting Started

### TypeScript

```bash
cd examples/
cp .env.example .env
# Edit .env with your private keys and contract address
npm install
npm run ts-mm-setup    # One-time MM setup
npm run ts-mm          # Run MM quote bot (WebSocket)
npm run ts-mm-grpc     # Run MM quote bot (gRPC)
npm run ts-retail      # Run retail flow (WebSocket)
npm run ts-retail-grpc # Run retail flow (gRPC)
```

### Go

```bash
cd examples/go-mm/
cp ../.env.example .env
# Edit .env with your private keys

# WebSocket
go run setup/setup.go  # One-time MM setup
go run main/main.go    # Run MM quote bot (WebSocket)

# gRPC (requires proto code generation first)
cd proto && ./generate.sh && cd ..
go run main-grpc/main.go  # Run MM quote bot (gRPC)
```

### Python (MM)

```bash
cd examples/python-mm/
cp ../.env.example .env
# Edit .env with your private keys
pip3 install injective-py eth-keys websockets eth-account python-dotenv grpcio

python3 setup.py          # One-time MM setup
python3 main.py           # Run MM quote bot (WebSocket)
python3 main-grpc.py      # Run MM quote bot (gRPC)
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
| `GRPC_ENDPOINT` | gRPC endpoint (e.g. `localhost:9910`) — required for gRPC examples |

## Architecture

For a deeper dive into the MM SDK architecture and pricing engine patterns, see [MM_SDK_ARCHITECTURE.md](./MM_SDK_ARCHITECTURE.md).
