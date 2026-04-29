/**
 * !!! v1 SIGNING — NEEDS PORT TO v2 (EIP-712) !!!
 * As of 2026-04-29 the indexer rejects empty `sign_mode`. v2 is the
 * rfq-testing standard. Canonical v2 reference:
 * src/rfq_test/crypto/eip712.py + PYTHON_BUILDING_GUIDE.md.
 *
 * RFQ – Retail User Main Flow (gRPC)
 *
 * Uses native gRPC APIs instead of WebSocket.
 *
 * Flow:
 * 0. Retail user has already granted permissions to RFQ contract (see setup.ts)
 * 1. Retail subscribes to StreamQuote (server-streaming gRPC)
 * 2. Retail creates RFQ request via unary Request RPC
 * 3. Makers respond with quotes (received on StreamQuote)
 * 4. Retail picks best quote within slippage tolerance
 * 5. Retail accepts quote on-chain
 */

import * as grpc from "@grpc/grpc-js";
import * as protoLoader from "@grpc/proto-loader";
import path from "path";
import { fileURLToPath } from "url";
import dotenv from "dotenv";
import { v4 as uuidv4 } from "uuid";
import {
  MsgBroadcasterWithPk,
  MsgExecuteContractCompat,
} from "@injectivelabs/sdk-ts";
import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import { ChainId } from "@injectivelabs/ts-types";

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
const CONTRACT_ADDRESS = process.env.CONTRACT_ADDRESS!;
const RETAIL_PRIVATE_KEY = process.env.RETAIL_PRIVATE_KEY!;
const CHAIN_ID = process.env.CHAIN_ID!;

// Market
const INJUSDT_MARKET_ID =
  "0x7cc8b10d7deb61e744ef83bdec2bbcf4a056867e89b062c6a453020ca82bd4e4";

// Taker address (derive from private key in production)
const TAKER_ADDRESS = "inj1cml96vmptgw99syqrrz8az79xer2pcgp0a885r";

// Network
const NETWORK = Network.Local;
const ENDPOINTS = getNetworkEndpoints(NETWORK);

/* -------------------------------------------------------------------------- */
/*                              ENV VALIDATION                                */
/* -------------------------------------------------------------------------- */

if (!GRPC_ENDPOINT) throw new Error("GRPC_ENDPOINT is not set");
if (!CONTRACT_ADDRESS) throw new Error("CONTRACT_ADDRESS is not set");
if (!RETAIL_PRIVATE_KEY) throw new Error("RETAIL_PRIVATE_KEY is not set");

/* -------------------------------------------------------------------------- */
/*                              INPUT PARAMETERS                              */
/* -------------------------------------------------------------------------- */

const args = process.argv.slice(2);
const margin = args[0] || "100";
const quantity = args[1] || "140";
const maxSlippageBps = args[2] || "100"; // 100 = 1%
const leverage = 2;

const expectedPrice = (Number(margin) * leverage) / Number(quantity);
const maxAcceptablePrice =
  expectedPrice * (1 + Number(maxSlippageBps) / 10000);

console.log("📥 RFQ input params");
console.log("margin:", margin);
console.log("quantity:", quantity);
console.log("max slippage:", maxSlippageBps, "bps");
console.log("expected price:", expectedPrice.toFixed(4));
console.log("max acceptable price:", maxAcceptablePrice.toFixed(4));

/* -------------------------------------------------------------------------- */
/*                                  TYPES                                     */
/* -------------------------------------------------------------------------- */

interface CollectedQuote {
  maker: string;
  margin: string;
  price: string;
  quantity: string;
  expiry: { ts?: number; h?: number };
  signature: string;
  nonce?: number;
}

/* -------------------------------------------------------------------------- */
/*                              QUOTE SELECTION                               */
/* -------------------------------------------------------------------------- */

function chooseBestQuotes(
  maxQuantity: number,
  quotes: CollectedQuote[],
  maxPrice: number
): CollectedQuote[] {
  if (quotes.length === 0 || maxQuantity <= 0) return [];

  const eligible = quotes
    .filter((q) => Number(q.price) <= maxPrice)
    .sort((a, b) => Number(a.price) - Number(b.price));

  if (eligible.length === 0) {
    console.log("⚠️  No quotes within slippage tolerance");
    return [];
  }

  let accQty = 0;
  const selected: CollectedQuote[] = [];
  for (const q of eligible) {
    selected.push(q);
    accQty += Number(q.quantity);
    if (accQty >= maxQuantity) break;
  }
  return selected;
}

/* -------------------------------------------------------------------------- */
/*                            ON-CHAIN SETTLEMENT                             */
/* -------------------------------------------------------------------------- */

async function acceptQuote(
  worstPrice: number,
  rfqId: number,
  marketId: string,
  quotes: CollectedQuote[]
) {
  console.log("\n📌 Accepting quotes on-chain...");

  // Convert signatures from hex to base64 for the contract
  const contractQuotes = quotes.map((q) => ({
    maker: q.maker,
    margin: q.margin,
    quantity: q.quantity,
    price: q.price,
    expiry: q.expiry.ts || q.expiry.h || 0,
    signature: Buffer.from(
      q.signature.replace("0x", ""),
      "hex"
    ).toString("base64"),
  }));

  const action = {
    accept_quote: {
      rfq_id: rfqId,
      market_id: marketId,
      margin,
      direction: "long",
      quantity,
      worst_price: worstPrice.toFixed(3),
      quotes: contractQuotes,
      unfilled_action: {
        limit: { price: worstPrice.toFixed(3) },
      },
      cid: `fe-rfq-grpc-${uuidv4()}`,
    },
  };

  const msg = MsgExecuteContractCompat.fromJSON({
    sender: TAKER_ADDRESS,
    contractAddress: CONTRACT_ADDRESS,
    msg: action,
    funds: [],
  });

  const broadcaster = new MsgBroadcasterWithPk({
    privateKey: RETAIL_PRIVATE_KEY,
    network: NETWORK,
    chainId: CHAIN_ID as ChainId,
    endpoints: ENDPOINTS,
    simulateTx: true,
    gasBufferCoefficient: 1.2,
  });

  const tx = await broadcaster.broadcast({ msgs: [msg] });
  console.log("✅ Quote accepted");
  console.log("TxHash:", tx.txHash);
}

/* -------------------------------------------------------------------------- */
/*                                   MAIN                                     */
/* -------------------------------------------------------------------------- */

async function main() {
  const useSsl =
    !GRPC_ENDPOINT.startsWith("localhost") &&
    !GRPC_ENDPOINT.startsWith("127.0.0.1");

  const credentials = useSsl
    ? grpc.credentials.createSsl()
    : grpc.credentials.createInsecure();

  const client = new InjectiveRfqRPC(GRPC_ENDPOINT, credentials);

  console.log("\n🔌 Connecting to gRPC...");
  console.log(`   Endpoint: ${GRPC_ENDPOINT}`);
  console.log(`   Taker:    ${TAKER_ADDRESS}`);

  // ── Step 1: Subscribe to quote stream ──────────────────────────────────
  const receivedQuotes: CollectedQuote[] = [];

  const quoteStream = client.StreamQuote({
    addresses: [TAKER_ADDRESS],
    market_ids: [INJUSDT_MARKET_ID],
  });

  quoteStream.on("data", (response: any) => {
    const q = response.quote;
    console.log(
      `📩 Quote received | price=${q.price} maker=${q.maker} rfq_id=${q.rfq_id}`
    );

    const expiryTs = q.expiry?.timestamp
      ? Number(q.expiry.timestamp)
      : undefined;
    const expiryH = q.expiry?.height ? Number(q.expiry.height) : undefined;

    receivedQuotes.push({
      maker: q.maker,
      margin: q.margin,
      price: q.price,
      quantity: q.quantity,
      expiry: { ts: expiryTs, h: expiryH },
      signature: q.signature,
      nonce: q.maker_subaccount_nonce
        ? Number(q.maker_subaccount_nonce)
        : undefined,
    });
  });

  quoteStream.on("error", (err: any) => {
    console.error("❌ StreamQuote error:", err.message);
  });

  console.log("📡 Subscribed to StreamQuote");

  // ── Step 2: Create RFQ request ─────────────────────────────────────────
  const clientId = uuidv4();
  const expiryMs = Date.now() + 5 * 60 * 1000; // 5 minutes

  const rfqRequest = {
    request: {
      client_id: clientId,
      market_id: INJUSDT_MARKET_ID,
      direction: "long",
      margin,
      quantity,
      worst_price: parseFloat(maxAcceptablePrice.toFixed(3)).toString(),
      request_address: TAKER_ADDRESS,
      expiry: expiryMs,
    },
  };

  console.log(`\n📤 Sending RFQ request (client_id=${clientId})...`);

  const requestResponse: any = await new Promise((resolve, reject) => {
    client.Request(rfqRequest, (err: any, resp: any) => {
      if (err) return reject(err);
      resolve(resp);
    });
  });

  const rfqId = Number(requestResponse.rfq_id);
  console.log(
    `📬 Request ACK: RFQ#${rfqId} status=${requestResponse.status}`
  );

  // ── Step 3-4: Wait for quotes, then pick the best ─────────────────────
  const QUOTE_WAIT_MS = 2000;
  console.log(`\n⏳ Waiting ${QUOTE_WAIT_MS}ms for quotes...`);

  await new Promise((r) => setTimeout(r, QUOTE_WAIT_MS));

  const best = chooseBestQuotes(
    Number(quantity),
    receivedQuotes,
    maxAcceptablePrice
  );

  if (best.length === 0) {
    console.log("❌ No acceptable quotes received");
    quoteStream.cancel();
    return;
  }

  console.log(`✅ ${receivedQuotes.length} quote(s) received, using ${best.length} quote(s)`);
  for (const q of best) {
    console.log(`   price=${q.price} maker=${q.maker}`);
  }

  // ── Step 5: Accept quote on-chain ──────────────────────────────────────
  await acceptQuote(maxAcceptablePrice, rfqId, INJUSDT_MARKET_ID, best);

  quoteStream.cancel();
  console.log("\n🔌 gRPC streams closed");
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
