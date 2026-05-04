/**
 * RFQ Taker — Multi-Quote Aggregation Example
 *
 * Retail forwards MM signatures to AcceptQuote — it doesn't sign anything
 * itself. The wire RFQQuoteType carries `sign_mode` and `evm_chain_id`;
 * this script forwards the v2 fields on each ContractQuote.
 *
 * Demonstrates accepting multiple quotes from different makers in a single
 * AcceptQuote transaction. The contract's `quotes` field is a Vec<Quote>
 * and walks the list in submission order, filling from each until the
 * taker's total quantity is covered.
 *
 * Key behaviours shown:
 *   1. Collect N quotes over a fixed window
 *   2. Filter by worst_price and sort cheapest-first (longs)
 *   3. Greedily select quotes until aggregate quantity >= requested
 *   4. Submit one AcceptQuote with the selected array
 *   5. Correct encoding: use the ACK rfq_id as number, expiry wrapped,
 *      signature as base64, and v2 fields forwarded
 *
 * Flow (same as main.ts, but accepts MULTIPLE quotes):
 *   0. Grants already in place
 *   1. Open WebSocket and subscribe to quote stream
 *   2. Send RFQ request
 *   3. Collect quotes for 5 seconds
 *   4. Select best N quotes that cover requested quantity
 *   5. Accept all selected on-chain
 */

import WebSocket from "ws";
import dotenv from "dotenv";
import { randomUUID } from "crypto";
import {
  MsgBroadcasterWithPk,
  MsgExecuteContractCompat,
} from "@injectivelabs/sdk-ts";
import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import { ChainId } from "@injectivelabs/ts-types";

dotenv.config();

/* -------------------------------------------------------------------------- */
/*                                   CONFIG                                   */
/* -------------------------------------------------------------------------- */

const WS_URL = process.env.RFQ_WS_URL || "ws://localhost:4464/ws";
const CONTRACT_ADDRESS = process.env.CONTRACT_ADDRESS!;
const TAKER_ADDRESS = process.env.TAKER_ADDRESS!;
const RETAIL_PRIVATE_KEY = process.env.RETAIL_PRIVATE_KEY!;
const INJUSDC_MARKET_ID =
  process.env.MARKET_ID ||
  "0x17ef48032cb24375ba7c2e39f384e56433bcab20cbee9a7357e4cba2eb00abe6";

const NETWORK = (process.env.INJ_NETWORK as Network) || Network.TestnetSentry;
const CHAIN_ID = (process.env.CHAIN_ID || "injective-888") as ChainId;
const ENDPOINTS = getNetworkEndpoints(NETWORK);

if (!CONTRACT_ADDRESS) throw new Error("CONTRACT_ADDRESS not set");
if (!RETAIL_PRIVATE_KEY) throw new Error("RETAIL_PRIVATE_KEY not set");
if (!TAKER_ADDRESS) throw new Error("TAKER_ADDRESS not set");

// Trade parameters — taker wants 100 INJ long, max 5.00 per contract
const TOTAL_MARGIN = "200";
const TOTAL_QUANTITY = 100;
const WORST_PRICE = 5.0;
const COLLECTION_WINDOW_MS = 5000;

/* -------------------------------------------------------------------------- */
/*                                   TYPES                                    */
/* -------------------------------------------------------------------------- */

interface IndexerQuote {
  rfq_id: number | string;
  market_id: string;
  maker: string;
  taker: string;
  taker_direction: "long" | "short";
  margin: string;
  quantity: string;
  price: string;
  expiry: number | string; // Unix ms; cast defensively before use
  signature: string; // hex with 0x prefix, as delivered by the indexer
  sign_mode?: "v2"; // v2 only — v1 is deprecated and will be rejected at launch
  evm_chain_id?: number | string;
  maker_subaccount_nonce?: number | string;
  min_fill_quantity?: string;
  status?: string;
}

interface ContractQuote {
  maker: string;
  margin: string;
  quantity: string;
  price: string;
  expiry: { ts: number }; // wrapped enum
  signature: string; // base64
  sign_mode?: "v2"; // v2 only — v1 is deprecated and will be rejected at launch
  evm_chain_id?: number;
  maker_subaccount_nonce?: number;
  min_fill_quantity?: string;
}

/* -------------------------------------------------------------------------- */
/*                              QUOTE SELECTION                                */
/* -------------------------------------------------------------------------- */

/**
 * Pick the cheapest quotes that together cover `requestedQty`.
 *
 * IMPORTANT: returns quotes sorted cheapest-first. The contract walks the
 * array in submission order, so this ordering directly controls which
 * quotes you consume and in what order.
 */
function selectMultiQuotes(
  quotes: IndexerQuote[],
  requestedQty: number,
  worstPrice: number,
): IndexerQuote[] {
  const eligible = quotes
    .filter((q) => Number(q.price) <= worstPrice)
    .sort((a, b) => Number(a.price) - Number(b.price));

  if (eligible.length === 0) return [];

  const selected: IndexerQuote[] = [];
  let covered = 0;

  for (const q of eligible) {
    selected.push(q);
    covered += Number(q.quantity);
    if (covered >= requestedQty) break;
  }

  return selected;
}

/* -------------------------------------------------------------------------- */
/*                         INDEXER → CONTRACT ENCODING                         */
/* -------------------------------------------------------------------------- */

/**
 * Convert an indexer-delivered quote into the shape the on-chain contract
 * expects. The three non-obvious conversions:
 *
 *   - expiry integer → { ts: <ms> } wrapped enum variant
 *   - signature hex  → base64 (CosmWasm Binary type)
 *   - quantity/margin stay as decimal strings
 */
function toContractQuote(q: IndexerQuote): ContractQuote {
  const sigHex = q.signature.replace(/^0x/, "");
  const signatureB64 = Buffer.from(sigHex, "hex").toString("base64");

  return {
    maker: q.maker,
    margin: q.margin,
    quantity: q.quantity,
    price: q.price,
    expiry: { ts: Number(q.expiry) },
    signature: signatureB64,
    sign_mode: q.sign_mode ?? "v2",
    evm_chain_id: q.evm_chain_id !== undefined ? Number(q.evm_chain_id) : undefined,
    maker_subaccount_nonce:
      q.maker_subaccount_nonce !== undefined
        ? Number(q.maker_subaccount_nonce)
        : 0,
    ...(q.min_fill_quantity ? { min_fill_quantity: q.min_fill_quantity } : {}),
  };
}

/* -------------------------------------------------------------------------- */
/*                              ACCEPT QUOTE                                   */
/* -------------------------------------------------------------------------- */

async function acceptQuotes(
  rfqId: number,
  marketId: string,
  direction: "long" | "short",
  selectedQuotes: IndexerQuote[],
): Promise<string> {
  const contractQuotes = selectedQuotes.map(toContractQuote);

  const action = {
    accept_quote: {
      rfq_id: rfqId, // NUMBER, not string
      market_id: marketId,
      direction, // lowercase string
      margin: TOTAL_MARGIN,
      quantity: TOTAL_QUANTITY.toString(),
      worst_price: WORST_PRICE.toFixed(3),
      quotes: contractQuotes,
      unfilled_action: null, // strict RFQ — no orderbook fallback
      cid: "multi-quote-example",
    },
  };

  console.log("\n📌 AcceptQuote payload:");
  console.log(JSON.stringify(action, null, 2));

  const msg = MsgExecuteContractCompat.fromJSON({
    sender: TAKER_ADDRESS,
    contractAddress: CONTRACT_ADDRESS,
    msg: action,
    funds: [],
  });

  const broadcaster = new MsgBroadcasterWithPk({
    privateKey: RETAIL_PRIVATE_KEY,
    network: NETWORK,
    chainId: CHAIN_ID,
    endpoints: ENDPOINTS,
    simulateTx: true,
    gasBufferCoefficient: 1.2,
  });

  const result = await broadcaster.broadcast({ msgs: [msg] });
  return result.txHash;
}

/* -------------------------------------------------------------------------- */
/*                              WEBSOCKET FLOW                                 */
/* -------------------------------------------------------------------------- */

async function main() {
  const clientId = randomUUID();
  let rfqId: number | null = null;
  const receivedQuotes: IndexerQuote[] = [];

  console.log(`👤 Taker: ${TAKER_ADDRESS}`);
  console.log(
    `🎯 Request: ${TOTAL_QUANTITY} contracts long, worst_price ${WORST_PRICE}`,
  );
  console.log(`🔗 WebSocket: ${WS_URL}`);
  console.log(`🆔 client_id: ${clientId}\n`);

  const ws = new WebSocket(WS_URL);

  await new Promise<void>((resolve, reject) => {
    ws.once("open", () => resolve());
    ws.once("error", reject);
  });
  console.log("✅ Connected");

  // Subscribe to quote stream for this taker
  ws.send(
    JSON.stringify({
      jsonrpc: "2.0",
      method: "subscribe",
      id: 1,
      params: {
        query: {
          stream: "quote",
          args: {
            addresses: [TAKER_ADDRESS],
            market_id: [INJUSDC_MARKET_ID],
          },
        },
      },
    }),
  );

  ws.on("message", (raw: Buffer) => {
    try {
      const msg = JSON.parse(raw.toString());
      const ack = msg?.result?.request_ack ?? msg?.result?.ack;
      if (ack?.rfq_id && ack.client_id === clientId) {
        rfqId = Number(ack.rfq_id);
        console.log(`   📬 request ACK: rfq_id=${rfqId} status=${ack.status}`);
        return;
      }

      const quote: IndexerQuote | undefined = msg?.result?.quote;
      if (quote && rfqId !== null && Number(quote.rfq_id) === rfqId) {
        receivedQuotes.push(quote);
        console.log(
          `   📩 quote from ${quote.maker.slice(0, 20)}... qty=${quote.quantity} price=${quote.price}`,
        );
      }
    } catch {
      /* ignore non-quote messages */
    }
  });

  // Send the RFQ request — client_id is caller-supplied, rfq_id comes back in ACK.
  // Direction is a lowercase string, not an integer.
  const request = {
    jsonrpc: "2.0",
    method: "create",
    id: Date.now(),
    params: {
      request: {
        client_id: clientId,
        market_id: INJUSDC_MARKET_ID,
        direction: "long",
        margin: TOTAL_MARGIN,
        quantity: TOTAL_QUANTITY.toString(),
        worst_price: WORST_PRICE.toFixed(3),
        request_address: TAKER_ADDRESS,
        expiry: Date.now() + 5 * 60 * 1000,
      },
    },
  };

  console.log("\n📤 Sending RFQ request...");
  ws.send(JSON.stringify(request));

  // Collect for the window
  console.log(`⏳ Collecting quotes for ${COLLECTION_WINDOW_MS}ms...`);
  await new Promise((r) => setTimeout(r, COLLECTION_WINDOW_MS));
  ws.close();

  console.log(`\n📦 Received ${receivedQuotes.length} quote(s)`);
  if (rfqId === null) {
    console.log("❌ No request ACK received — cannot settle without indexer-assigned rfq_id");
    return;
  }

  if (receivedQuotes.length === 0) {
    console.log("❌ No quotes — check maker whitelist, stream routing, balances");
    return;
  }

  // Select which quotes to accept
  const selected = selectMultiQuotes(receivedQuotes, TOTAL_QUANTITY, WORST_PRICE);
  console.log(
    `\n🏆 Selected ${selected.length} quote(s), cheapest-first:`,
  );
  selected.forEach((q, i) => {
    console.log(
      `   ${i + 1}. qty=${q.quantity.padStart(5)} price=${q.price.padStart(6)} from ${q.maker.slice(0, 20)}...`,
    );
  });

  if (selected.length === 0) {
    console.log("❌ No quotes within worst_price");
    return;
  }

  const aggregateQty = selected.reduce(
    (sum, q) => sum + Number(q.quantity),
    0,
  );
  const aggregateNotional = selected.reduce(
    (sum, q) => sum + Number(q.quantity) * Number(q.price),
    0,
  );
  const blendedPrice = aggregateNotional / aggregateQty;
  console.log(
    `\n📊 Aggregate: ${aggregateQty} contracts at blended price ${blendedPrice.toFixed(4)}`,
  );

  // Submit
  try {
    const txHash = await acceptQuotes(rfqId, INJUSDC_MARKET_ID, "long", selected);
    console.log(`\n✅ Settled. TxHash: ${txHash}`);
  } catch (e) {
    console.error(`\n❌ AcceptQuote failed:`, e);
  }
}

main().catch((e) => {
  console.error("Fatal:", e);
  process.exit(1);
});
