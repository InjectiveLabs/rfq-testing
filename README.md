# injective-rfq-toolkit

**The Injective RFQ developer toolkit.** A Python package, generated protobuf stubs, EIP-712 v2 signing primitives, end-to-end test harness, and reference market-maker / retail implementations in Python, TypeScript, and Go — all in one repo, all working against the same testnet contract.

> **Positioning.** This is a *toolkit*: importable client library + signing helpers + generated proto + reference scripts + integration test suite, packaged together. Partners use it three ways:
> 1. **`pip install -e .`** and import `rfq_test` to build a Python bot on top of `MakerStreamClient`, `sign_quote_v2`, etc.
> 2. **Clone the gRPC examples** in `examples/{python,go,ts}-mm/main-grpc.*` as a starting point in their language of choice.
> 3. **Run the test harness** against testnet to verify their own integrations end-to-end.
>
> It is not yet a packaged SDK with semver, PyPI/npm distribution, or formal multi-language API parity — see [§ Roadmap to SDK](#roadmap-to-sdk) at the bottom for what changes between "toolkit" and "SDK".

**Companion guides:**
- [PYTHON_BUILDING_GUIDE.md](PYTHON_BUILDING_GUIDE.md) — full protocol walkthrough for teams that want to build standalone (no `rfq_test` dependency)
- Live HTML docs: [rfq.inj.so/onboarding.html](https://rfq.inj.so/onboarding.html) and [rfq.inj.so/runbook.html](https://rfq.inj.so/runbook.html)

---

## What's in the box

```
src/rfq_test/                # Python package (importable as `rfq_test`)
  ├── clients/               # Network clients
  │   ├── websocket.py       #   TakerStreamClient, MakerStreamClient (auth-handshake aware)
  │   ├── chain.py           #   ChainClient — authz grants, balances, txs
  │   └── contract.py        #   ContractClient — AcceptQuote, CancelIntentLane, CancelAllIntents
  ├── crypto/                # Signing & wallets
  │   ├── eip712.py          #   sign_quote_v2, sign_conditional_order_v2,
  │   │                      #   sign_maker_challenge_v2, domain_separator, bech32_to_evm
  │   └── wallet.py          #   Wallet, mnemonic + address conversion helpers
  ├── proto/                 # gRPC-generated stubs + hand-written gRPC-web framing
  ├── actors/                # High-level orchestration (MarketMaker, RetailUser, Admin)
  ├── models/                # Pydantic types — Request, Quote, Settlement, EnvironmentConfig, …
  ├── factories/             # Builders — RequestFactory, QuoteFactory, WalletFactory
  ├── utils/                 # Decimal canonicalization, retry, logging, price/tick helpers
  ├── config.py              # Env-aware config loader (RFQ_ENV=testnet|mainnet|local)
  └── exceptions.py          # IndexerValidationError, IndexerTimeoutError, …

configs/                     # Per-environment YAML (testnet, local, …)
scripts/                     # Operational scripts — authz grants, maker registration,
                             #   funding, conditional-order demo, signing self-test
examples/                    # End-to-end reference implementations
  ├── test_roundtrip.py      #   Python: retail request → MM quote → retail receives
  ├── test_settlement.py     #     "      + on-chain AcceptQuote (full E2E)
  ├── test_settlement_grpc.py#   Same flow over native gRPC
  ├── taker_multi_quote.py   #   Multiple MMs quoting the same RFQ
  ├── python-mm/main-grpc.py #   Standalone MM bot (no rfq_test dep) — gRPC, auth-handshake
  ├── go-mm/main-grpc/       #   Same bot in Go
  └── ts-mm/main-grpc.ts     #   Same bot in TypeScript
tests/                       # pytest suite — smoke / functional / contract / load / validation
```

### Capabilities at a glance

| Capability | Where it lives | Notes |
|---|---|---|
| **MakerStream WS subscribe + auth handshake** | `clients.websocket.MakerStreamClient` | Auto-signs `MakerChallenge` when given `auth_private_key` + `auth_evm_chain_id` + `auth_contract_address` |
| **TakerStream WS request + ACK + quote collection** | `clients.websocket.TakerStreamClient` | `send_request`, `wait_for_ack`, `collect_quotes`, `send_conditional_order` |
| **Quote signing (EIP-712 v2)** | `crypto.eip712.sign_quote_v2` | 16-field digest including `evmChainId` first; byte-compatible with the Rust contract |
| **Conditional-order signing (TP/SL)** | `crypto.eip712.sign_conditional_order_v2` | 19-field `SignedTakerIntent` digest; supports both blind and taker-bound paths |
| **Auth-handshake signing** | `crypto.eip712.sign_maker_challenge_v2` | 4-field `StreamAuthChallenge` digest; raw `bytes32` nonce, not keccak'd |
| **Decimal canonicalization** | `utils.price.quantize_for_fpdecimal` | Quantize-to-tick + strip-trailing-zeros — what the indexer requires |
| **bech32 ↔ EVM address conversion** | `crypto.eip712.bech32_to_evm` + `crypto.wallet.{eth_to_inj,inj_to_eth}_address` | Used in domain separator and address-typed digest fields |
| **Wallet generation** | `crypto.wallet.Wallet`, `WalletFactory` | From private key, mnemonic, or generated |
| **On-chain settlement** | `clients.contract.ContractClient.accept_quote` | Builds `MsgPrivilegedExecuteContract` with the right wrapping |
| **Conditional-order cancellation** | `clients.contract.ContractClient.{cancel_intent_lane, cancel_all_intents}` | Lane-level vs global epoch bumps |
| **Authz grant orchestration** | `clients.chain.ChainClient.grant_authz` | `GenericAuthorization`, no expiration, gas-heuristic broadcast |
| **Generated proto bindings** | `proto/injective_rfq_rpc_pb2.py` + hand-written `rfq_messages.py` | Includes `MakerChallenge`, `MakerAuth`, conditional-order frames |
| **Test harness** | `tests/`, `factories/`, `utils.scenario`, `actors/` | pytest with smoke/functional/contract/load/validation marks |

### Public API

The package's importable surface today:

```python
# Top-level
from rfq_test import Settings, get_settings, Direction, Quote, Request, Settlement

# Clients
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient
from rfq_test.clients.chain    import ChainClient
from rfq_test.clients.contract import ContractClient

# Signing
from rfq_test.crypto.eip712 import (
    sign_quote_v2,
    sign_conditional_order_v2,
    sign_maker_challenge_v2,
    domain_separator,
    bech32_to_evm,
)
from rfq_test.crypto.wallet import Wallet, eth_to_inj_address, inj_to_eth_address

# Config + actors
from rfq_test.config        import get_environment_config
from rfq_test.models.config import EnvironmentConfig
from rfq_test.actors.market_maker import MarketMaker
from rfq_test.actors.retail       import RetailUser
from rfq_test.actors.admin        import Admin

# Decimal hygiene
from rfq_test.utils.price import quantize_for_fpdecimal, quantize_to_tick
```

> **API stability:** not committed to semver yet. Pin to a commit SHA if you're vendoring. The signing helpers (`sign_quote_v2`, `sign_conditional_order_v2`, `sign_maker_challenge_v2`) are the most stable surface — their digest layouts are locked to the on-chain contract.

---

## Quick start

### 1. Install

```bash
pip install -U pip
pip install -e ".[dev]"
```

Python 3.11+ required.

### 2. Configure

```bash
cp .env.example .env       # edit with your private keys
export RFQ_ENV=testnet     # testnet | mainnet | local
```

The harness reads `TESTNET_MM_PRIVATE_KEY`, `TESTNET_RETAIL_PRIVATE_KEY`, and (for TP/SL) `TESTNET_RELAYER_PRIVATE_KEY` from your env. Raw 64-char hex, no `0x` prefix. The bech32 `inj1…` is derived at runtime — you don't need to write it down.

### 3. Setup (one-time)

```bash
python scripts/setup_authz_grants.py   # both MM and retail wallets need this
python scripts/register_makers.py      # admin-only; or ask your TrueCurrent contact
python scripts/fund_subaccounts.py     # USDC margin into the maker/retail subaccounts
```

### 4. Run a flow

```bash
python examples/test_roundtrip.py      # WS round-trip: request → ACK → quote
python examples/test_settlement.py     # full E2E with on-chain AcceptQuote
python examples/python-mm/main-grpc.py # standalone MM bot (no rfq_test dep)
```

For TypeScript and Go reference makers:

```bash
cd examples/ts-mm && npm install && npm run start
cd examples/go-mm/main-grpc && go run .
```

### 5. Run the test suite

```bash
pytest -m smoke          # ~30s, fast health check
pytest -m functional     # E2E flows
pytest                   # everything except `load`
```

---

## Environment configuration

| Item | Testnet | Mainnet |
|------|---------|---------|
| Cosmos chain ID | `injective-888` | `injective-1` |
| EVM chain ID (EIP-712 domain) | `1439` | `1776` |
| RFQ Contract | `inj1qw7jk82hjvf79tnjykux6zacuh9gl0z0wl3ruk` | _TBA_ |
| MakerStream WSS | `wss://testnet.rfq.ws.injective.network/injective_rfq_rpc.InjectiveRfqRPC/MakerStream` | _TBA_ |
| TakerStream WSS | `wss://testnet.rfq.ws.injective.network/injective_rfq_rpc.InjectiveRfqRPC/TakerStream` | _TBA_ |
| Indexer gRPC-web | `https://testnet.rfq.grpc.injective.network/injective_rfq_rpc.InjectiveRfqRPC` | _TBA_ |
| Chain gRPC | `testnet-grpc.injective.dev:443` | `sentry.chain.grpc.injective.network:443` |
| LCD | `https://testnet.sentry.lcd.injective.network` | `https://lcd.injective.network` |
| Faucet | `https://testnet-faucet.injective.dev` | n/a |

YAML defaults live in `configs/{env}.yaml`; override individual fields via env vars when running against a bespoke deployment.

---

## Protocol cheat-sheet

The RFQ Indexer uses **gRPC-web over WebSocket** with protobuf framing. Two streams — `TakerStream` and `MakerStream` — and a settlement path that goes directly to the CosmWasm contract on Injective.

- **Subprotocol:** `grpc-ws`
- **Framing:** `[1 byte flags][4 bytes length BE][protobuf payload]`
- **Keep-alive:** send `ping` every ~1s; the indexer drops idle streams.
- **Signing:** **EIP-712 v2** typed-data digest → secp256k1 raw → `0x` + `r ‖ s ‖ v` (v=0/1, **not** 27/28). Custom layout, *not* `eth_signTypedData_v4`. Spec in [`crypto/eip712.py`](src/rfq_test/crypto/eip712.py); recipe in [PYTHON_BUILDING_GUIDE.md § Quote Signing (v2)](PYTHON_BUILDING_GUIDE.md#quote-signing-v2).
- **Wire-required fields:** every quote and conditional-order create carries `sign_mode="v2"` and `evm_chain_id` (`1439` testnet, `1776` mainnet). Empty values are rejected. Omitting `sign_mode` falls back to deprecated `"v1"`.
- **MakerStream auth handshake:** the first server message after a maker connects is a `MakerChallenge`. Sign the `StreamAuthChallenge` typed-data and reply with `MakerAuth{evm_chain_id, signature}`. `MakerStreamClient` does this for you when you pass `auth_private_key` + `auth_evm_chain_id` + `auth_contract_address`. Standalone implementations in `examples/{python,go,ts}-mm/main-grpc.*`. Full protocol: [PYTHON_BUILDING_GUIDE.md § MakerStream Auth Handshake](PYTHON_BUILDING_GUIDE.md#makerstream-auth-handshake).

### Maker subscriptions

`MakerStreamClient` accepts these options on construction:

```python
from rfq_test.clients.websocket import MakerStreamClient

mm_client = MakerStreamClient(
    ws_url,
    maker_address=maker_inj_address,
    subscribe_to_quotes_updates=True,           # quote_update events
    subscribe_to_settlement_updates=True,       # settlement_update events
    auth_private_key=maker_private_key,         # auto-signs MakerChallenge
    auth_evm_chain_id=1439,
    auth_contract_address=contract_address,
)
```

Update event semantics:
- `quote_update` arrives for any quote whose `maker` matches `maker_address`. `status="accepted"` means used in settlement; `status="rejected"` means evaluated but not used. `executed_quantity` / `executed_margin` are the actual fill.
- `settlement_update` arrives whenever a settlement included at least one quote from this maker — even quotes that weren't the winning one.

### Conditional orders (TP/SL)

Takers pre-sign trigger-based orders that fire when mark price crosses a threshold. Two paths:
- **TakerStream** with `message_type: "conditional_order"` and `conditional_order_sign_mode="v2"` + `conditional_order_evm_chain_id` (proto field 6). `TakerStreamClient.send_conditional_order(...)` sets both for you.
- **REST API** `POST /conditionalOrder` with `sign_mode` + `evm_chain_id` (proto field 4 on direct creates).

Cancellation is on-chain via `ContractClient.cancel_intent_lane(market_id, subaccount_nonce)` (lane-scoped) or `cancel_all_intents()` (taker-wide epoch bump).

Reference: [PYTHON_BUILDING_GUIDE.md § Conditional Orders](PYTHON_BUILDING_GUIDE.md#conditional-orders-tpsl), `scripts/conditional_order_example.py`.

### Supported markets (testnet)

| Symbol | Market ID | Tick (price + qty) |
|--------|-----------|--------------------|
| INJ/USDC PERP  | `0xdc70164d7120529c3cd84278c98df4151210c0447a65a2aab03459cf328de41e` | `0.01` |
| BTC/USDC PERP  | `0xfd704649cf3a516c0c145ab0111717c44640d8dbe52a462ae35cadf2f6df1515` | `1` |
| LINK/USDC PERP | `0xdbb9bb072015238096f6e821ee9aab7affd741f8662a71acc14ac30ee6b687a5` | `0.01` |
| ETH/USDC PERP  | `0x135de28700392fb1c17d40d5170a74f30055a4ad522feddafec42fbbbb780897` | `0.01` |

All four use USDC margin: `erc20:0x0C382e685bbeeFE5d3d9C29e29E341fEE8E84C5d`.

Always run decimal fields through `quantize_for_fpdecimal` before signing — *the wire string must equal the signed string byte-for-byte*. The most common rejection is `price "76462.0": not in canonical decimal form` (BTC perp at tick `1`).

---

## Regenerating proto code

After editing `src/rfq_test/proto/injective_rfq_rpc.proto`:

```bash
.venv/bin/python -m grpc_tools.protoc \
  -I src/rfq_test/proto \
  --python_out=src/rfq_test/proto \
  --grpc_python_out=src/rfq_test/proto \
  src/rfq_test/proto/injective_rfq_rpc.proto
```

Overwrites `injective_rfq_rpc_pb2.py` and `injective_rfq_rpc_pb2_grpc.py`. After regen, review field/type changes (especially `evm_chain_id` field numbers, nested `Expiry`, and the `MakerChallenge` / `MakerAuth` shapes) and update `clients/websocket.py` if needed. `grpcio-tools` is a dev extra (`pip install -e ".[dev]"`).

---

## Roadmap to SDK

`injective-rfq-toolkit` is *almost* an SDK already — clean import surface, stable digest primitives, and reference implementations across three languages. The gap is packaging, naming hygiene, and stability commitments. Concrete steps to close it:

1. **Carve the SDK out of the toolkit.** Today everything lives under one umbrella. Split the repo internally so the published artifact is just the integration kit and the harness ships separately:
   - Rename the Python distribution from `rfq-e2e-tests` (in `pyproject.toml`) to `injective-rfq` (the *package* name). Keep the import as `rfq_test` for one release with a deprecation warning, then move it to `rfq` to match.
   - Move `tests/`, `factories/`, `utils/scenario.py`, and the pytest plumbing into a `[harness]` extra (or a sibling repo `injective-rfq-toolkit-tests`) so `pip install injective-rfq` ships only what an integrator needs.
   - Keep `actors/` if we promote it to public orchestration helpers; otherwise move it to the harness extra too.

2. **Publish.** Cut `0.1.0` to PyPI as `injective-rfq`. Adopt **semver** with a deprecation policy ("two minor releases of warning before removal") and gate every public symbol behind an explicit `__all__`. Add `py.typed` and ship full type hints in the wheel.

3. **Multi-language parity.** Today `examples/go-mm/main-grpc/main.go` and `examples/ts-mm/main-grpc.ts` are scripts. Promote them to first-class SDKs with the same surface as Python:
   - **Go** — `github.com/InjectiveLabs/injective-rfq-go` with `MakerStreamClient`, `TakerStreamClient`, `SignQuoteV2`, `SignConditionalOrderV2`, `SignMakerChallengeV2`, generated proto vendored.
   - **TypeScript** — `@injectivelabs/injective-rfq` on npm. Use `protobuf-ts` or `connect-es` so it works in Node and the browser. Same surface.
   - **Conformance:** all three languages must produce byte-identical digests for the same inputs. We already have `rfq-contract/contracts/rfq/src/test/go/eip712crosscheck/` as the seed of a cross-language test; lift that into a shared CI matrix that runs Python ↔ Go ↔ TS round-trips on every PR.

4. **Public API boundary.** Mark every leading-underscore symbol as truly private (some are reachable today). Add an `rfq.types` re-export module so partners don't import from `models.types` directly. Generate API reference docs from docstrings — Sphinx+autodoc or `mkdocs-material` + `mkdocstrings` are both fine. Cross-link from rfq.inj.so to the published reference.

5. **Versioned protocol contract.** Move `injective_rfq_rpc.proto` into a stable home — either its own `github.com/InjectiveLabs/injective-rfq-proto` repo or as a top-level `proto/` directory inside this toolkit, versioned independently of the language SDKs. All three SDKs vendor the same release tag; a `sign_mode` or wire-field change becomes one coordinated proto bump rather than three drifting regens.

6. **Maintenance contract.** Pin the support matrix (Python 3.11+, Go 1.22+, Node 20+). Set a release cadence (every 2–4 weeks for non-breaking, immediate for security). Wire `examples/*` end-to-end into CI against staging testnet so a regression breaks our build, not a partner's bot. Add a security-disclosure policy.

7. **Migration story.** Once `injective-rfq` is on PyPI, the `injective-rfq-toolkit` repo becomes a *meta-repo* that bundles: the SDK source (Python), the harness, the gRPC reference scripts in all three languages, and the proto definitions. Partners who only want the SDK install from PyPI/npm/pkg.go.dev. Partners who want the full developer experience clone the toolkit. Both work.

Until those land, "toolkit" is the honest framing. Once #1–4 ship, we drop the qualifier and start pointing partners at the package registries instead of `git clone`.

---

## License

See [LICENSE](LICENSE).
