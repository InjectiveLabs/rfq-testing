import asyncio
import json
import os
import dotenv
import time

from pyinjective.async_client_v2 import AsyncClient
from pyinjective.core.broadcaster import MsgBroadcasterWithPk
from pyinjective.core.network import Network
from pyinjective.wallet import PrivateKey

dotenv.load_dotenv()

def must_env(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"{key} not set")
    return v

async def main():
    print("🚀 RFQ Market Maker Setup\n")

    CONTRACT_ADDRESS = must_env("CONTRACT_ADDRESS")
    ADMIN_PRIVATE_KEY = must_env("ADMIN_PRIVATE_KEY")
    MM_PRIVATE_KEY = must_env("MM_PRIVATE_KEY")
    CHAIN_ID = must_env("CHAIN_ID")

    # --------------------------------------------------
    # Network
    # --------------------------------------------------

    network = Network.local()  # or testnet()
    network.chain_id = CHAIN_ID

    client = AsyncClient(network)
    composer = await client.composer()

    # --------------------------------------------------
    # MM Broadcaster (AUTHZ)
    # --------------------------------------------------

    gas_price = await client.current_chain_gas_price()

    mm_broadcaster = MsgBroadcasterWithPk.new_using_gas_heuristics(
        network=network,
        private_key=MM_PRIVATE_KEY,
        gas_price=gas_price,
        client=client,
        composer=composer,
    )

    mm_priv = PrivateKey.from_hex(MM_PRIVATE_KEY)
    mm_addr = mm_priv.to_public_key().to_address()

    print("Step 1: MM grants permissions")
    print("MM address:", mm_addr.to_acc_bech32())

    # --------------------------------------------------
    # STEP 1 — MM grants authz
    # --------------------------------------------------

    granted_msgs = [
        "/injective.exchange.v2.MsgPrivilegedExecuteContract",
        "/cosmos.bank.v1beta1.MsgSend",
    ]

    authz_msgs = []

    for msg_type in granted_msgs:
        authz_msgs.append(
            composer.msg_grant_generic(
                granter=mm_addr.to_acc_bech32(),
                grantee=CONTRACT_ADDRESS,
                msg_type=msg_type,
                expire_in=int(time.time() + 10*3600*24)
            )
        )

    result = await mm_broadcaster.broadcast(authz_msgs)
    print("✅ Permissions granted")
    print("TxHash:", result['txResponse']['txhash'])

    # --------------------------------------------------
    # Admin Broadcaster (REGISTER MAKER)
    # This must be admin to execute this, we just include this here so that it's simple to setup on local
    # --------------------------------------------------
    admin_client = AsyncClient(network)
    gas_price = await admin_client.current_chain_gas_price()

    admin_broadcaster = MsgBroadcasterWithPk.new_using_gas_heuristics(
        network=network,
        private_key=ADMIN_PRIVATE_KEY,
        gas_price=gas_price,
        client=admin_client,
        composer=composer,
    )

    admin_priv = PrivateKey.from_hex(ADMIN_PRIVATE_KEY)
    admin_addr = admin_priv.to_public_key().to_address()

    print("\nStep 2: Admin registers Market Maker")

    exec_msg = {
        "register_maker": {
            "maker": mm_addr.to_acc_bech32(),
        }
    }

    wasm_msg = composer.msg_execute_contract(
        sender=admin_addr.to_acc_bech32(),
        contract=CONTRACT_ADDRESS,
        msg=json.dumps(exec_msg),
        funds=[],
    )

    result = await admin_broadcaster.broadcast([wasm_msg])

    print("✅ Maker registered")
    print("TxHash:", result['txResponse']['txhash'])

    print("\n🎉 RFQ MM setup completed successfully")

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
