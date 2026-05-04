from eth_account import Account

from rfq_test.crypto.eip712 import (
    sign_conditional_order_v2,
    sign_maker_challenge_v2,
    sign_quote_digest,
    sign_quote_v2,
    signed_taker_intent_digest,
    stream_auth_challenge_digest,
)


PRIVATE_KEY = "0x" + "11" * 32
ADDRESS = "inj1r8n7xah8cgfm0el8u3kvwzja6zrd4le2krtp7d"
ETH_ADDRESS = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
CONTRACT = "inj19g43wyj843ydkc845dcdea6su4mgfjwnpjz6h5"


def test_sign_quote_v2_matches_ws_client_reference():
    digest = sign_quote_digest(
        evm_chain_id=1439,
        verifying_contract_bech32=CONTRACT,
        market_id="0xmarket",
        rfq_id=123,
        taker_bech32=ADDRESS,
        direction="long",
        taker_margin="10",
        taker_quantity="1",
        maker_bech32=ADDRESS,
        maker_subaccount_nonce=0,
        maker_quantity="1",
        maker_margin="10",
        price="3.000250787727220160",
        expiry_kind=0,
        expiry_value=1772851186901,
        min_fill_quantity="0.001",
    )
    signature = sign_quote_v2(
        private_key=PRIVATE_KEY,
        evm_chain_id=1439,
        verifying_contract_bech32=CONTRACT,
        market_id="0xmarket",
        rfq_id=123,
        taker=ADDRESS,
        direction="long",
        taker_margin="10",
        taker_quantity="1",
        maker=ADDRESS,
        maker_subaccount_nonce=0,
        maker_quantity="1",
        maker_margin="10",
        price="3.000250787727220160",
        expiry_ms=1772851186901,
        min_fill_quantity="0.001",
    )

    assert digest.hex() == "8921e261b8a81e3e5d41df57db50e4c956c6ecf45d49427a25048c67de8d077e"
    assert signature == (
        "0xd917f7d61959b08889c2ae61a18fe6bfd1afb0a9b18b284989574f70493015cd5"
        "d9e25ba6acf1dd7fd2013ea0c72fea1d036dabc0873fe8ecdb8a8aaaf1b2a4e00"
    )
    assert bytes.fromhex(signature[2:])[-1] in (0, 1)
    assert Account._recover_hash(digest, signature=bytes.fromhex(signature[2:])) == ETH_ADDRESS


def test_signed_taker_intent_v2_matches_ws_client_reference():
    digest = signed_taker_intent_digest(
        evm_chain_id=1439,
        verifying_contract_bech32=CONTRACT,
        version=1,
        taker_bech32=ADDRESS,
        epoch=1,
        rfq_id=456,
        market_id="0xmarket",
        subaccount_nonce=0,
        lane_version=1,
        deadline_ms=1772851186901,
        direction="short",
        quantity="1",
        margin="0",
        worst_price="2.5",
        min_total_fill_quantity="1",
        trigger_type="mark_price_gte",
        trigger_price="3",
    )
    signature = sign_conditional_order_v2(
        private_key=PRIVATE_KEY,
        evm_chain_id=1439,
        verifying_contract_bech32=CONTRACT,
        version=1,
        taker=ADDRESS,
        epoch=1,
        rfq_id=456,
        market_id="0xmarket",
        subaccount_nonce=0,
        lane_version=1,
        deadline_ms=1772851186901,
        direction="short",
        quantity="1",
        margin="0",
        worst_price="2.5",
        min_total_fill_quantity="1",
        trigger_type="mark_price_gte",
        trigger_price="3",
    )

    assert digest.hex() == "4173384ed7ac0b850b7f2be95667aa2b17b41da527b7058848a6949d202763ee"
    assert signature == (
        "0x47f88e7957f011a075acef768839c7a21e4fdaf50742a2ba6dbf240fe0d0dae61"
        "9c10b86a6bd3ff535bba5e8445c22407b177d8f50aab95412fff76c0ca714a600"
    )
    assert bytes.fromhex(signature[2:])[-1] in (0, 1)
    assert Account._recover_hash(digest, signature=bytes.fromhex(signature[2:])) == ETH_ADDRESS


def test_stream_auth_challenge_v2_matches_reference():
    digest = stream_auth_challenge_digest(
        evm_chain_id=1439,
        verifying_contract_bech32=CONTRACT,
        maker_bech32=ADDRESS,
        nonce_hex="0x" + "22" * 32,
        expires_at=1772851186901,
    )
    signature = sign_maker_challenge_v2(
        private_key=PRIVATE_KEY,
        evm_chain_id=1439,
        verifying_contract_bech32=CONTRACT,
        maker=ADDRESS,
        nonce_hex="0x" + "22" * 32,
        expires_at=1772851186901,
    )

    assert digest.hex() == "ed164d3658274b3f2089006be054bea0d408c52e45673069709da1990f72e0ce"
    assert signature == (
        "0x724f3ebc4d4f6c75165514f86796bb021cde12de70f00242e51556e047e3c15d"
        "06eb3ace5d88af86989badc4b4ca3e0405f54c4d60b1e8562727c008bd347f0d01"
    )
    assert bytes.fromhex(signature[2:])[-1] in (0, 1)
    assert Account._recover_hash(digest, signature=bytes.fromhex(signature[2:])) == ETH_ADDRESS
