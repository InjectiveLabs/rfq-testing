from eth_account import Account

from rfq_test.crypto.eip712 import (
    sign_conditional_order_v2,
    sign_quote_digest,
    sign_quote_v2,
    signed_taker_intent_digest,
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

    assert digest.hex() == "c3b67f4ba971fffdd3cab3270207d47f5e3ab886fcb951ce8bb49a65dcc342b3"
    assert signature == (
        "0xa743a5b448cd163a5054c558f1a71d6c626d05a80d45fd1d6e485b5c9e765452"
        "65b83b50d6970dfd7ccdc6f2f151f7891a0a50fb9c6ba88b0515ece648259ee700"
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

    assert digest.hex() == "c0f6f749dd69b99249d0cac24b7579c63f6f582f518de0117859d8a806d03c51"
    assert signature == (
        "0x315bc1d16291a9496a8783fcdc41eb51c4b46416e78ec79cb37c183f5ae788f1"
        "63be0b92b10fc7629ad7bcfa2dfd8908c05e45bb957c8c279081b098cef9155900"
    )
    assert bytes.fromhex(signature[2:])[-1] in (0, 1)
    assert Account._recover_hash(digest, signature=bytes.fromhex(signature[2:])) == ETH_ADDRESS
