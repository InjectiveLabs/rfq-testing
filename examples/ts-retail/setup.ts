/**
 * RFQ – Retail User Initialization Script
 *
 * Flow:
 * 1. Retail user grants permissions to RFQ contract
 *
 * This is a ONE-TIME setup for demo / testing.
 */

import {
  MsgGrant,
  MsgBroadcasterWithPk,
  PrivateKey,
} from "@injectivelabs/sdk-ts";
import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import dotenv from "dotenv";

dotenv.config();

/* -------------------------------------------------------------------------- */
/*                                   CONFIG                                   */
/* -------------------------------------------------------------------------- */

// Contract
const CONTRACT_ADDRESS = process.env.CONTRACT_ADDRESS!;

// Retail private key
const RETAIL_PRIVATE_KEY = process.env.RETAIL_PRIVATE_KEY!;

// Derived retail address
const RETAIL_ADDRESS = PrivateKey.fromHex(RETAIL_PRIVATE_KEY).toBech32();

// Network
const NETWORK = Network.Local;
const CHAIN_ID = "injective-777";
const ENDPOINTS = getNetworkEndpoints(NETWORK);

/* -------------------------------------------------------------------------- */
/*                              ENV VALIDATION                                */
/* -------------------------------------------------------------------------- */

if (!CONTRACT_ADDRESS) throw new Error("CONTRACT_ADDRESS not set");
if (!RETAIL_PRIVATE_KEY) throw new Error("RETAIL_PRIVATE_KEY not set");

/* -------------------------------------------------------------------------- */
/*                                   SCRIPT                                   */
/* -------------------------------------------------------------------------- */

async function main() {
  console.log("🚀 RFQ Retail User Setup\n");

  /* ------------------------------------------------------------------------ */
  /* Step 1: Retail user grants permissions to RFQ contract                   */
  /* ------------------------------------------------------------------------ */

  console.log("Step 1: Retail grants permissions to RFQ contract");

  /**
   * RFQ contract needs:
   * - MsgPrivilegedExecuteContract   → settle synthetic trades
   * - MsgCreateDerivativeLimitOrder,MsgCreateDerivativeMarketOrder → place / cancel limit orders on behalf of user
   * - MsgSend                        → auto use necessary fund
   */
  const GRANTED_MSG_TYPES = [
    "/injective.exchange.v2.MsgPrivilegedExecuteContract",
    "/injective.exchange.v2.MsgCreateDerivativeMarketOrder",
    "/injective.exchange.v2.MsgCreateDerivativeLimitOrder",
    "/cosmos.bank.v1beta1.MsgSend",
  ];

  const grantMsgs = GRANTED_MSG_TYPES.map((type) =>
    MsgGrant.fromJSON({
      granter: RETAIL_ADDRESS,
      grantee: CONTRACT_ADDRESS,
      messageType: type,
    })
  );

  const retailBroadcaster = new MsgBroadcasterWithPk({
    privateKey: RETAIL_PRIVATE_KEY,
    network: NETWORK,
    chainId: CHAIN_ID,
    endpoints: ENDPOINTS,
  });

  const grantTx = await retailBroadcaster.broadcast({
    msgs: grantMsgs,
  });

  console.log("✅ Permissions granted:");
  GRANTED_MSG_TYPES.forEach((t) => console.log(" -", t));
  console.log("TxHash:", grantTx.txHash);

  /* ------------------------------------------------------------------------ */

  console.log("\n🎉 RFQ retail setup completed successfully");
}

/* -------------------------------------------------------------------------- */
/*                                   ENTRY                                    */
/* -------------------------------------------------------------------------- */

main().catch((err) => {
  console.error("❌ Setup failed");
  console.error(err);
  process.exit(1);
});
