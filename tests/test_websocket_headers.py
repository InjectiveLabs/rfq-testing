from unittest.mock import AsyncMock, patch

import pytest

from rfq_test.actors.market_maker import MarketMaker
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient


class DummyTask:
    def __init__(self, coro):
        coro.close()

    def cancel(self) -> None:
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class FakeWebSocket:
    close_code = None

    async def send(self, _data) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_taker_stream_sends_request_address_header():
    fake_ws = FakeWebSocket()
    connect_mock = AsyncMock(return_value=fake_ws)

    with patch("rfq_test.clients.websocket.websockets.connect", connect_mock), patch(
        "rfq_test.clients.websocket.asyncio.create_task",
        side_effect=lambda coro: DummyTask(coro),
    ):
        client = TakerStreamClient(
            "wss://example.test/injective_rfq_rpc.InjectiveRfqRPC",
            request_address="inj1taker",
        )
        await client.connect()

    assert connect_mock.await_args.kwargs["additional_headers"] == {
        "request_address": "inj1taker",
    }


@pytest.mark.asyncio
async def test_maker_stream_sends_supported_metadata_headers():
    fake_ws = FakeWebSocket()
    connect_mock = AsyncMock(return_value=fake_ws)

    with patch("rfq_test.clients.websocket.websockets.connect", connect_mock), patch(
        "rfq_test.clients.websocket.asyncio.create_task",
        side_effect=lambda coro: DummyTask(coro),
    ):
        client = MakerStreamClient(
            "wss://example.test/injective_rfq_rpc.InjectiveRfqRPC",
            maker_address="inj1maker",
            subscribe_to_quotes_updates=True,
            subscribe_to_settlement_updates=True,
        )
        await client.connect()

    assert connect_mock.await_args.kwargs["additional_headers"] == {
        "maker_address": "inj1maker",
        "subscribe_to_quotes_updates": "true",
        "subscribe_to_settlement_updates": "true",
    }


@pytest.mark.asyncio
async def test_market_maker_passes_wallet_address_to_stream_client():
    wallet = type("WalletStub", (), {"inj_address": "inj1maker"})()

    with patch("rfq_test.actors.market_maker.MakerStreamClient") as stream_client_cls:
        stream_client = AsyncMock()
        stream_client_cls.return_value = stream_client

        maker = MarketMaker(
            wallet=wallet,
            ws_url="wss://example.test/injective_rfq_rpc.InjectiveRfqRPC",
            subscribe_to_quotes_updates=True,
            subscribe_to_settlement_updates=True,
        )
        await maker.connect()

    stream_client_cls.assert_called_once_with(
        "wss://example.test/injective_rfq_rpc.InjectiveRfqRPC",
        maker_address="inj1maker",
        subscribe_to_quotes_updates=True,
        subscribe_to_settlement_updates=True,
    )
    stream_client.connect.assert_awaited_once()
