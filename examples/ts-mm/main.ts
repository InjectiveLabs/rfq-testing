/**
 * RFQ – Market Maker Main Flow (v2 EIP-712 signing)
 *
 * Wire payloads MUST carry `sign_mode: "v2"` — empty values are rejected
 * with `value of message.sign_mode must be one of "v1", "v2"`. Full spec:
 * PYTHON_BUILDING_GUIDE.md and rfq.inj.so/onboarding.html#sign.
 *
 * Flow:
 * 0. MM has already granted permissions to RFQ contract
 * 1. MM connects to WebSocket & subscribes to RFQ requests
 * 2. MM receives RFQ request from retail
 * 3. MM builds quotes
 * 4. MM signs quotes (v2 EIP-712, see signQuoteV2 in eip712.ts)
 * 5. MM sends quotes back via WebSocket with sign_mode="v2"
 */

import WebSocket from "ws";
import dotenv from "dotenv";
import { PrivateKey } from "@injectivelabs/sdk-ts";

import { signQuoteV2 } from "./eip712.js";

dotenv.config();

/* -------------------------------------------------------------------------- */
/*                                   CONFIG                                   */
/* -------------------------------------------------------------------------- */

// WebSocket (RFQ backend / indexer)
const WS_URL = "ws://localhost:4464/ws";

// Maker private key (used ONLY for signing quotes)
const MM_PRIVATE_KEY = process.env.MM_PRIVATE_KEY!;
const CHAIN_ID = process.env.CHAIN_ID!;
const CONTRACT_ADDRESS = process.env.CONTRACT_ADDRESS!;
const EVM_CHAIN_ID = Number(process.env.EVM_CHAIN_ID ?? "1439"); // 1439 testnet, 1776 mainnet

// Derive maker address from private key
const MAKER_ADDRESS = PrivateKey.fromHex(
  MM_PRIVATE_KEY.replace(/^0x/, "")
).toBech32();

/* -------------------------------------------------------------------------- */
/*                              ENV VALIDATION                                */
/* -------------------------------------------------------------------------- */

if (!MM_PRIVATE_KEY) {
  throw new Error("MM_PRIVATE_KEY is not set");
}
if (!CHAIN_ID) throw new Error("CHAIN_ID is not set");
if (!CONTRACT_ADDRESS) throw new Error("CONTRACT_ADDRESS is not set");

/* -------------------------------------------------------------------------- */
/*                                   TYPES                                    */
/* -------------------------------------------------------------------------- */

interface RfqRequest {
  rfq_id: number;
  market_id: string;
  direction: number;
  margin: string;
  quantity: string;
  worst_price: string;
  request_address: string;
  expiry: number;
}

interface Quote {
  market_id: string;
  rfq_id: number;
  taker_direction: "long" | "short";
  margin: string;          // FPDecimal as string — must equal what was signed
  quantity: string;        // FPDecimal as string — must equal what was signed
  price: string;           // FPDecimal as string — must equal what was signed
  expiry: number;          // unix ms
  maker?: string;
  taker?: string;
  signature: string;
  sign_mode: "v2";         // required by indexer
  chain_id: string;
  contract_address: string;
  maker_subaccount_nonce: number;
}

/* -------------------------------------------------------------------------- */
/*                               STATE                                        */
/* -------------------------------------------------------------------------- */

let mmWs: WebSocket | null = null;

/* -------------------------------------------------------------------------- */
/*                          QUOTE / SIGNING LOGIC                              */
/* -------------------------------------------------------------------------- */

/**
 * Step 3 → 5: Build, sign (v2 EIP-712), and send quote
 */
async function sendQuote(
  ws: WebSocket,
  request: RfqRequest,
  price: number
) {
  const expiry = Date.now() + 20_000; // 20s validity
  const direction: "long" | "short" = request.direction === 0 ? "long" : "short";
  const priceStr = price.toString();
  const makerSubaccountNonce = 0;

  const signature = signQuoteV2({
    privateKey: MM_PRIVATE_KEY,
    evmChainId: EVM_CHAIN_ID,
    contractAddress: CONTRACT_ADDRESS,
    marketId: request.market_id,
    rfqId: request.rfq_id,
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

  const quote: Quote = {
    chain_id: CHAIN_ID,
    contract_address: CONTRACT_ADDRESS,
    rfq_id: request.rfq_id,
    market_id: request.market_id,
    taker_direction: direction,
    margin: request.margin,
    quantity: request.quantity,
    price: priceStr,
    expiry,
    maker: MAKER_ADDRESS,
    taker: request.request_address,
    signature,
    sign_mode: "v2",
    maker_subaccount_nonce: makerSubaccountNonce,
  };

  const message = {
    jsonrpc: "2.0",
    method: "quote",
    id: Date.now(),
    params: { quote },
  };

  console.log("\n📤 Sending quote");
  console.log(JSON.stringify(message, null, 2));

  ws.send(JSON.stringify(message));
}

/* -------------------------------------------------------------------------- */
/*                             WEBSOCKET FLOW                                 */
/* -------------------------------------------------------------------------- */

mmWs = new WebSocket(WS_URL);

mmWs.on("open", () => {
  console.log("🔌 MM WebSocket connected");

  // Step 1: Subscribe to RFQ request stream
  mmWs!.send(
    JSON.stringify({
      jsonrpc: "2.0",
      method: "subscribe",
      id: 1,
      params: {
        query: {
          stream: "request"
        }
      },
    })
  );

  console.log("📡 Subscribed to RFQ request stream");
});

mmWs.on("message", async (data) => {
  const msg = JSON.parse(data.toString());
  console.log('received:', msg)
  if (!msg.result?.request) return;

  const request: RfqRequest = msg.result.request;

  console.log("\n📩 RFQ request received");
  console.log(request);

  /**
   * Step 2: Pricing logic
   * (demo: simple ladder)
   */
  const prices = [1.3, 1.4, 1.5];

  for (const price of prices) {
    await sendQuote(mmWs!, request, price);
  }
});

mmWs.on("error", (err) => {
  console.error("❌ MM WebSocket error:", err);
});

mmWs.on("close", () => {
  console.log("🔌 MM WebSocket closed");
});
