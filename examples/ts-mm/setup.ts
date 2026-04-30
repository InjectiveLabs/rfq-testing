/**
 * RFQ – Market Maker Initialization Script
 *
 * Flow:
 * 1. MM grants permissions to RFQ contract
 * 2. Admin registers MM on RFQ contract
 *
 * This is a ONE-TIME setup for demo / testing.
 */

import {
  MsgGrant,
  MsgBroadcasterWithPk,
  MsgExecuteContractCompat,
  PrivateKey,
} from "@injectivelabs/sdk-ts";
import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import type { ChainId } from "@injectivelabs/ts-types";
import dotenv from "dotenv";

dotenv.config();

/* -------------------------------------------------------------------------- */
/*                                   CONFIG                                   */
/* -------------------------------------------------------------------------- */

// Contract
const CONTRACT_ADDRESS = process.env.CONTRACT_ADDRESS!;

// Private keys
const ADMIN_PRIVATE_KEY = process.env.ADMIN_PRIVATE_KEY!;
const MM_PRIVATE_KEY = process.env.MM_PRIVATE_KEY!;

// Derived addresses
const ADMIN_ADDRESS = PrivateKey.fromHex(ADMIN_PRIVATE_KEY).toBech32();
const MM_ADDRESS = PrivateKey.fromHex(MM_PRIVATE_KEY).toBech32();

// Optional external settlement contract (not used for now)
const SETTLEMENT_CONTRACT = null;

// Network
const NETWORK = (process.env.NETWORK as Network) || Network.TestnetSentry;
const CHAIN_ID = (process.env.CHAIN_ID || "injective-888") as ChainId;
const ENDPOINTS = getNetworkEndpoints(NETWORK);

/* -------------------------------------------------------------------------- */
/*                              ENV VALIDATION                                */
/* -------------------------------------------------------------------------- */

if (!CONTRACT_ADDRESS) throw new Error("CONTRACT_ADDRESS not set");
if (!ADMIN_PRIVATE_KEY) throw new Error("ADMIN_PRIVATE_KEY not set");
if (!MM_PRIVATE_KEY) throw new Error("MM_PRIVATE_KEY not set");

/* -------------------------------------------------------------------------- */
/*                                   SCRIPT                                   */
/* -------------------------------------------------------------------------- */

async function main() {
  console.log("🚀 RFQ Market Maker Setup\n");

  /* ------------------------------------------------------------------------ */
  /* Step 1: MM grants permissions to RFQ contract                             */
  /* ------------------------------------------------------------------------ */

  console.log("Step 1: MM grants permissions to RFQ contract");

  /**
   * RFQ contract needs:
   * - MsgPrivilegedExecuteContract → settle synthetic trades
   * - MsgSend → move funds if needed
   */
  const GRANTED_MSG_TYPES = [
    "/injective.exchange.v2.MsgPrivilegedExecuteContract",
    "/cosmos.bank.v1beta1.MsgSend",
  ];

  const grantMsgs = GRANTED_MSG_TYPES.map((type) =>
    MsgGrant.fromJSON({
      granter: MM_ADDRESS,
      grantee: CONTRACT_ADDRESS,
      messageType: type,
    })
  );

  const mmBroadcaster = new MsgBroadcasterWithPk({
    privateKey: MM_PRIVATE_KEY,
    network: NETWORK,
    chainId: CHAIN_ID,
    endpoints: ENDPOINTS,
  });

  const grantTx = await mmBroadcaster.broadcast({
    msgs: grantMsgs,
  });

  console.log("✅ Permissions granted:");
  GRANTED_MSG_TYPES.forEach((t) => console.log(" -", t));
  console.log("TxHash:", grantTx.txHash, "\n");

  /* ------------------------------------------------------------------------ */
  /* Step 2: Admin registers MM on RFQ contract                                */
  /* ------------------------------------------------------------------------ */

  console.log("Step 2: Admin registers Market Maker");

  const registerMakerMsg = MsgExecuteContractCompat.fromJSON({
    contractAddress: CONTRACT_ADDRESS,
    sender: ADMIN_ADDRESS,
    msg: {
      register_maker: {
        maker: MM_ADDRESS,
        settlement_contract: SETTLEMENT_CONTRACT,
      },
    },
    funds: [],
  });

  const adminBroadcaster = new MsgBroadcasterWithPk({
    privateKey: ADMIN_PRIVATE_KEY,
    network: NETWORK,
    chainId: CHAIN_ID,
    endpoints: ENDPOINTS,
  });

  const registerTx = await adminBroadcaster.broadcast({
    msgs: registerMakerMsg,
  });

  console.log("✅ Maker registered");
  console.log("TxHash:", registerTx.txHash);

  /* ------------------------------------------------------------------------ */

  console.log("\n🎉 RFQ MM setup completed successfully");
}

/* -------------------------------------------------------------------------- */
/*                                   ENTRY                                    */
/* -------------------------------------------------------------------------- */

main().catch((err) => {
  console.error("❌ Setup failed");
  console.error(err);
  process.exit(1);
});
