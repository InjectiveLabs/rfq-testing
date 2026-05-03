"""WebSocket client for RFQ Indexer (gRPC-web protocol).

Supports bidirectional streaming via TakerStream and MakerStream endpoints.
Uses gRPC-web message framing over WebSocket with protobuf serialization.
"""

import asyncio
import logging
import ssl
import struct
import time
import uuid
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

import certifi
import websockets

from rfq_test.exceptions import (
    IndexerConnectionError,
    IndexerTimeoutError,
    IndexerValidationError,
)
from rfq_test.proto.injective_rfq_rpc_pb2 import (
    CreateRFQRequestType,
    MakerStreamStreamingRequest,
    MakerStreamResponse,
    RFQExpiryType,
    RFQProcessedQuoteType,
    RFQQuoteType,
    RFQSettlementMakerUpdate,
    TakerStreamStreamingRequest,
    TakerStreamResponse,
)
# ConditionalOrderInput and TakerStreamRequest (with conditional_order field) are
# only in the hand-written proto module — the generated proto does not include them yet.
from rfq_test.proto.rfq_messages import (
    ConditionalOrderInput,
    TakerStreamRequest as TakerStreamConditionalRequest,
)

logger = logging.getLogger(__name__)

GRPC_WS_SUBPROTOCOL = "grpc-ws"
PING_INTERVAL_SECONDS = 1.0


def _format_connection_closed(exc: Exception) -> str:
    """Format ConnectionClosed for logging."""
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        code = getattr(rcvd, "code", None)
        reason = getattr(rcvd, "reason", None) or ""
    else:
        code = getattr(exc, "code", None)
        reason = getattr(exc, "reason", None) or ""
    if code is not None:
        return f"code={code} reason={reason!r}" if reason else f"code={code}"
    return str(exc)


def encode_grpc_message(message) -> bytes:
    """Encode a protobuf message with gRPC-web framing.
    
    Format: [1 byte compression flag][4 bytes length BE][protobuf payload]
    """
    payload = message.SerializeToString()
    header = struct.pack(">BI", 0, len(payload))
    return header + payload


def decode_grpc_message(data: bytes, message_type):
    """Decode a gRPC-web framed message.
    
    Returns None if this is a trailer frame (compression flag 0x80).
    """
    if len(data) < 5:
        return None
    
    compression_flag = data[0]
    if compression_flag == 0x80:
        return None
    if compression_flag != 0:
        logger.warning(f"Unsupported compression flag: {compression_flag}")
        return None
    
    length = struct.unpack(">I", data[1:5])[0]
    payload = data[5:5 + length]
    
    return message_type.FromString(payload)


class BaseStreamClient(ABC):
    """Base class for RFQ stream clients."""
    
    MAX_RECONNECT_ATTEMPTS = 3
    RECONNECT_DELAY_SECONDS = 1.0
    
    def __init__(self, base_url: str, timeout: float = 10.0):
        """Initialize stream client.
        
        Args:
            base_url: WebSocket base URL (without /TakerStream or /MakerStream)
            timeout: Default timeout for operations
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._ws = None
        self._connected = False
        self._ping_task: Optional[asyncio.Task] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._reconnect_count = 0
        self._auto_reconnect = True
    
    @property
    @abstractmethod
    def stream_path(self) -> str:
        """Stream endpoint path (e.g., '/TakerStream')."""
        pass
    
    @property
    def url(self) -> str:
        """Full WebSocket URL."""
        return f"{self.base_url}{self.stream_path}"

    def _additional_headers(self) -> Optional[dict[str, str]]:
        """Optional WebSocket handshake headers for stream metadata."""
        return None
    
    async def connect(self) -> None:
        """Connect to the WebSocket stream."""
        try:
            logger.info(f"Connecting to {self.url}")
            
            ssl_context = None
            if self.url.startswith("wss://"):
                ssl_context = ssl.create_default_context(cafile=certifi.where())
            
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.url,
                    subprotocols=[GRPC_WS_SUBPROTOCOL],
                    ssl=ssl_context,
                    additional_headers=self._additional_headers(),
                ),
                timeout=self.timeout,
            )
            self._connected = True
            
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._ping_task = asyncio.create_task(self._ping_loop())
            
            try:
                await self._send_ping()
            except Exception as e:
                logger.warning(f"Initial ping failed: {e}")
            
            logger.info(f"Connected to {self.url}")
            
        except asyncio.TimeoutError as e:
            raise IndexerConnectionError(f"Connection timeout: {self.url}") from e
        except Exception as e:
            raise IndexerConnectionError(f"Failed to connect: {e}") from e
    
    @property
    def is_connected(self) -> bool:
        """True if WebSocket is open and background loops are running."""
        if not self._connected or self._ws is None:
            return False
        return self._ws.close_code is None

    async def ensure_connected(self) -> bool:
        """Check connection and reconnect if needed. Returns True if connected."""
        if self.is_connected:
            return True
        return await self._reconnect()

    async def _reconnect(self) -> bool:
        """Attempt to reconnect with retries. Returns True on success."""
        for attempt in range(1, self.MAX_RECONNECT_ATTEMPTS + 1):
            try:
                logger.warning(
                    "Reconnecting %s (attempt %d/%d) ...",
                    self.url, attempt, self.MAX_RECONNECT_ATTEMPTS,
                )
                await self._cleanup_connection()
                await self.connect()
                self._reconnect_count += 1
                logger.info("Reconnected to %s (total reconnects: %d)", self.url, self._reconnect_count)
                return True
            except Exception as e:
                logger.warning("Reconnect attempt %d failed: %s", attempt, e)
                if attempt < self.MAX_RECONNECT_ATTEMPTS:
                    await asyncio.sleep(self.RECONNECT_DELAY_SECONDS * attempt)
        logger.error("Failed to reconnect after %d attempts", self.MAX_RECONNECT_ATTEMPTS)
        return False

    async def _cleanup_connection(self) -> None:
        """Stop background tasks and close socket without raising."""
        self._connected = False
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ping_task = None
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._receive_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._auto_reconnect = False
        await self._cleanup_connection()
        logger.info("WebSocket connection closed")
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def _ping_loop(self) -> None:
        """Send periodic pings to keep connection alive."""
        while self._connected:
            try:
                await self._send_ping()
                await asyncio.sleep(PING_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed as e:
                if self._connected:
                    logger.warning("WebSocket connection closed: %s", _format_connection_closed(e))
                    self._connected = False
                break
            except Exception as e:
                if self._connected:
                    logger.error(f"Ping error: {e}")
                    self._connected = False
                break
    
    @abstractmethod
    async def _send_ping(self) -> None:
        """Send a ping message."""
        pass
    
    @abstractmethod
    async def _receive_loop(self) -> None:
        """Receive and process incoming messages."""
        pass
    
    async def _send_raw(self, data: bytes) -> None:
        """Send raw bytes over WebSocket."""
        if not self._ws:
            raise IndexerConnectionError("Not connected")
        await self._ws.send(data)


class TakerStreamClient(BaseStreamClient):
    """WebSocket client for takers (retail users).

    Takers send RFQ requests and receive quotes. The indexer requires
    request_address (taker's Injective address) as gRPC metadata when
    opening the stream.
    """

    def __init__(
        self,
        base_url: str,
        request_address: Optional[str] = None,
        timeout: float = 10.0,
    ):
        """Initialize Taker stream client.

        Args:
            base_url: WebSocket base URL (without /TakerStream)
            request_address: Taker's Injective address (required by indexer as stream metadata)
            timeout: Default timeout for operations
        """
        super().__init__(base_url, timeout=timeout)
        self._request_address = request_address

    @property
    def stream_path(self) -> str:
        return "/TakerStream"

    def _additional_headers(self) -> Optional[dict[str, str]]:
        """gRPC-web metadata required to identify the taker stream."""
        if not self._request_address:
            return None
        return {"request_address": self._request_address}

    async def _send_ping(self) -> None:
        """Send ping to keep connection alive."""
        msg = TakerStreamStreamingRequest(message_type="ping")
        await self._send_raw(encode_grpc_message(msg))
    
    async def _receive_loop(self) -> None:
        """Receive and queue incoming messages."""
        while self._connected and self._ws:
            try:
                data = await self._ws.recv()
                
                if isinstance(data, str):
                    logger.debug(f"Received header: {data}")
                    continue
                
                response = decode_grpc_message(data, TakerStreamResponse)
                if response is None:
                    continue
                
                msg_type = response.message_type
                
                if msg_type == "pong":
                    pass
                
                elif msg_type == "quote":
                    quote = response.quote
                    logger.info(f"Received quote: RFQ#{quote.rfq_id} price={quote.price} from {quote.maker}")
                    await self._message_queue.put(("quote", quote))
                
                elif msg_type == "request_ack":
                    ack = response.request_ack
                    logger.debug(f"Request ACK: RFQ#{ack.rfq_id} status={ack.status}")
                    await self._message_queue.put(("request_ack", ack))
                
                elif msg_type == "conditional_order_ack":
                    ack = response.conditional_order_ack
                    rfq_id = ack.order.rfq_id if ack and ack.order else 0
                    logger.info(f"Conditional order ACK: RFQ#{rfq_id}")
                    await self._message_queue.put(("conditional_order_ack", ack))

                elif msg_type == "error":
                    err = response.error
                    logger.error(f"Stream error: code={err.code} message={err.message_}")
                    await self._message_queue.put(("error", err))

                else:
                    logger.warning(f"Unknown message type: {msg_type}")

            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket connection closed: %s", _format_connection_closed(e))
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._connected:
                    logger.error(f"Receive error: {e}")
                break

    async def send_conditional_order(
        self,
        order_body: dict,
        signature: str,
        wait_for_ack: bool = True,
        response_timeout: float = 5.0,
        sign_mode: str = "v2",
    ) -> Optional[dict]:
        """Send a conditional order (TP/SL) via the TakerStream.

        Args:
            order_body: Dict with the conditional order fields — version, chain_id,
                contract_address, taker, epoch, rfq_id, market_id, subaccount_nonce,
                lane_version, deadline_ms, direction, quantity, margin, worst_price,
                min_total_fill_quantity, trigger_type, trigger_price, unfilled_action,
                cid, allowed_relayer.
            signature: Hex-encoded secp256k1 signature from
                `sign_conditional_order_v2()` (with or without 0x prefix — 0x is
                added automatically).
            wait_for_ack: If True, block until conditional_order_ack or error arrives.
            response_timeout: Seconds to wait for ack (default 5.0).
            sign_mode: Signature scheme — must be "v2" for any signature this
                client produces. The indexer rejects empty values
                (`value of message.conditional_order_sign_mode must be one of
                "v1", "v2"`). Default "v2".

        Returns:
            Dict with rfq_id and status if wait_for_ack=True, else None.

        Raises:
            IndexerValidationError: If the server returns a stream error.
            IndexerTimeoutError: If no ack is received within response_timeout.
        """
        def _str(v) -> str:
            return "" if v is None else str(v)

        co = ConditionalOrderInput(
            version=int(order_body.get("version", 1)),
            chain_id=_str(order_body.get("chain_id")),
            contract_address=_str(order_body.get("contract_address")),
            taker=_str(order_body.get("taker")),
            epoch=int(order_body.get("epoch", 1)),
            rfq_id=int(order_body.get("rfq_id", 0)),
            market_id=_str(order_body.get("market_id")),
            subaccount_nonce=int(order_body.get("subaccount_nonce", 0)),
            lane_version=int(order_body.get("lane_version", 1)),
            deadline_ms=int(order_body.get("deadline_ms", 0)),
            direction=_str(order_body.get("direction")),
            quantity=_str(order_body.get("quantity")),
            margin=_str(order_body.get("margin") or "0"),
            worst_price=_str(order_body.get("worst_price")),
            min_total_fill_quantity=_str(order_body.get("min_total_fill_quantity")),
            trigger_type=_str(order_body.get("trigger_type")),
            trigger_price=_str(order_body.get("trigger_price")),
            unfilled_action=_str(order_body.get("unfilled_action")),
            cid=_str(order_body.get("cid")),
            allowed_relayer=_str(order_body.get("allowed_relayer")),
        )

        sig = signature if signature.startswith("0x") else ("0x" + signature)
        # TakerStreamConditionalRequest is the hand-written proto that supports
        # the conditional_order field (not yet in the generated injective_rfq_rpc_pb2).
        msg = TakerStreamConditionalRequest(
            message_type="conditional_order",
            conditional_order=co,
            conditional_order_signature=sig,
            conditional_order_sign_mode=sign_mode,
        )
        logger.info(
            "Sending conditional_order: market=%s dir=%s trigger_type=%s trigger_price=%s",
            co.market_id[:20], co.direction, co.trigger_type, co.trigger_price,
        )
        await self._send_raw(encode_grpc_message(msg))

        if not wait_for_ack:
            return None
        return await self._wait_for_conditional_order_ack(response_timeout)

    async def _wait_for_conditional_order_ack(self, timeout: float) -> dict:
        """Wait for conditional_order_ack or error after sending a conditional order."""
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                remaining = timeout - (time.monotonic() - start)
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=remaining,
                )

                if msg_type == "conditional_order_ack":
                    order = data.order
                    rfq_id = order.rfq_id if order else 0
                    status = order.status if order else ""
                    logger.info(f"Conditional order ACK: RFQ#{rfq_id} status={status}")
                    return {"rfq_id": rfq_id, "status": status, "order": order}

                if msg_type == "error":
                    error_msg = f"{data.code}: {data.message_}"
                    logger.warning(f"Conditional order error: {error_msg}")
                    raise IndexerValidationError(error_msg)

                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)

            except asyncio.TimeoutError:
                break

        raise IndexerTimeoutError(f"No conditional_order_ack within {timeout}s")


    async def send_request(self, request_data: dict, wait_for_response: bool = False, response_timeout: float = 2.0) -> Optional[dict]:
        """Send an RFQ request.
        
        Args:
            request_data: Request parameters (market_id, direction, etc.)
            wait_for_response: If True, wait for ACK or error response
            response_timeout: Timeout for waiting for response
            
        Returns:
            Response dict if wait_for_response=True, None otherwise
            
        Raises:
            IndexerValidationError: If server returns an error
        """
        direction = request_data.get("direction", "")
        if isinstance(direction, int):
            direction = str(direction)
        
        client_id = request_data.get("client_id") or str(uuid.uuid4())
        expiry_raw = request_data.get("expiry")
        if expiry_raw is None:
            expiry_val = int(time.time() * 1000) + 300_000
        elif isinstance(expiry_raw, dict):
            expiry_val = int(expiry_raw.get("ts") or expiry_raw.get("timestamp") or 0)
        else:
            expiry_val = int(expiry_raw)
        request = CreateRFQRequestType(
            client_id=client_id,
            market_id=request_data.get("market_id", ""),
            direction=direction,
            margin=str(request_data.get("margin", "")),
            quantity=str(request_data.get("quantity", "")),
            worst_price=str(request_data.get("worst_price", "0")),
            expiry=expiry_val,
        )
        msg = TakerStreamStreamingRequest(message_type="request", request=request)
        logger.info(f"Sending request: client_id={client_id} {request.direction} qty={request.quantity}")
        await self._send_raw(encode_grpc_message(msg))
        if wait_for_response:
            return await self._wait_for_response(client_id, response_timeout)
        return None
    
    async def _wait_for_response(self, client_id: str, timeout: float) -> dict:
        """Wait for ACK or error response after sending a request.
        
        Args:
            client_id: Client ID used to identify the request
            timeout: Maximum wait time
            
        Returns:
            Response dict with type, rfq_id, client_id, and status
            
        Raises:
            IndexerValidationError: If server returns an error
        """
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                remaining = timeout - (time.monotonic() - start)
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=remaining,
                )
                
                if msg_type == "request_ack":
                    response = {
                        "type": "ack",
                        "rfq_id": data.rfq_id,
                        "client_id": data.client_id,
                        "status": data.status,
                    }
                    logger.info(f"Request ACK received: RFQ#{data.rfq_id} status={data.status}")
                    return response
                
                if msg_type == "error":
                    error_msg = f"{data.code}: {data.message_}"
                    logger.warning(f"Request error received: {error_msg}")
                    raise IndexerValidationError(error_msg)
                
                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)
                
            except asyncio.TimeoutError:
                break
        
        logger.warning(f"No response received for request {client_id} within {timeout}s (no ACK, no error)")
        return {"type": "no_response", "rfq_id": 0}
    
    async def wait_for_ack(self, rfq_id: int, timeout: float = 5.0) -> dict:
        """Wait for request acknowledgment.
        
        Args:
            rfq_id: Request ID to wait for
            timeout: Maximum wait time
            
        Returns:
            Acknowledgment data
        """
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=timeout - (time.monotonic() - start),
                )
                
                if msg_type == "request_ack" and data.rfq_id == rfq_id:
                    return {"rfq_id": data.rfq_id, "status": data.status}
                
                if msg_type == "error":
                    raise IndexerValidationError(f"{data.code}: {data.message_}")

                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)

            except asyncio.TimeoutError:
                break

        raise IndexerTimeoutError(f"No ACK for request {rfq_id} within {timeout}s")
    
    async def wait_for_quote(self, rfq_id: int, timeout: float = 10.0) -> dict:
        """Wait for a quote for a specific request.
        
        Args:
            rfq_id: Request ID to wait for quotes
            timeout: Maximum wait time
            
        Returns:
            Quote data as dict
        """
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=timeout - (time.monotonic() - start),
                )
                
                if msg_type == "quote" and data.rfq_id == rfq_id:
                    return self._quote_to_dict(data)
                
                if msg_type == "error":
                    raise IndexerValidationError(f"{data.code}: {data.message}")
                
                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)
                
            except asyncio.TimeoutError:
                break
        
        raise IndexerTimeoutError(f"No quote for request {rfq_id} within {timeout}s")
    
    async def get_next_event(self, timeout: float = 1.0) -> Optional[tuple]:
        """Get the next stream event (request_ack, quote, error). Returns None on timeout."""
        try:
            return await asyncio.wait_for(self._message_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
    
    async def collect_quotes(self, rfq_id: int, timeout: float = 5.0, min_quotes: int = 1) -> list[dict]:
        """Collect quotes for a request.
        
        Args:
            rfq_id: Request ID to collect quotes for
            timeout: Maximum wait time
            min_quotes: Minimum number of quotes to collect
            
        Returns:
            List of quote dicts
        """
        quotes = []
        start = time.monotonic()
        
        while (time.monotonic() - start) < timeout:
            try:
                remaining = timeout - (time.monotonic() - start)
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=min(remaining, 1.0),
                )
                
                if msg_type == "quote" and data.rfq_id == rfq_id:
                    quotes.append(self._quote_to_dict(data))
                    if len(quotes) >= min_quotes:
                        await asyncio.sleep(0.5)
                        while not self._message_queue.empty():
                            try:
                                msg_type, data = self._message_queue.get_nowait()
                                if msg_type == "quote" and data.rfq_id == rfq_id:
                                    quotes.append(self._quote_to_dict(data))
                            except asyncio.QueueEmpty:
                                break
                        break
                else:
                    await self._message_queue.put((msg_type, data))
                    # Yield to event loop so ping task can run
                    # Without this, re-reading the same non-matching message creates
                    # a hot loop that starves the ping task → server disconnects
                    await asyncio.sleep(0)
                    
            except asyncio.TimeoutError:
                if quotes:
                    break
                continue
        
        return quotes
    
    def _quote_to_dict(self, quote: RFQQuoteType) -> dict:
        """Convert quote protobuf to dict."""
        expiry = quote.expiry
        expiry_val = expiry.timestamp if expiry else 0
        return {
            "rfq_id": str(quote.rfq_id),
            "market_id": quote.market_id,
            "taker_direction": quote.taker_direction,
            "margin": quote.margin,
            "quantity": quote.quantity,
            "price": quote.price,
            "expiry": expiry_val,
            "maker": quote.maker,
            "taker": quote.taker,
            "signature": quote.signature,
            "status": quote.status,
        }


class MakerStreamClient(BaseStreamClient):
    """WebSocket client for makers (market makers).
    
    Makers receive RFQ requests and send quotes.
    
    Usage:
        async with MakerStreamClient(ws_url) as client:
            async for request in client.requests(timeout=60):
                await client.send_quote(quote_data)
    """

    def __init__(
        self,
        base_url: str,
        maker_address: Optional[str] = None,
        subscribe_to_quotes_updates: bool = False,
        subscribe_to_settlement_updates: bool = False,
        timeout: float = 10.0,
    ):
        """Initialize Maker stream client.

        Args:
            base_url: WebSocket base URL (without /MakerStream)
            maker_address: Maker's Injective address sent as stream metadata
            subscribe_to_quotes_updates: Request quote update events for this maker
            subscribe_to_settlement_updates: Request settlement update events for this maker
            timeout: Default timeout for operations
        """
        super().__init__(base_url, timeout=timeout)
        self._maker_address = maker_address
        self._subscribe_to_quotes_updates = subscribe_to_quotes_updates
        self._subscribe_to_settlement_updates = subscribe_to_settlement_updates
    
    @property
    def stream_path(self) -> str:
        return "/MakerStream"

    def _additional_headers(self) -> Optional[dict[str, str]]:
        """gRPC-web metadata supported by the maker stream handshake."""
        headers: dict[str, str] = {}
        if self._maker_address:
            headers["maker_address"] = self._maker_address
        if self._subscribe_to_quotes_updates:
            headers["subscribe_to_quotes_updates"] = "true"
        if self._subscribe_to_settlement_updates:
            headers["subscribe_to_settlement_updates"] = "true"
        return headers or None
    
    async def _send_ping(self) -> None:
        """Send ping to keep connection alive."""
        msg = MakerStreamStreamingRequest(message_type="ping")
        await self._send_raw(encode_grpc_message(msg))
    
    async def _receive_loop(self) -> None:
        """Receive and queue incoming messages."""
        while self._connected and self._ws:
            try:
                data = await self._ws.recv()
                
                if isinstance(data, str):
                    logger.debug(f"Received header: {data}")
                    continue
                
                response = decode_grpc_message(data, MakerStreamResponse)
                if response is None:
                    continue
                
                msg_type = response.message_type
                
                if msg_type == "pong":
                    pass
                
                elif msg_type == "request":
                    request = response.request
                    logger.info(f"Received request: RFQ#{request.rfq_id} {request.direction} qty={request.quantity}")
                    await self._message_queue.put(("request", request))
                
                elif msg_type == "quote_ack":
                    ack = response.quote_ack
                    logger.debug(f"Quote ACK: RFQ#{ack.rfq_id} status={ack.status}")
                    await self._message_queue.put(("quote_ack", ack))

                elif msg_type == "quote_update":
                    quote = response.processed_quote
                    logger.info(
                        "Received quote update: RFQ#%s status=%s maker=%s",
                        quote.rfq_id,
                        quote.status,
                        quote.maker,
                    )
                    await self._message_queue.put(("quote_update", quote))

                elif msg_type == "settlement_update":
                    settlement = response.settlement
                    logger.info(
                        "Received settlement update: RFQ#%s taker=%s cid=%s",
                        settlement.rfq_id,
                        settlement.taker,
                        settlement.cid,
                    )
                    await self._message_queue.put(("settlement_update", settlement))

                elif msg_type == "error":
                    err = response.error
                    logger.error(f"Stream error: code={err.code} message={err.message_}")
                    await self._message_queue.put(("error", err))
                
                else:
                    logger.warning(f"Unknown message type: {msg_type}")
                    
            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket connection closed: %s", _format_connection_closed(e))
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._connected:
                    logger.error(f"Receive error: {e}")
                break
    
    async def send_quote(self, quote_data: dict, wait_for_response: bool = False, response_timeout: float = 2.0) -> Optional[dict]:
        """Send a quote.
        
        Args:
            quote_data: Quote parameters including signature
            wait_for_response: If True, wait for ACK or error response
            response_timeout: Timeout for waiting for response
            
        Returns:
            Response dict if wait_for_response=True, None otherwise
            
        Raises:
            IndexerValidationError: If server returns an error
        """
        taker_direction = quote_data.get("taker_direction", quote_data.get("direction", ""))
        if taker_direction in (0, "0"):
            taker_direction = "long"
        elif taker_direction in (1, "1"):
            taker_direction = "short"
        elif isinstance(taker_direction, str) and taker_direction.lower() in ("long", "short"):
            taker_direction = taker_direction.lower()
        else:
            taker_direction = str(taker_direction).lower() if isinstance(taker_direction, str) else "long"
        
        signature = quote_data.get("signature", "")
        if signature and not signature.startswith("0x"):
            signature = "0x" + signature

        expiry_ts = int(quote_data.get("expiry", 0))
        min_fill_quantity = quote_data.get("min_fill_quantity")
        # sign_mode is required by the indexer (post 2026-04-29 devnet deploy);
        # an empty string fails validation with `value of message.sign_mode must
        # be one of "v1", "v2"`. We default to v2 — this client only signs v2.
        sign_mode = quote_data.get("sign_mode") or "v2"
        quote_kwargs = {
            "chain_id": quote_data.get("chain_id", ""),
            "contract_address": quote_data.get("contract_address", ""),
            "market_id": quote_data.get("market_id", ""),
            "rfq_id": int(quote_data.get("rfq_id", 0)),
            "taker_direction": taker_direction,
            "margin": str(quote_data.get("margin", "")),
            "quantity": str(quote_data.get("quantity", "")),
            "price": str(quote_data.get("price", "")),
            "expiry": RFQExpiryType(timestamp=expiry_ts),
            "maker": quote_data.get("maker", ""),
            "taker": quote_data.get("taker", ""),
            "signature": signature,
            "status": "pending",
            "transaction_time": int(time.time() * 1000),
            "maker_subaccount_nonce": int(quote_data.get("maker_subaccount_nonce", 0)),
            "sign_mode": sign_mode,
        }
        if quote_data.get("evm_chain_id") is not None:
            quote_kwargs["evm_chain_id"] = int(quote_data["evm_chain_id"])
        if min_fill_quantity is not None:
            quote_kwargs["min_fill_quantity"] = str(min_fill_quantity)

        quote = RFQQuoteType(**quote_kwargs)

        msg = MakerStreamStreamingRequest(message_type="quote", quote=quote)
        
        logger.info(f"Sending quote: RFQ#{quote.rfq_id} price={quote.price}")
        await self._send_raw(encode_grpc_message(msg))
        
        if wait_for_response:
            return await self._wait_for_quote_response(quote.rfq_id, response_timeout)
        return None
    
    async def _wait_for_quote_response(self, rfq_id: int, timeout: float) -> dict:
        """Wait for ACK or error response after sending a quote."""
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                remaining = timeout - (time.monotonic() - start)
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=remaining,
                )
                
                if msg_type == "quote_ack":
                    response = {
                        "type": "ack",
                        "rfq_id": data.rfq_id,
                        "status": data.status,
                    }
                    logger.info(f"Quote ACK received: RFQ#{data.rfq_id} status={data.status}")
                    return response
                
                if msg_type == "error":
                    error_msg = f"{data.code}: {data.message_}"
                    logger.warning(f"Quote error received: {error_msg}")
                    raise IndexerValidationError(error_msg)
                
                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)
                
            except asyncio.TimeoutError:
                break
        
        logger.warning(f"No response received for quote on RFQ#{rfq_id} within {timeout}s (no ACK, no error)")
        return {"type": "no_response", "rfq_id": rfq_id}
    
    async def wait_for_request(
        self,
        timeout: float = 30.0,
        client_id: str = None,
        market_id: str = None,
        direction: str = None,
    ) -> dict:
        """Wait for an RFQ request, optionally filtering by client_id, market_id, and/or direction.

        Args:
            timeout: Maximum wait time in seconds
            client_id: If set, skip requests whose client_id doesn't match.
            market_id: If set, skip requests for a different market.
            direction: If set, skip requests with a different direction
                (e.g. "short" or "long"). Compared case-insensitively.

        Returns:
            Request data as dict
        """
        start = time.monotonic()
        skipped = 0
        while (time.monotonic() - start) < timeout:
            try:
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=timeout - (time.monotonic() - start),
                )

                if msg_type == "request":
                    req = self._request_to_dict(data)

                    if market_id and req.get("market_id") != market_id:
                        skipped += 1
                        logger.debug(
                            "Skipping request RFQ#%s: market_id mismatch (skipped %d)",
                            req.get("rfq_id", "?"), skipped,
                        )
                        continue

                    if client_id and req.get("client_id") != client_id:
                        skipped += 1
                        logger.debug(
                            "Skipping request RFQ#%s: client_id mismatch (skipped %d)",
                            req.get("rfq_id", "?"), skipped,
                        )
                        continue

                    if direction:
                        req_dir = str(req.get("direction", "")).lower()
                        if req_dir != direction.lower():
                            skipped += 1
                            logger.debug(
                                "Skipping request RFQ#%s: direction=%s, want %s (skipped %d)",
                                req.get("rfq_id", "?"), req_dir, direction.lower(), skipped,
                            )
                            continue

                    return req

                if msg_type == "error":
                    raise IndexerValidationError(f"{data.code}: {data.message_}")

                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)

            except asyncio.TimeoutError:
                break

        raise IndexerTimeoutError(f"No request within {timeout}s")
    
    async def requests(self, timeout: float = 60.0) -> AsyncIterator[dict]:
        """Async iterator for incoming requests.
        
        Args:
            timeout: Total timeout for iteration
            
        Yields:
            Request data dicts
        """
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=min(timeout - (time.monotonic() - start), 1.0),
                )
                
                if msg_type == "request":
                    yield self._request_to_dict(data)
                elif msg_type == "error":
                    logger.error(f"Stream error: {data.code}: {data.message_}")
                else:
                    await self._message_queue.put((msg_type, data))
                    
            except asyncio.TimeoutError:
                continue

    async def get_next_event(self, timeout: float = 1.0) -> Optional[tuple]:
        """Get the next maker stream event. Returns None on timeout."""
        try:
            return await asyncio.wait_for(self._message_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def wait_for_quote_update(self, rfq_id: int, timeout: float = 30.0) -> dict:
        """Wait for a processed quote update for a specific RFQ."""
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=timeout - (time.monotonic() - start),
                )

                if msg_type == "quote_update" and data.rfq_id == rfq_id:
                    return self._processed_quote_to_dict(data)

                if msg_type == "error":
                    raise IndexerValidationError(f"{data.code}: {data.message}")

                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)
            except asyncio.TimeoutError:
                break

        raise IndexerTimeoutError(f"No quote update for request {rfq_id} within {timeout}s")

    async def wait_for_settlement_update(self, rfq_id: int, timeout: float = 30.0) -> dict:
        """Wait for a settlement update for a specific RFQ."""
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=timeout - (time.monotonic() - start),
                )

                if msg_type == "settlement_update" and data.rfq_id == rfq_id:
                    return self._settlement_to_dict(data)

                if msg_type == "error":
                    raise IndexerValidationError(f"{data.code}: {data.message}")

                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)
            except asyncio.TimeoutError:
                break

        raise IndexerTimeoutError(f"No settlement update for request {rfq_id} within {timeout}s")
    
    def _request_to_dict(self, request) -> dict:
        """Convert request protobuf to dict."""
        return {
            "client_id": request.client_id,
            "rfq_id": str(request.rfq_id),
            "market_id": request.market_id,
            "direction": request.direction,
            "margin": request.margin,
            "quantity": request.quantity,
            "worst_price": request.worst_price,
            "request_address": request.request_address,
            "taker": request.request_address,
            "expiry": request.expiry,
            "status": request.status,
        }

    def _processed_quote_to_dict(self, quote: RFQProcessedQuoteType) -> dict:
        """Convert processed quote protobuf to dict."""
        return {
            "rfq_id": str(quote.rfq_id),
            "chain_id": quote.chain_id,
            "contract_address": quote.contract_address,
            "market_id": quote.market_id,
            "taker_direction": quote.taker_direction,
            "margin": quote.margin,
            "quantity": quote.quantity,
            "price": quote.price,
            "expiry": self._expiry_to_dict(quote.expiry),
            "maker": quote.maker,
            "taker": quote.taker,
            "signature": quote.signature,
            "status": quote.status,
            "error": quote.error,
            "executed_quantity": quote.executed_quantity,
            "executed_margin": quote.executed_margin,
            "created_at": quote.created_at,
            "updated_at": quote.updated_at,
            "height": quote.height,
            "event_time": quote.event_time,
            "transaction_time": quote.transaction_time,
        }

    def _settlement_to_dict(self, settlement: RFQSettlementMakerUpdate) -> dict:
        """Convert settlement protobuf to dict."""
        return {
            "rfq_id": str(settlement.rfq_id),
            "market_id": settlement.market_id,
            "taker": settlement.taker,
            "direction": settlement.direction,
            "margin": settlement.margin,
            "quantity": settlement.quantity,
            "worst_price": settlement.worst_price,
            "unfilled_action": self._unfilled_action_to_dict(settlement.unfilled_action),
            "fallback_quantity": settlement.fallback_quantity,
            "fallback_margin": settlement.fallback_margin,
            "cid": settlement.cid,
            "created_at": settlement.created_at,
            "updated_at": settlement.updated_at,
            "event_time": settlement.event_time,
            "transaction_time": settlement.transaction_time,
            "height": settlement.height,
            "quotes": [
                {
                    "maker": q.maker,
                    "price": q.price,
                    "quoted_margin": q.quoted_margin,
                    "quoted_quantity": q.quoted_quantity,
                    "executed_margin": q.executed_margin,
                    "executed_quantity": q.executed_quantity,
                    "status": q.status,
                }
                for q in getattr(settlement, "quotes", [])
            ],
        }

    def _expiry_to_dict(self, expiry) -> dict:
        """Convert RFQExpiryType protobuf to dict."""
        result = {}
        timestamp = getattr(expiry, "timestamp", 0)
        height = getattr(expiry, "height", 0)
        if timestamp:
            result["ts"] = timestamp
        if height:
            result["h"] = height
        return result

    def _unfilled_action_to_dict(self, unfilled_action) -> Optional[dict]:
        """Convert settlement unfilled action protobuf to dict."""
        if not unfilled_action:
            return None
        if getattr(unfilled_action, "limit", None):
            return {"limit": {"price": unfilled_action.limit.price}}
        if getattr(unfilled_action, "market", None):
            return {"market": {}}
        return None


# Backwards compatibility alias
WebSocketClient = TakerStreamClient
