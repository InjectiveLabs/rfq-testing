/**
 * !!! v1 SIGNING — NEEDS PORT TO v2 (EIP-712) !!!
 * As of 2026-04-29 the indexer rejects empty `sign_mode`. The rfq-testing
 * standard is v2. Canonical v2 reference: src/rfq_test/crypto/eip712.py +
 * PYTHON_BUILDING_GUIDE.md.
 *
 * RFQ – Retail User Main Flow
 *
 * Flow:
 * 0. Retail user has already granted permissions to RFQ contract
 * 1. Retail opens WebSocket & subscribes to quotes
 * 2. Retail creates RFQ request
 * 3. Makers respond with quotes
 * 4. Retail picks best quote
 * 5. Retail accepts quote on-chain
 */

import WebSocket from "ws";
import dotenv from "dotenv";
import {
  ChainGrpcExchangeApi,
  MsgBroadcasterWithPk,
  MsgExecuteContractCompat,
} from "@injectivelabs/sdk-ts";
import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import { ChainId } from "@injectivelabs/ts-types";

dotenv.config();

/* -------------------------------------------------------------------------- */
/*                                   CONFIG                                   */
/* -------------------------------------------------------------------------- */

// WebSocket (RFQ backend / indexer)
const WS_URL = "ws://localhost:4464/ws";

// Market
const INJUSDT_MARKET_ID =
  "0x7cc8b10d7deb61e744ef83bdec2bbcf4a056867e89b062c6a453020ca82bd4e4";

// Contract
const CONTRACT_ADDRESS = process.env.CONTRACT_ADDRESS!;

// Retail user
const TAKER_ADDRESS =
  "inj1cml96vmptgw99syqrrz8az79xer2pcgp0a885r";
const RETAIL_PRIVATE_KEY = process.env.RETAIL_PRIVATE_KEY!;

// Network
const NETWORK = Network.Local;
const CHAIN_ID = process.env.CHAIN_ID;
const ENDPOINTS = getNetworkEndpoints(NETWORK);

/* -------------------------------------------------------------------------- */
/*                              ENV VALIDATION                                */
/* -------------------------------------------------------------------------- */

if (!CONTRACT_ADDRESS) throw new Error("CONTRACT_ADDRESS not set");
if (!RETAIL_PRIVATE_KEY) throw new Error("RETAIL_PRIVATE_KEY not set");

/* -------------------------------------------------------------------------- */
/*                              INPUT PARAMETERS                              */
/* -------------------------------------------------------------------------- */

// CLI args: node main.ts <margin> <quantity> <maxSlippageBps>
const args = process.argv.slice(2);

const margin = args[0] || "100";           // USDT margin
const quantity = args[1] || "140";         // position size
const maxSlippageBps = args[2] || "100";   // max slippage in basis points (100 = 1%)
const leverage = 2;

console.log("📥 RFQ input params");
console.log("margin:", margin);
console.log("leverage:", leverage);
console.log("quantity:", quantity);
console.log("max slippage:", maxSlippageBps, "bps (", Number(maxSlippageBps) / 100, "%)");

// Calculate max acceptable price based on expected price and slippage
// For a buy order, expected price = (margin * leverage) / quantity
const expectedPrice = Number(margin) * leverage / Number(quantity);
const maxAcceptablePrice = expectedPrice * (1 + Number(maxSlippageBps) / 10000);

console.log("expected price:", expectedPrice.toFixed(4));
console.log("max acceptable price:", maxAcceptablePrice.toFixed(4));

/* -------------------------------------------------------------------------- */
/*                                  TYPES                                     */
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

interface Expiry {
  ts: number | undefined; 
  h: number | undefined;
}

interface Quote {
  maker: string;
  margin: string; // Assuming FPDecimal is serialized as string for precision
  price: string;  // Assuming FPDecimal is serialized as string for precision
  quantity: string; // Assuming FPDecimal is serialized as string for precision
  expiry: Expiry;
  signature: string; // Binary is typically base64 or hex encoded string
  nonce: number | undefined;
}

interface Settlement {
  rfq_id: number; 
  market_id: string;
  taker: string;
  direction: 'LONG' | 'SHORT' | string;
  margin: string;
  quantity: string;
  worst_price: string;
  quote?: Quote;
  unfilled_action?: PostUnfilledAction;
  created_at: number;
  tx_hash: string;
  transaction_time: number; 
  fallback_quantity: string;
  fallback_margin: string;
  order_hash: string;
}

interface PostUnfilledAction {
  limit?: LimitAction;
  market?: MarketAction;
}

interface LimitAction {
  price: string;
}
interface MarketAction {}

interface WebSocketMessage {
  jsonrpc: string;
  method?: string;
  id: number;
  params?: any;
  result?: {
    quote?: Quote;
    settlement?: Settlement;
    stream_operation?: string;
  };
}

/* -------------------------------------------------------------------------- */
/*                               STATE STORAGE                                */
/* -------------------------------------------------------------------------- */

let retailWs: WebSocket | null = null;
let settlementWs: WebSocket | null = null;
let receivedQuotes: Quote[] = [];
let requestRfqId: number | null = null;

/* -------------------------------------------------------------------------- */
/*                              RFQ OPERATIONS                                */
/* -------------------------------------------------------------------------- */

/**
 * Step 2: Create RFQ request (off-chain)
 */

async function createRfqRequest(
  ws: WebSocket,
  request: RfqRequest,
) {
  
  const message: WebSocketMessage = {
    jsonrpc: "2.0",
    method: "create",
    id: Date.now(),
    params: { request },
  };

  console.log("\n📤 Sending RFQ request");
  console.log(JSON.stringify(message, null, 2));

  ws.send(JSON.stringify(message));

  console.log("\n📤 Start aggregating quotes");

  return request
}

/**
 * Step 4: Pick best quote within slippage tolerance
 */
function chooseBestQuotes(
  maxQuantity: number,
  quotes: Quote[],
  maxPrice: number
): Quote[] {
  if (quotes.length === 0 || maxQuantity <= 0) return [];

  // 1. Filter by price and sort best → worst
  const eligibleQuotes = quotes
    .filter(q => Number(q.price) <= maxPrice)
    .sort((a, b) => Number(a.price) - Number(b.price));

  if (eligibleQuotes.length === 0) {
    console.log("⚠️  No quotes within slippage tolerance");
    return [];
  }

  // 2. Accumulate until filled (allow overshoot)
  let accumulatedQty = 0;
  const selected: Quote[] = [];

  for (const q of eligibleQuotes) {
    selected.push(q);
    accumulatedQty += Number(q.quantity);

    if (accumulatedQty >= maxQuantity) {
      break; // filled (or overfilled)
    }
  }

  return selected;
}

/**
 * Step 5: Accept quote on-chain
 */
async function acceptQuote(worst_price: number, request: RfqRequest, quotes: Quote[]) {
  console.log("\n📌 Accepting quotes:", quotes);

  const action = {
    accept_quote: {
      rfq_id: request.rfq_id,
      market_id: request.market_id,
      margin: request.margin,
      direction: request.direction === 0 ? "long" : "short",
      quantity: request.quantity,
      worst_price: worst_price.toFixed(3),
      quotes: quotes,
      unfilled_action: {
        limit: {
          // set the price user wants a resting order to be at
          price: worst_price.toFixed(3),
        }
      }, // Use limit / market to settle unfilled quantity
      cid: 'fe-rfq',
    },
  };

  console.log('action str:', JSON.stringify(action))

  // Fetch market info to compute fee
  const msg = MsgExecuteContractCompat.fromJSON({
    sender: TAKER_ADDRESS,
    contractAddress: CONTRACT_ADDRESS,
    msg: action,
    funds: [], // contract will auto calculate this
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
/*                              WEBSOCKET FLOW                                */
/* -------------------------------------------------------------------------- */

retailWs = new WebSocket(WS_URL);
settlementWs = new WebSocket(WS_URL);  

retailWs.on("open", () => {
  console.log("🔌 Retail WebSocket connected");

  // Step 1: Subscribe to quote stream
  retailWs!.send(
    JSON.stringify({
      jsonrpc: "2.0",
      method: "subscribe",
      id: 1,
      params: { query: {
        stream: "quote",
        args: {
          addresses: [TAKER_ADDRESS],
          market_id: [INJUSDT_MARKET_ID],
        }
      }},
    })
  );

  console.log("📡 Subscribed to quote stream");

  const expiry = Date.now() + 5 * 60 * 1000; // 5 minutes

  const request: RfqRequest = {
    rfq_id: Date.now(),
    market_id: INJUSDT_MARKET_ID,
    direction: 0, // long
    margin: margin,
    quantity: quantity,
    request_address: TAKER_ADDRESS,
    worst_price: parseFloat(maxAcceptablePrice.toFixed(3)).toString(),
    expiry,
  };

  // Step 2: Create RFQ request
  createRfqRequest(
    retailWs!,
    request,
  );

  // Step 4 → 5: wait for quotes then accept best within slippage
  setTimeout(async () => {
    const best = chooseBestQuotes(Number(quantity), receivedQuotes, maxAcceptablePrice);
    if (best.length == 0) {
      console.log("❌ No quotes received");
      process.exit(0);
    }

    await acceptQuote(maxAcceptablePrice, request, best);

    receivedQuotes = [];
    requestRfqId = null;
  }, 2000);
});

retailWs.on("message", (data) => {
  const msg: WebSocketMessage = JSON.parse(data.toString());
  if (!msg.result?.quote) return;

  const quote = msg.result.quote as any;
  console.log(
    `📩 Quote received | price=${quote.price} maker=${quote.maker}`
  );

  let q: Quote = {
    maker: quote.maker,
    margin: quote.margin,
    price: quote.price,
    quantity: quote.quantity,
    expiry: quote.expiry,
    signature: Buffer.from(quote.signature.replace('0x', ''), 'hex').toString('base64'),
    nonce: undefined,
  }

  if (quote.nonce) {
    q.nonce = quote.nonce
  }

  receivedQuotes.push(q);
});

retailWs.on("error", (err) => {
  console.error("❌ WebSocket error:", err);
});

retailWs.on("close", () => {
  console.log("🔌 WebSocket closed");
});

settlementWs.on("open", () => {
  console.log("🔌 Settlement WebSocket connected");

  settlementWs!.send(
    JSON.stringify({
      jsonrpc: "2.0",
      method: "subscribe",
      id: 1,
      params: { query: {
        stream: "settlement",
        args: {},
      } },
    })
  );

  console.log("📡 Subscribed to settlement stream");
})

settlementWs.on("message", (data) => {
  const msg: WebSocketMessage = JSON.parse(data.toString());
  if (!msg.result?.settlement) return;
  
  const settlement = msg.result.settlement as any;
  const results = settlement.results as { 
    q: string; 
    m: string; 
    e?: string 
  }[];
  const filledQuantity = results.reduce((sum, r) => r.e ? sum : sum + parseFloat(r.q), 0);
  const usedMargin = results.reduce((sum, r) => r.e ? sum : sum + parseFloat(r.m), 0);

  console.table({
    "Original Order": { margin: settlement.margin, quantity: settlement.quantity },
    "Filled": { margin: usedMargin, quantity: filledQuantity },
    "Fallback Order": { margin: settlement.fallback_margin, quantity: settlement.fallback_quantity }
  });
});

settlementWs.on("error", (err) => {
  console.error("❌ WebSocket error:", err);
});

settlementWs.on("close", () => {
  console.log("🔌 WebSocket closed");
});