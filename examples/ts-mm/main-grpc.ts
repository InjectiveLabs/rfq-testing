/**
 * RFQ – Market Maker Main Flow (gRPC, v2 EIP-712 signing)
 *
 * Uses native gRPC MakerStream (bidirectional) instead of WebSocket.
 *
 * Wire payloads MUST carry `sign_mode: "v2"` — empty values are rejected
 * with `value of message.sign_mode must be one of "v1", "v2"`. The full
 * spec lives in PYTHON_BUILDING_GUIDE.md and rfq.inj.so/onboarding.html#sign.
 *
 * Flow:
 * 0. MM has already granted permissions to RFQ contract (see setup.ts)
 * 1. MM connects to gRPC MakerStream with maker metadata
 * 2. MM receives RFQ requests from the stream
 * 3. MM builds and signs quotes (v2 EIP-712, see signQuoteV2 in eip712.ts)
 * 4. MM sends quotes back via the same stream with sign_mode="v2"
 * 5. MM receives quote_ack, quote_update, settlement_update events
 */

import * as grpc from "@grpc/grpc-js";
import * as protoLoader from "@grpc/proto-loader";
import path from "path";
import { fileURLToPath } from "url";
import dotenv from "dotenv";
import { PrivateKey } from "@injectivelabs/sdk-ts";

import { signMakerChallengeV2, signQuoteV2 } from "./eip712.js";

dotenv.config();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/* -------------------------------------------------------------------------- */
/*                                PROTO LOADING                               */
/* -------------------------------------------------------------------------- */

const PROTO_PATH = path.resolve(
  __dirname,
  "../../src/rfq_test/proto/injective_rfq_rpc.proto"
);

const packageDefinition = protoLoader.loadSync(PROTO_PATH, {
  keepCase: true,
  longs: String,
  enums: String,
  defaults: true,
  oneofs: true,
});

const proto = grpc.loadPackageDefinition(packageDefinition);
const InjectiveRfqRPC = (proto.injective_rfq_rpc as any).InjectiveRfqRPC;

/* -------------------------------------------------------------------------- */
/*                                   CONFIG                                   */
/* -------------------------------------------------------------------------- */

const GRPC_ENDPOINT = process.env.GRPC_ENDPOINT!;
const MM_PRIVATE_KEY = process.env.MM_PRIVATE_KEY!;
const CHAIN_ID = process.env.CHAIN_ID!;
const CONTRACT_ADDRESS = process.env.CONTRACT_ADDRESS!;
const EVM_CHAIN_ID = Number(process.env.EVM_CHAIN_ID ?? "1439"); // 1439 testnet, 1776 mainnet

const MAKER_ADDRESS = PrivateKey.fromHex(
  MM_PRIVATE_KEY.replace(/^0x/, "")
).toBech32();

/* -------------------------------------------------------------------------- */
/*                              ENV VALIDATION                                */
/* -------------------------------------------------------------------------- */

if (!GRPC_ENDPOINT) throw new Error("GRPC_ENDPOINT is not set");
if (!MM_PRIVATE_KEY) throw new Error("MM_PRIVATE_KEY is not set");
if (!CHAIN_ID) throw new Error("CHAIN_ID is not set");
if (!CONTRACT_ADDRESS) throw new Error("CONTRACT_ADDRESS is not set");

/* -------------------------------------------------------------------------- */
/*                              GRPC STREAM                                   */
/* -------------------------------------------------------------------------- */

const PING_INTERVAL = 10_000; // 10 seconds

function isLoopbackTarget(target: string): boolean {
  let host = target;

  if (target.includes("://")) {
    try {
      const parsed = new URL(target);
      host = parsed.host || parsed.pathname;
    } catch {
      host = target;
    }
  }
  if (host.startsWith("dns:///")) {
    host = host.slice("dns:///".length);
  }
  if (host.startsWith("[")) {
    const end = host.indexOf("]");
    if (end !== -1) {
      host = host.slice(1, end);
    }
  } else if (host.includes(":")) {
    host = host.slice(0, host.lastIndexOf(":"));
  }

  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

function sendQuote(stream: grpc.ClientDuplexStream<any, any>, request: any, price: number) {
  const expiry = Date.now() + 20_000; // 20s validity
  const priceStr = price.toString();
  const makerSubaccountNonce = 0;
  const direction: "long" | "short" = request.direction === "short" ? "short" : "long";

  const signature = signQuoteV2({
    privateKey: MM_PRIVATE_KEY,
    evmChainId: EVM_CHAIN_ID,
    contractAddress: CONTRACT_ADDRESS,
    marketId: request.market_id,
    rfqId: Number(request.rfq_id),
    taker: request.request_address,
    direction,
    takerMargin: request.margin,
    takerQuantity: request.quantity,
    maker: MAKER_ADDRESS,
    makerSubaccountNonce,
    makerQuantity: request.quantity,
    makerMargin: request.margin,
    price: priceStr,
    expiryMs: expiry,
  });

  const quote = {
    chain_id: CHAIN_ID,
    contract_address: CONTRACT_ADDRESS,
    rfq_id: Number(request.rfq_id),
    market_id: request.market_id,
    taker_direction: direction,
    margin: request.margin,
    quantity: request.quantity,
    price: priceStr,
    expiry: { timestamp: expiry },
    maker: MAKER_ADDRESS,
    taker: request.request_address,
    signature,
    sign_mode: "v2",                       // required by indexer
    maker_subaccount_nonce: makerSubaccountNonce,
  };

  console.log(`\n📤 Sending quote (price=${price})`);
  stream.write({ message_type: "quote", quote });
}

/* -------------------------------------------------------------------------- */
/*                                   MAIN                                     */
/* -------------------------------------------------------------------------- */

function main() {
  const useSsl = !isLoopbackTarget(GRPC_ENDPOINT);

  const credentials = useSsl
    ? grpc.credentials.createSsl()
    : grpc.credentials.createInsecure();

  const client = new InjectiveRfqRPC(GRPC_ENDPOINT, credentials);

  const metadata = new grpc.Metadata();
  metadata.set("maker_address", MAKER_ADDRESS);
  metadata.set("subscribe_to_quotes_updates", "true");
  metadata.set("subscribe_to_settlement_updates", "true");

  console.log("🔌 Connecting to gRPC MakerStream...");
  console.log(`   Endpoint: ${GRPC_ENDPOINT}`);
  console.log(`   Maker:    ${MAKER_ADDRESS}`);

  const stream: grpc.ClientDuplexStream<any, any> = client.MakerStream(metadata);

  // First message must carry data alongside the HEADERS frame
  stream.write({ message_type: "ping" });

  // Periodic ping to keep the stream alive
  const pingTimer = setInterval(() => {
    stream.write({ message_type: "ping" });
  }, PING_INTERVAL);

  console.log("📡 MM connected — listening for RFQ requests...\n");

  stream.on("data", (response: any) => {
    const msgType = response.message_type;

    if (msgType === "pong") {
        return;
    }

    if (msgType === "challenge") {
      const cha = response.challenge;
      console.log(`\n🔒 RFQ auth challenge nonce: ${cha.nonce}`);
      console.log(`🔒 RFQ auth challenge expires at: ${cha.expires_at}`);
      console.log(`🔒 RFQ auth challenge chain ID: ${cha.evm_chain_id}`);
      console.log("🔐 Signing and sending auth response...");

      const signature = signMakerChallengeV2({
        privateKey: MM_PRIVATE_KEY,
        evmChainId: Number(cha.evm_chain_id),
        contractAddress: CONTRACT_ADDRESS,
        maker: MAKER_ADDRESS,
        nonceHex: cha.nonce,
        expiresAt: BigInt(cha.expires_at),
      });

      stream.write({
        message_type: "auth",
        auth: {
          evm_chain_id: Number(cha.evm_chain_id),
          signature,
        },
      });
    } else if (msgType === "request") {
      const req = response.request;
      console.log(`\n📩 RFQ request: RFQ#${req.rfq_id} market=${req.market_id}`);
      console.log(
        `   direction=${req.direction} margin=${req.margin} qty=${req.quantity}`
      );

      // Demo pricing ladder
      const prices = [1.3, 1.4, 1.5];
      for (const p of prices) {
        sendQuote(stream, req, p);
      }
    } else if (msgType === "quote_ack") {
      const ack = response.quote_ack;
      console.log(`📬 Quote ACK: rfq_id=${ack.rfq_id} status=${ack.status}`);
    } else if (msgType === "quote_update") {
      const qu = response.processed_quote;
      console.log(
        `📊 Quote update: rfq_id=${qu.rfq_id} status=${qu.status}`
      );
    } else if (msgType === "settlement_update") {
      const s = response.settlement;
      console.log(`⚖️  Settlement: rfq_id=${s.rfq_id} cid=${s.cid}`);
      for (const q of s.quotes || []) {
        console.log(
          `   quote: maker=${q.maker} price=${q.price} status=${q.status}`
        );
      }
    } else if (msgType === "error") {
      const e = response.error;
      console.error(`❌ Stream error: ${e.code}: ${e.message_}`);
    } else {
      console.log(`Unknown message type: ${msgType}`, response);
    }
  });

  stream.on("error", (err: any) => {
    console.error("❌ MakerStream error:", err.message);
    clearInterval(pingTimer);
  });

  stream.on("end", () => {
    console.log("🔌 MakerStream ended");
    clearInterval(pingTimer);
  });
}

main();
