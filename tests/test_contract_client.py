import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from rfq_test.clients.contract import ContractClient
from rfq_test.crypto.signing import sign_quote
from rfq_test.models.types import Direction


class FakeNetwork:
    def string(self) -> str:
        return "testnet"


class FakeComposer:
    last_msg = None

    def __init__(self, network: str):
        self.network = network

    def msg_execute_contract(self, sender: str, contract: str, msg: str):
        FakeComposer.last_msg = {
            "sender": sender,
            "contract": contract,
            "msg": msg,
        }
        return FakeComposer.last_msg


class FakeBroadcaster:
    async def broadcast(self, messages):
        return SimpleNamespace(txResponse=SimpleNamespace(txhash="ABC123", code=0))


@pytest.mark.asyncio
async def test_accept_quote_normalizes_expiry_and_signature_for_contract():
    FakeComposer.last_msg = None
    client = ContractClient(
        contract_config=SimpleNamespace(address="inj1contract"),
        chain_config=SimpleNamespace(lcd_endpoint="https://lcd.test"),
    )

    with patch("pyinjective.composer_v2.Composer", FakeComposer), patch(
        "pyinjective.core.broadcaster.MsgBroadcasterWithPk.new_using_simulation",
        return_value=FakeBroadcaster(),
    ), patch.object(client, "_get_network", AsyncMock(return_value=FakeNetwork())), patch.object(
        client,
        "_wait_for_tx_result",
        AsyncMock(return_value={"code": 0, "rawLog": ""}),
    ), patch("rfq_test.clients.contract._get_sender_address", return_value="inj1sender"):
        tx_hash = await client.accept_quote(
            private_key="0x" + "11" * 32,
            quotes=[
                {
                    "maker": "inj1maker",
                    "margin": "10",
                    "quantity": "1",
                    "price": "2.5",
                    "expiry": 1772851186901,
                    "signature": "0x" + "ab" * 65,
                }
            ],
            rfq_id="1772850886884",
            market_id="0xmarket",
            direction=Direction.LONG,
            margin="10",
            quantity="1",
            worst_price="3.0",
            unfilled_action={"market": {}},
            cid="tc-cli-123",
        )

    assert tx_hash == "ABC123"
    assert FakeComposer.last_msg is not None

    payload = json.loads(FakeComposer.last_msg["msg"])
    accept_quote = payload["accept_quote"]
    quote = accept_quote["quotes"][0]

    assert accept_quote["rfq_id"] == 1772850886884
    assert accept_quote["cid"] == "tc-cli-123"
    assert quote["expiry"] == {"ts": 1772851186901}
    assert quote["signature"] != "0x" + "ab" * 65
    assert quote["signature"].endswith("=")


def test_sign_quote_preserves_exact_price_string():
    kwargs = dict(
        private_key="11" * 32,
        rfq_id="123",
        market_id="0xmarket",
        direction="long",
        taker="inj1taker",
        taker_margin="10",
        taker_quantity="1",
        maker="inj1maker",
        maker_margin="10",
        maker_quantity="1",
        expiry=1772851186901,
        chain_id="injective-888",
        contract_address="inj1contract",
    )

    sig_with_trailing_zero = sign_quote(price="3.000250787727220160", **kwargs)
    sig_without_trailing_zero = sign_quote(price="3.00025078772722016", **kwargs)

    assert sig_with_trailing_zero != sig_without_trailing_zero
