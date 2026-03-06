/**
 * RFQ – Market Maker Main Flow
 *
 * Flow:
 * 0. MM has already granted permissions to RFQ contract
 * 1. MM connects to WebSocket & subscribes to RFQ requests
 * 2. MM receives RFQ request from retail
 * 3. MM builds quotes
 * 4. MM signs quotes
 * 5. MM sends quotes back via WebSocket
 */

import WebSocket from "ws";
import dotenv from "dotenv";
import { ethers, keccak256, toUtf8Bytes } from "ethers";
import { PrivateKey } from "@injectivelabs/sdk-ts";

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
  taker_direction: number; // Assuming Direction is an enum: 0 = 'LONG' | 1 = 'SHORT'
  margin: string; // FPDecimal as string
  quantity: string; // FPDecimal as string
  price: string; // FPDecimal as string
  expiry: number; // timestamp, use string if large uint64
  maker?: string; // optional
  taker?: string; // optional
  signature: string;
  chain_id: string,
  contract_address: string,
}

// Interface for SignQuote matching Contract struct
interface SignQuote {
  c: string,
  ca: string,
  mi: string;  // market_id
  id: number;  // rfq_id
  t: string;   // taker address
  td: string;  // taker_direction (0 = LONG, 1 = SHORT)
  tm: string;  // taker_margin (FPDecimal as string)
  tq: string;  // taker_quantity (FPDecimal as string)
  m: string;   // maker address
  mq: string;  // maker_quantity (FPDecimal as string)
  mm: string;  // maker_margin (FPDecimal as string)
  p: string;   // price (FPDecimal as string)
  e: number;  // expiry
}

/* -------------------------------------------------------------------------- */
/*                               STATE                                        */
/* -------------------------------------------------------------------------- */

let mmWs: WebSocket | null = null;

/* -------------------------------------------------------------------------- */
/*                          QUOTE / SIGNING LOGIC                              */
/* -------------------------------------------------------------------------- */

/**
 * Convert Quote → SignQuote (compact & deterministic)
 */
function toSignQuote(request: RfqRequest, quote: Quote): SignQuote {
  return {
    c: CHAIN_ID,
    ca: CONTRACT_ADDRESS,
    mi: quote.market_id,  // market_id
    id: quote.rfq_id,     // rfq_id
    t: request.request_address,  // taker address
    td: !request.direction ? "long" : "short",  // taker_direction from request
    tm: request.margin,  // taker_margin from request
    tq: request.quantity,  // taker_quantity from request
    m: quote.maker || '',  // maker address (required in SignQuote)
    mq: quote.quantity,  // maker_quantity from quote
    mm: quote.margin,  // maker_margin from quote
    p: quote.price,  // price from quote
    e: quote.expiry,  // expiry from quote
  };
}

/**
 * Step 4: Sign quote with MM private key
 */
async function signQuote(
  signQuote: SignQuote,
  privateKeyHex: string
): Promise<string> {
  const payload = JSON.stringify(signQuote);
  const hash = keccak256(toUtf8Bytes(payload));

  console.log('signed payload:', payload)

  const signingKey = new ethers.SigningKey(
    Buffer.from(privateKeyHex.replace(/^0x/, ""), "hex")
  );

  const sig = signingKey.sign(hash);
  return ethers.Signature.from(sig).serialized;
}

/**
 * Step 3 → 5: Build, sign, and send quote
 */
async function sendQuote(
  ws: WebSocket,
  request: RfqRequest,
  price: number
) {
  const expiry = Date.now() + 20_000; // 20s validity

  const quote: Quote = {
    chain_id: CHAIN_ID,
    contract_address: CONTRACT_ADDRESS,
    rfq_id: request.rfq_id,
    market_id: request.market_id,
    taker_direction: request.direction,
    margin: request.margin, // use same margin as taker, MM can choose how much margin to put in
    quantity: request.quantity,
    price: price.toString(),
    expiry,
    maker: MAKER_ADDRESS,
    taker: request.request_address, // ✅ from RFQ request
    signature: "", // assign later
  };

  quote.signature = await signQuote(
    toSignQuote(request, quote),
    MM_PRIVATE_KEY
  );

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
