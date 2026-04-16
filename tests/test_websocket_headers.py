import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from google.protobuf.internal.decoder import _DecodeVarint32

from rfq_test.actors.market_maker import MarketMaker
from rfq_test.clients.websocket import MakerStreamClient, TakerStreamClient
from rfq_test.proto.rfq_messages import (
    CreateRFQRequestType,
    RFQProcessedQuoteType,
    RFQQuoteType,
    RFQSettlementLimitActionType,
    RFQSettlementType,
    _encode_message,
    _encode_string,
    _encode_uint64,
)


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


def test_create_rfq_request_encodes_expiry_as_submessage():
    request = CreateRFQRequestType(
        client_id="client-1",
        market_id="0xmarket",
        direction="long",
        margin="10",
        quantity="1",
        worst_price="100",
        expiry={"ts": 1234567890},
    )

    encoded = request.encode()

    pos = 0
    expiry_value = None
    while pos < len(encoded):
        tag_wire, pos = _DecodeVarint32(encoded, pos)
        field_num = tag_wire >> 3
        wire_type = tag_wire & 0x7
        if wire_type != 2:
            raise AssertionError(f"Unexpected wire type {wire_type} for request field {field_num}")

        length, pos = _DecodeVarint32(encoded, pos)
        value = encoded[pos:pos + length]
        pos += length
        if field_num == 7:
            expiry_value = value
            break

    assert expiry_value is not None

    pos = 0
    tag_wire, pos = _DecodeVarint32(expiry_value, pos)
    assert tag_wire >> 3 == 1
    timestamp, pos = _DecodeVarint32(expiry_value, pos)
    assert timestamp == 1234567890


@pytest.mark.asyncio
async def test_send_request_accepts_rfq_expiry_dict():
    client = TakerStreamClient("wss://example.test/injective_rfq_rpc.InjectiveRfqRPC")
    client._send_raw = AsyncMock()

    await client.send_request(
        {
            "client_id": "client-1",
            "market_id": "0xmarket",
            "direction": "long",
            "margin": "10",
            "quantity": "1",
            "worst_price": "100",
            "expiry": {"ts": 1234567890},
        }
    )

    client._send_raw.assert_awaited_once()


def test_processed_quote_decode_preserves_expiry_timestamp_and_height():
    expiry = _encode_uint64(1, 1234567890) + _encode_uint64(2, 321)
    encoded = b"".join(
        [
            _encode_string(1, "injective-888"),
            _encode_string(2, "inj1contract"),
            _encode_string(3, "0xmarket"),
            _encode_uint64(4, 999),
            _encode_string(10, "inj1maker"),
            _encode_string(13, "accepted"),
            _encode_message(9, expiry),
        ]
    )

    decoded = RFQProcessedQuoteType.decode(encoded)
    client = MakerStreamClient("wss://example.test/injective_rfq_rpc.InjectiveRfqRPC")
    as_dict = client._processed_quote_to_dict(decoded)

    assert decoded.expiry.timestamp == 1234567890
    assert decoded.expiry.height == 321
    assert as_dict["expiry"] == {"ts": 1234567890, "h": 321}
    assert as_dict["chain_id"] == "injective-888"
    assert as_dict["contract_address"] == "inj1contract"


def test_settlement_decode_preserves_unfilled_action_limit():
    limit_action = _encode_string(1, "3.25")
    unfilled_action = _encode_message(1, limit_action)
    encoded = b"".join(
        [
            _encode_uint64(1, 456),
            _encode_string(2, "0xmarket"),
            _encode_string(3, "inj1taker"),
            _encode_string(4, "long"),
            _encode_string(5, "10"),
            _encode_string(6, "1"),
            _encode_string(7, "3.5"),
            _encode_message(8, unfilled_action),
            _encode_string(16, "cid-456"),
        ]
    )

    decoded = RFQSettlementType.decode(encoded)
    client = MakerStreamClient("wss://example.test/injective_rfq_rpc.InjectiveRfqRPC")
    as_dict = client._settlement_to_dict(decoded)

    assert decoded.unfilled_action is not None
    assert isinstance(decoded.unfilled_action.limit, RFQSettlementLimitActionType)
    assert decoded.unfilled_action.limit.price == "3.25"
    assert as_dict["unfilled_action"] == {"limit": {"price": "3.25"}}
    assert as_dict["cid"] == "cid-456"
