import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from rfq_test.actors.market_maker import MarketMaker
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient
from rfq_test.proto.rfq_messages import RFQProcessedQuoteType, RFQQuoteType, RFQSettlementType


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


@pytest.mark.asyncio
async def test_wait_for_quote_update_requeues_other_events():
    client = MakerStreamClient("wss://example.test/injective_rfq_rpc.InjectiveRfqRPC")
    settlement = RFQSettlementType(rfq_id=123, cid="cid-123")
    quote_update = RFQProcessedQuoteType(
        rfq_id=123,
        status="accepted",
        executed_quantity="1",
        maker="inj1maker",
    )

    await client._message_queue.put(("settlement_update", settlement))

    async def enqueue_quote_update():
        await asyncio.sleep(0.02)
        await client._message_queue.put(("quote_update", quote_update))

    task = asyncio.create_task(enqueue_quote_update())
    try:
        result = await client.wait_for_quote_update(123, timeout=1.0)
    finally:
        await task

    assert result["rfq_id"] == "123"
    assert result["status"] == "accepted"
    assert result["executed_quantity"] == "1"

    queued_type, queued_data = await client._message_queue.get()
    assert queued_type == "settlement_update"
    assert queued_data.cid == "cid-123"


@pytest.mark.asyncio
async def test_wait_for_settlement_update_requeues_other_events():
    client = MakerStreamClient("wss://example.test/injective_rfq_rpc.InjectiveRfqRPC")
    quote_update = RFQProcessedQuoteType(rfq_id=456, status="accepted")
    settlement = RFQSettlementType(rfq_id=456, cid="cid-456", height=999)

    await client._message_queue.put(("quote_update", quote_update))

    async def enqueue_settlement_update():
        await asyncio.sleep(0.02)
        await client._message_queue.put(("settlement_update", settlement))

    task = asyncio.create_task(enqueue_settlement_update())
    try:
        result = await client.wait_for_settlement_update(456, timeout=1.0)
    finally:
        await task

    assert result["rfq_id"] == "456"
    assert result["cid"] == "cid-456"
    assert result["height"] == 999

    queued_type, queued_data = await client._message_queue.get()
    assert queued_type == "quote_update"
    assert queued_data.status == "accepted"


def test_rfq_quote_type_round_trip_decode():
    quote = RFQQuoteType(
        chain_id="injective-888",
        contract_address="inj1contract",
        market_id="0xmarket",
        rfq_id=789,
        taker_direction="long",
        margin="10",
        quantity="1",
        price="4.5",
        expiry=1234567890,
        maker="inj1maker",
        taker="inj1taker",
        signature="0xsig",
        status="pending",
        event_time=999,
        transaction_time=1000,
    )

    decoded = RFQQuoteType.decode(quote.encode())

    assert decoded.rfq_id == 789
    assert decoded.market_id == "0xmarket"
    assert decoded.maker == "inj1maker"
    assert decoded.taker == "inj1taker"
    assert decoded.expiry == 1234567890
