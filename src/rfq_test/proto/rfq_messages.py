"""RFQ API protobuf message types.

Manual protobuf encoding/decoding for the RFQ API messages.
This avoids code generation and external dependencies beyond the standard protobuf library.

Field layouts match the injective_rfq_rpc service proto:
- CreateRFQRequestType: field 1 = client_id (string), 7 fields total
- RFQRequestType: field 1 = client_id (string), field 2 = rfq_id (uint64), shifted +1
- RFQQuoteType: fields 1-20; fields 19=maker_subaccount_nonce, 20=min_fill_quantity added in V2
- RequestStreamAck: field 1 = rfq_id, field 2 = client_id, field 3 = status
- QuoteStreamAck: field 1 = rfq_id, field 2 = status
- ConditionalOrderInput: fields 1-20; sent inside TakerStreamRequest for TP/SL orders
- TakerStreamRequest: field 3 = conditional_order, field 4 = conditional_order_signature
- TakerStreamResponse: field 5 = conditional_order_ack
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from google.protobuf.internal.encoder import _VarintBytes
from google.protobuf.internal.decoder import _DecodeVarint, _DecodeVarint32


def _encode_varint(value: int) -> bytes:
    """Encode an integer as a varint."""
    return _VarintBytes(value)


def _encode_string(field_num: int, value: str) -> bytes:
    """Encode a string field."""
    if not value:
        return b""
    encoded = value.encode("utf-8")
    tag = (field_num << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(encoded)) + encoded


def _encode_uint64(field_num: int, value: int) -> bytes:
    """Encode a uint64 field."""
    if value == 0:
        return b""
    if value < 0:
        return b""
    tag = (field_num << 3) | 0
    return _encode_varint(tag) + _encode_varint(value)


def _encode_sint64(field_num: int, value: int) -> bytes:
    """Encode a sint64 field (zigzag encoding)."""
    if value == 0:
        return b""
    encoded_value = (value << 1) ^ (value >> 63)
    tag = (field_num << 3) | 0
    return _encode_varint(tag) + _encode_varint(encoded_value)


def _encode_message(field_num: int, message_bytes: bytes) -> bytes:
    """Encode an embedded message field."""
    if not message_bytes:
        return b""
    tag = (field_num << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(message_bytes)) + message_bytes


def _decode_expiry_submessage(data: bytes) -> int:
    """Decode RFQExpiryType sub-message -> return timestamp (field 1) or height (field 2)."""
    pos = 0
    timestamp = 0
    while pos < len(data):
        tag_wire, new_pos = _DecodeVarint32(data, pos)
        field_num = tag_wire >> 3
        pos = new_pos
        value, pos = _DecodeVarint(data, pos)
        if field_num == 1:
            timestamp = value
        elif field_num == 2 and timestamp == 0:
            timestamp = value
    return timestamp


def _decode_expiry_type(data: bytes) -> "RFQExpiryType":
    """Decode RFQExpiryType sub-message preserving both timestamp and height."""
    return RFQExpiryType.decode(data)


def _decode_zigzag(value: int) -> int:
    """Decode protobuf zigzag-encoded sint64."""
    return (value >> 1) ^ -(value & 1)


def _encode_expiry_submessage(expiry: int | dict | "RFQExpiryType") -> bytes:
    """Encode RFQExpiryType with either timestamp or height."""
    timestamp = 0
    height = 0

    if isinstance(expiry, RFQExpiryType):
        timestamp = expiry.timestamp
        height = expiry.height
    elif isinstance(expiry, dict):
        timestamp = int(expiry.get("ts") or expiry.get("timestamp") or 0)
        height = int(expiry.get("h") or expiry.get("height") or 0)
    elif expiry:
        timestamp = int(expiry)

    result = b""
    result += _encode_uint64(1, timestamp)
    result += _encode_uint64(2, height)
    return result


# ============================================================
# Core Types
# ============================================================


@dataclass
class RFQExpiryType:
    """Expiry represented by timestamp or block height."""

    timestamp: int = 0
    height: int = 0

    def encode(self) -> bytes:
        return _encode_expiry_submessage(self)

    @classmethod
    def decode(cls, data: bytes) -> "RFQExpiryType":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type != 0:
                break

            value, pos = _DecodeVarint(data, pos)
            if field_num == 1:
                result.timestamp = value
            elif field_num == 2:
                result.height = value

        return result

@dataclass
class CreateRFQRequestType:
    """Create-request message for TakerStream.
    
    Fields: 1=client_id, 2=market_id, 3=direction, 4=margin, 5=quantity,
    6=worst_price, 7=expiry.
    """
    client_id: str = ""
    market_id: str = ""
    direction: str = ""
    margin: str = ""
    quantity: str = ""
    worst_price: str = ""
    expiry: int | dict | RFQExpiryType = 0

    def encode(self) -> bytes:
        result = b""
        result += _encode_string(1, self.client_id)
        result += _encode_string(2, self.market_id)
        result += _encode_string(3, self.direction)
        result += _encode_string(4, self.margin)
        result += _encode_string(5, self.quantity)
        result += _encode_string(6, self.worst_price)
        expiry_inner = _encode_expiry_submessage(self.expiry)
        result += _encode_message(7, expiry_inner)
        return result


@dataclass
class RFQRequestType:
    """RFQ request message received by makers.
    
    Fields: 1=client_id, 2=rfq_id, 3=market_id, 4=direction, 5=margin,
    6=quantity, 7=worst_price, 8=request_address, 9=expiry, 10=status,
    11=created_at, 12=updated_at, 13=transaction_time, 14=height.
    """
    client_id: str = ""
    rfq_id: int = 0
    market_id: str = ""
    direction: str = ""
    margin: str = ""
    quantity: str = ""
    worst_price: str = ""
    request_address: str = ""
    expiry: int = 0
    status: str = ""
    created_at: int = 0
    updated_at: int = 0
    transaction_time: int = 0
    height: int = 0

    @classmethod
    def decode(cls, data: bytes) -> "RFQRequestType":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 0:  # Varint
                if field_num in (2, 9, 13, 14):
                    value, pos = _DecodeVarint(data, pos)
                    if field_num == 2:
                        result.rfq_id = value
                    elif field_num == 9:
                        result.expiry = value
                    elif field_num == 13:
                        result.transaction_time = value
                    elif field_num == 14:
                        result.height = value
                else:
                    value, pos = _DecodeVarint(data, pos)
                    if field_num == 11:
                        result.created_at = (value >> 1) ^ -(value & 1)
                    elif field_num == 12:
                        result.updated_at = (value >> 1) ^ -(value & 1)
            elif wire_type == 2:  # Length-delimited
                length, pos = _DecodeVarint32(data, pos)
                value = data[pos:pos + length].decode("utf-8")
                pos += length
                if field_num == 1:
                    result.client_id = value
                elif field_num == 3:
                    result.market_id = value
                elif field_num == 4:
                    result.direction = value
                elif field_num == 5:
                    result.margin = value
                elif field_num == 6:
                    result.quantity = value
                elif field_num == 7:
                    result.worst_price = value
                elif field_num == 8:
                    result.request_address = value
                elif field_num == 9:
                    result.expiry = _decode_expiry_submessage(value_bytes)
                elif field_num == 10:
                    result.status = value

        return result


@dataclass
class RFQQuoteType:
    """RFQ quote message sent by makers and received by takers.

    Fields: 1=chain_id, 2=contract_address, 3=market_id, 4=rfq_id,
    5=taker_direction, 6=margin, 7=quantity, 8=price, 9=expiry,
    10=maker, 11=taker, 12=signature, 13=status, 14=created_at,
    15=updated_at, 16=height, 17=event_time, 18=transaction_time,
    19=maker_subaccount_nonce, 20=min_fill_quantity.

    Fields 19 and 20 are V2 additions. maker_subaccount_nonce must match
    the nonce used when the maker was registered. min_fill_quantity is
    optional and sets the minimum partial fill the maker will accept.
    Both fields must also be included in the signed payload (see signing.py).
    """
    market_id: str = ""
    rfq_id: int = 0
    taker_direction: str = ""
    margin: str = ""
    quantity: str = ""
    price: str = ""
    expiry: int = 0
    maker: str = ""
    taker: str = ""
    signature: str = ""
    status: str = ""
    created_at: int = 0
    updated_at: int = 0
    height: int = 0
    event_time: int = 0
    transaction_time: int = 0
    chain_id: str = ""
    contract_address: str = ""
    maker_subaccount_nonce: int = 0
    min_fill_quantity: str = ""

    def encode(self) -> bytes:
        result = b""
        result += _encode_string(1, self.chain_id)
        result += _encode_string(2, self.contract_address)
        result += _encode_string(3, self.market_id)
        result += _encode_uint64(4, self.rfq_id)
        result += _encode_string(5, self.taker_direction)
        result += _encode_string(6, self.margin)
        result += _encode_string(7, self.quantity)
        result += _encode_string(8, self.price)
        expiry_inner = _encode_uint64(1, self.expiry)
        result += _encode_message(9, expiry_inner)
        result += _encode_string(10, self.maker)
        result += _encode_string(11, self.taker)
        result += _encode_string(12, self.signature)
        result += _encode_string(13, self.status)
        result += _encode_sint64(14, self.created_at)
        result += _encode_sint64(15, self.updated_at)
        result += _encode_uint64(16, self.height)
        result += _encode_uint64(17, self.event_time)
        result += _encode_uint64(18, self.transaction_time if self.transaction_time else 0)
        result += _encode_uint64(19, self.maker_subaccount_nonce)
        result += _encode_string(20, self.min_fill_quantity)
        return result

    @classmethod
    def decode(cls, data: bytes) -> "RFQQuoteType":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 0:  # Varint
                if field_num in (4, 16, 17, 18, 19):
                    value, pos = _DecodeVarint(data, pos)
                    if field_num == 4:
                        result.rfq_id = value
                    elif field_num == 16:
                        result.height = value
                    elif field_num == 17:
                        result.event_time = value
                    elif field_num == 18:
                        result.transaction_time = value
                    elif field_num == 19:
                        result.maker_subaccount_nonce = value
                else:
                    value, pos = _DecodeVarint32(data, pos)
                    if field_num == 14:
                        result.created_at = _decode_zigzag(value)
                    elif field_num == 15:
                        result.updated_at = _decode_zigzag(value)
            elif wire_type == 2:  # Length-delimited
                length, pos = _DecodeVarint32(data, pos)
                value_bytes = data[pos:pos + length]
                pos += length
                if field_num == 9:
                    result.expiry = _decode_expiry_submessage(value_bytes)
                else:
                    value = value_bytes.decode("utf-8")
                    if field_num == 1:
                        result.chain_id = value
                    elif field_num == 2:
                        result.contract_address = value
                    elif field_num == 3:
                        result.market_id = value
                    elif field_num == 5:
                        result.taker_direction = value
                    elif field_num == 6:
                        result.margin = value
                    elif field_num == 7:
                        result.quantity = value
                    elif field_num == 8:
                        result.price = value
                    elif field_num == 10:
                        result.maker = value
                    elif field_num == 11:
                        result.taker = value
                    elif field_num == 12:
                        result.signature = value
                    elif field_num == 13:
                        result.status = value
                    elif field_num == 20:
                        result.min_fill_quantity = value

        return result


@dataclass
class RFQProcessedQuoteType:
    """Processed RFQ quote update streamed back to makers."""

    error: str = ""
    executed_quantity: str = ""
    executed_margin: str = ""
    market_id: str = ""
    rfq_id: int = 0
    taker_direction: str = ""
    margin: str = ""
    quantity: str = ""
    price: str = ""
    expiry: RFQExpiryType = field(default_factory=RFQExpiryType)
    maker: str = ""
    taker: str = ""
    signature: str = ""
    status: str = ""
    created_at: int = 0
    updated_at: int = 0
    height: int = 0
    event_time: int = 0
    transaction_time: int = 0
    chain_id: str = ""
    contract_address: str = ""

    @classmethod
    def decode(cls, data: bytes) -> "RFQProcessedQuoteType":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 0:
                value, pos = _DecodeVarint(data, pos)
                if field_num == 4:
                    result.rfq_id = value
                elif field_num == 14:
                    result.created_at = _decode_zigzag(value)
                elif field_num == 15:
                    result.updated_at = _decode_zigzag(value)
                elif field_num == 16:
                    result.height = value
                elif field_num == 17:
                    result.event_time = value
                elif field_num == 18:
                    result.transaction_time = value
            elif wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value_bytes = data[pos:pos + length]
                pos += length
                if field_num == 9:
                    result.expiry = _decode_expiry_type(value_bytes)
                    continue

                value = value_bytes.decode("utf-8")
                if field_num == 1:
                    result.chain_id = value
                elif field_num == 2:
                    result.contract_address = value
                elif field_num == 3:
                    result.market_id = value
                elif field_num == 5:
                    result.taker_direction = value
                elif field_num == 6:
                    result.margin = value
                elif field_num == 7:
                    result.quantity = value
                elif field_num == 8:
                    result.price = value
                elif field_num == 10:
                    result.maker = value
                elif field_num == 11:
                    result.taker = value
                elif field_num == 12:
                    result.signature = value
                elif field_num == 13:
                    result.status = value
                elif field_num == 50:
                    result.error = value
                elif field_num == 51:
                    result.executed_quantity = value
                elif field_num == 52:
                    result.executed_margin = value

        return result


@dataclass
class RFQSettlementLimitActionType:
    """Limit order action for unfilled settlement quantity."""

    price: str = ""

    @classmethod
    def decode(cls, data: bytes) -> "RFQSettlementLimitActionType":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value = data[pos:pos + length].decode("utf-8")
                pos += length
                if field_num == 1:
                    result.price = value

        return result


@dataclass
class RFQSettlementMarketActionType:
    """Market order action for unfilled settlement quantity."""

    @classmethod
    def decode(cls, _data: bytes) -> "RFQSettlementMarketActionType":
        return cls()


@dataclass
class RFQSettlementUnfilledActionType:
    """Action to take for unfilled settlement quantity."""

    limit: Optional[RFQSettlementLimitActionType] = None
    market: Optional[RFQSettlementMarketActionType] = None

    @classmethod
    def decode(cls, data: bytes) -> "RFQSettlementUnfilledActionType":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value_bytes = data[pos:pos + length]
                pos += length

                if field_num == 1:
                    result.limit = RFQSettlementLimitActionType.decode(value_bytes)
                elif field_num == 2:
                    result.market = RFQSettlementMarketActionType.decode(value_bytes)

        return result


@dataclass
class RFQSettlementType:
    """Settlement update streamed back to makers."""

    rfq_id: int = 0
    market_id: str = ""
    taker: str = ""
    direction: str = ""
    margin: str = ""
    quantity: str = ""
    worst_price: str = ""
    unfilled_action: Optional[RFQSettlementUnfilledActionType] = None
    fallback_quantity: str = ""
    fallback_margin: str = ""
    transaction_time: int = 0
    created_at: int = 0
    updated_at: int = 0
    event_time: int = 0
    height: int = 0
    cid: str = ""

    @classmethod
    def decode(cls, data: bytes) -> "RFQSettlementType":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 0:
                value, pos = _DecodeVarint(data, pos)
                if field_num == 1:
                    result.rfq_id = value
                elif field_num == 11:
                    result.transaction_time = value
                elif field_num == 12:
                    result.created_at = _decode_zigzag(value)
                elif field_num == 13:
                    result.updated_at = _decode_zigzag(value)
                elif field_num == 14:
                    result.event_time = value
                elif field_num == 15:
                    result.height = value
            elif wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value_bytes = data[pos:pos + length]
                pos += length

                if field_num == 8:
                    result.unfilled_action = RFQSettlementUnfilledActionType.decode(value_bytes)
                    continue

                value = value_bytes.decode("utf-8")
                if field_num == 2:
                    result.market_id = value
                elif field_num == 3:
                    result.taker = value
                elif field_num == 4:
                    result.direction = value
                elif field_num == 5:
                    result.margin = value
                elif field_num == 6:
                    result.quantity = value
                elif field_num == 7:
                    result.worst_price = value
                elif field_num == 9:
                    result.fallback_quantity = value
                elif field_num == 10:
                    result.fallback_margin = value
                elif field_num == 16:
                    result.cid = value

        return result



@dataclass
class RequestStreamAck:
    """Acknowledgment for taker request operations.
    
    Fields: 1=rfq_id, 2=client_id, 3=status.
    """
    rfq_id: int = 0
    client_id: str = ""
    status: str = ""

    @classmethod
    def decode(cls, data: bytes) -> "RequestStreamAck":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 0:
                value, pos = _DecodeVarint(data, pos)
                if field_num == 1:
                    result.rfq_id = value
            elif wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value = data[pos:pos + length].decode("utf-8")
                pos += length
                if field_num == 2:
                    result.client_id = value
                elif field_num == 3:
                    result.status = value

        return result


@dataclass
class QuoteStreamAck:
    """Acknowledgment for maker quote operations.
    
    Fields: 1=rfq_id, 2=status.
    """
    rfq_id: int = 0
    status: str = ""

    @classmethod
    def decode(cls, data: bytes) -> "QuoteStreamAck":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 0:
                value, pos = _DecodeVarint(data, pos)
                if field_num == 1:
                    result.rfq_id = value
            elif wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value = data[pos:pos + length].decode("utf-8")
                pos += length
                if field_num == 2:
                    result.status = value

        return result


@dataclass
class StreamError:
    """Error message in stream."""
    code: str = ""
    message: str = ""

    @classmethod
    def decode(cls, data: bytes) -> "StreamError":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value = data[pos:pos + length].decode("utf-8")
                pos += length
                if field_num == 1:
                    result.code = value
                elif field_num == 2:
                    result.message = value

        return result


# ============================================================
# Conditional Order Stream Types
# ============================================================

@dataclass
class ConditionalOrderInput:
    """Conditional order (TP/SL) payload sent inside TakerStreamRequest.

    Field numbers match the ConditionalOrderInput definition in the
    injective_rfq_rpc service:
    1=version, 2=chain_id, 3=contract_address, 4=taker, 5=epoch, 6=rfq_id,
    7=market_id, 8=subaccount_nonce, 9=lane_version, 10=deadline_ms,
    11=direction, 12=quantity, 13=margin, 14=worst_price,
    15=min_total_fill_quantity, 16=trigger_type, 17=trigger_price,
    18=unfilled_action, 19=cid, 20=allowed_relayer.

    All string numeric fields (quantity, margin, worst_price, etc.) use
    human-readable decimal strings — not scaled integers.

    margin must be "0" for v1 reduce-only (close-position) orders.
    deadline_ms is a Unix timestamp in milliseconds; max 30 days from now.
    """
    version: int = 0
    chain_id: str = ""
    contract_address: str = ""
    taker: str = ""
    epoch: int = 0
    rfq_id: int = 0
    market_id: str = ""
    subaccount_nonce: int = 0
    lane_version: int = 0
    deadline_ms: int = 0
    direction: str = ""
    quantity: str = ""
    margin: str = ""
    worst_price: str = ""
    min_total_fill_quantity: str = ""
    trigger_type: str = ""
    trigger_price: str = ""
    unfilled_action: str = ""
    cid: str = ""
    allowed_relayer: str = ""

    def encode(self) -> bytes:
        result = b""
        result += _encode_uint64(1, self.version)
        result += _encode_string(2, self.chain_id)
        result += _encode_string(3, self.contract_address)
        result += _encode_string(4, self.taker)
        result += _encode_uint64(5, self.epoch)
        result += _encode_uint64(6, self.rfq_id)
        result += _encode_string(7, self.market_id)
        result += _encode_uint64(8, self.subaccount_nonce)
        result += _encode_uint64(9, self.lane_version)
        result += _encode_uint64(10, self.deadline_ms)
        result += _encode_string(11, self.direction)
        result += _encode_string(12, self.quantity)
        result += _encode_string(13, self.margin)
        result += _encode_string(14, self.worst_price)
        result += _encode_string(15, self.min_total_fill_quantity)
        result += _encode_string(16, self.trigger_type)
        result += _encode_string(17, self.trigger_price)
        result += _encode_string(18, self.unfilled_action)
        result += _encode_string(19, self.cid)
        result += _encode_string(20, self.allowed_relayer)
        return result


@dataclass
class ConditionalOrderResponseType:
    """Conditional order data returned inside ConditionalOrderAck.

    Fields: 1=rfq_id, 2=market_id, 3=direction, 4=margin, 5=quantity,
    6=worst_price, 7=request_address, 8=trigger_price, 9=status,
    10=created_at, 11=updated_at, 12=expires_at, 13=trigger_type,
    14=min_total_fill_quantity.
    """
    rfq_id: int = 0
    market_id: str = ""
    direction: str = ""
    margin: str = ""
    quantity: str = ""
    worst_price: str = ""
    request_address: str = ""
    trigger_price: str = ""
    status: str = ""
    created_at: int = 0
    updated_at: int = 0
    expires_at: int = 0
    trigger_type: str = ""
    min_total_fill_quantity: str = ""

    @classmethod
    def decode(cls, data: bytes) -> "ConditionalOrderResponseType":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 0:
                value, pos = _DecodeVarint(data, pos)
                if field_num == 1:
                    result.rfq_id = value
                elif field_num == 10:
                    result.created_at = value
                elif field_num == 11:
                    result.updated_at = value
                elif field_num == 12:
                    result.expires_at = value
            elif wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value = data[pos:pos + length].decode("utf-8")
                pos += length
                if field_num == 2:
                    result.market_id = value
                elif field_num == 3:
                    result.direction = value
                elif field_num == 4:
                    result.margin = value
                elif field_num == 5:
                    result.quantity = value
                elif field_num == 6:
                    result.worst_price = value
                elif field_num == 7:
                    result.request_address = value
                elif field_num == 8:
                    result.trigger_price = value
                elif field_num == 9:
                    result.status = value
                elif field_num == 13:
                    result.trigger_type = value
                elif field_num == 14:
                    result.min_total_fill_quantity = value

        return result


@dataclass
class ConditionalOrderAck:
    """Acknowledgment returned by the server after a conditional order is accepted via stream.

    Field 1=order (ConditionalOrderResponseType).
    """
    order: Optional[ConditionalOrderResponseType] = None

    @classmethod
    def decode(cls, data: bytes) -> "ConditionalOrderAck":
        result = cls()
        pos = 0
        while pos < len(data):
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 2:
                length, pos = _DecodeVarint32(data, pos)
                value_bytes = data[pos:pos + length]
                pos += length
                if field_num == 1:
                    result.order = ConditionalOrderResponseType.decode(value_bytes)

        return result


# ============================================================
# Taker Stream Messages
# ============================================================

@dataclass
class TakerStreamRequest:
    """Message sent by taker in bidirectional stream.

    Fields: 1=message_type, 2=request, 3=conditional_order,
    4=conditional_order_signature, 5=conditional_order_sign_mode.

    Set message_type to "ping" for keep-alive, "request" when sending an
    RFQ request (populate field 2), or "conditional_order" when submitting
    a TP/SL order (populate fields 3 and 4 — and field 5, see below).

    `conditional_order_sign_mode` is required for conditional_order
    messages: the indexer rejects empty values with
    `value of message.conditional_order_sign_mode must be one of "v1", "v2"`.
    Use "v2" — it is the only mode this client signs.
    """
    message_type: str = ""  # "ping" | "request" | "conditional_order"
    request: Optional[CreateRFQRequestType] = None
    conditional_order: Optional[ConditionalOrderInput] = None
    conditional_order_signature: str = ""
    conditional_order_sign_mode: str = ""

    def encode(self) -> bytes:
        result = b""
        result += _encode_string(1, self.message_type)
        if self.request is not None:
            result += _encode_message(2, self.request.encode())
        if self.conditional_order is not None:
            result += _encode_message(3, self.conditional_order.encode())
        result += _encode_string(4, self.conditional_order_signature)
        result += _encode_string(5, self.conditional_order_sign_mode)
        return result


@dataclass
class TakerStreamResponse:
    """Message received by taker from server.

    Fields: 1=message_type, 2=quote, 3=request_ack, 4=error,
    5=conditional_order_ack.

    message_type values: "pong", "quote", "request_ack", "error",
    "conditional_order_ack".
    """
    message_type: str = ""
    quote: Optional[RFQQuoteType] = None
    request_ack: Optional[RequestStreamAck] = None
    error: Optional[StreamError] = None
    conditional_order_ack: Optional[ConditionalOrderAck] = None

    @classmethod
    def decode(cls, data: bytes) -> "TakerStreamResponse":
        result = cls()
        pos = 0
        while pos < len(data):
            if pos >= len(data):
                break
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 2:  # Length-delimited
                length, pos = _DecodeVarint32(data, pos)
                value_bytes = data[pos:pos + length]
                pos += length

                if field_num == 1:
                    result.message_type = value_bytes.decode("utf-8")
                elif field_num == 2:
                    result.quote = RFQQuoteType.decode(value_bytes)
                elif field_num == 3:
                    result.request_ack = RequestStreamAck.decode(value_bytes)
                elif field_num == 4:
                    result.error = StreamError.decode(value_bytes)
                elif field_num == 5:
                    result.conditional_order_ack = ConditionalOrderAck.decode(value_bytes)

        return result


# ============================================================
# Maker Stream Messages
# ============================================================

@dataclass
class MakerStreamRequest:
    """Message sent by maker in bidirectional stream.
    
    Fields: 1=message_type, 2=quote.
    """
    message_type: str = ""  # "ping" | "quote"
    quote: Optional[RFQQuoteType] = None

    def encode(self) -> bytes:
        result = b""
        result += _encode_string(1, self.message_type)
        if self.quote:
            result += _encode_message(2, self.quote.encode())
        return result


@dataclass
class MakerStreamResponse:
    """Message received by maker from server.
    
    Fields: 1=message_type, 2=request, 3=quote_ack, 4=error,
    5=processed_quote, 6=settlement.
    """
    message_type: str = ""  # "pong" | "request" | "quote_ack" | "error"
    request: Optional[RFQRequestType] = None
    quote_ack: Optional[QuoteStreamAck] = None
    error: Optional[StreamError] = None
    processed_quote: Optional[RFQProcessedQuoteType] = None
    settlement: Optional[RFQSettlementType] = None

    @classmethod
    def decode(cls, data: bytes) -> "MakerStreamResponse":
        result = cls()
        pos = 0
        while pos < len(data):
            if pos >= len(data):
                break
            tag_wire, new_pos = _DecodeVarint32(data, pos)
            field_num = tag_wire >> 3
            wire_type = tag_wire & 0x7
            pos = new_pos

            if wire_type == 2:  # Length-delimited
                length, pos = _DecodeVarint32(data, pos)
                value_bytes = data[pos:pos + length]
                pos += length

                if field_num == 1:
                    result.message_type = value_bytes.decode("utf-8")
                elif field_num == 2:
                    result.request = RFQRequestType.decode(value_bytes)
                elif field_num == 3:
                    result.quote_ack = QuoteStreamAck.decode(value_bytes)
                elif field_num == 4:
                    result.error = StreamError.decode(value_bytes)
                elif field_num == 5:
                    result.processed_quote = RFQProcessedQuoteType.decode(value_bytes)
                elif field_num == 6:
                    result.settlement = RFQSettlementType.decode(value_bytes)

        return result
