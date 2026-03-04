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
from rfq_test.proto.rfq_messages import (
    CreateRFQRequestType,
    MakerStreamRequest,
    MakerStreamResponse,
    QuoteStreamAck,
    RFQQuoteType,
    RFQRequestType,
    RequestStreamAck,
    TakerStreamRequest,
    TakerStreamResponse,
)

logger = logging.getLogger(__name__)

# gRPC-web WebSocket subprotocol
GRPC_WS_SUBPROTOCOL = "grpc-ws"

# Ping interval to keep connection alive (server requires this)
PING_INTERVAL_SECONDS = 1.0


def _format_connection_closed(exc: Exception) -> str:
    """Format ConnectionClosed for logging: code and reason so we can see why the server closed.
    Common codes: 1000=normal closure, 1008=policy, 1011=server error. If reason is empty,
    check the previous log line for 'Stream error' (indexer may send an error message before closing).
    Uses rcvd.code/rcvd.reason (websockets 13.1+) to avoid deprecation warnings.
    """
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
    payload = message.encode()
    header = struct.pack(">BI", 0, len(payload))  # compression=0, length as big-endian uint32
    return header + payload


def decode_grpc_message(data: bytes, message_type):
    """Decode a gRPC-web framed message.
    
    Returns None if this is a trailer frame (compression flag 0x80).
    """
    if len(data) < 5:
        return None
    
    compression_flag = data[0]
    if compression_flag == 0x80:
        # Trailer frame, ignore
        return None
    if compression_flag != 0:
        logger.warning(f"Unsupported compression flag: {compression_flag}")
        return None
    
    length = struct.unpack(">I", data[1:5])[0]
    payload = data[5:5 + length]
    
    return message_type.decode(payload)


class BaseStreamClient(ABC):
    """Base class for RFQ stream clients."""
    
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
    
    @property
    @abstractmethod
    def stream_path(self) -> str:
        """Stream endpoint path (e.g., '/TakerStream')."""
        pass
    
    @property
    def url(self) -> str:
        """Full WebSocket URL."""
        return f"{self.base_url}{self.stream_path}"
    
    async def connect(self) -> None:
        """Connect to the WebSocket stream."""
        try:
            logger.info(f"Connecting to {self.url}")
            
            # Create SSL context with proper certificates for wss:// connections
            ssl_context = None
            if self.url.startswith("wss://"):
                ssl_context = ssl.create_default_context(cafile=certifi.where())
            
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.url,
                    subprotocols=[GRPC_WS_SUBPROTOCOL],
                    ssl=ssl_context,
                ),
                timeout=self.timeout,
            )
            self._connected = True
            
            # Start background tasks
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._ping_task = asyncio.create_task(self._ping_loop())
            
            # Send initial ping so server gets a frame immediately (avoids idle-close before first scheduled ping)
            try:
                await self._send_ping()
            except Exception as e:
                logger.warning(f"Initial ping failed: {e}")
            
            logger.info(f"Connected to {self.url}")
            
        except asyncio.TimeoutError as e:
            raise IndexerConnectionError(f"Connection timeout: {self.url}") from e
        except Exception as e:
            raise IndexerConnectionError(f"Failed to connect: {e}") from e
    
    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._connected = False
        
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        
        if self._ws:
            await self._ws.close()
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
                # Server closed the connection; log code and reason so we can see why (e.g. 1000=normal, 1008=policy)
                if self._connected:
                    logger.warning("WebSocket connection closed: %s", _format_connection_closed(e))
                break
            except Exception as e:
                if self._connected:
                    logger.error(f"Ping error: {e}")
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
    opening the stream; pass it to connect successfully.
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

    async def connect(self) -> None:
        """Connect to TakerStream; send request_address as metadata if set."""
        try:
            logger.info(f"Connecting to {self.url}")
            ssl_context = None
            if self.url.startswith("wss://"):
                ssl_context = ssl.create_default_context(cafile=certifi.where())
            additional_headers: dict[str, str] = {}
            if self._request_address:
                # Indexer expects gRPC metadata key "request_address" (see DecodeTakerStreamRequest)
                additional_headers["request_address"] = self._request_address
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.url,
                    subprotocols=[GRPC_WS_SUBPROTOCOL],
                    ssl=ssl_context,
                    additional_headers=additional_headers or None,
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

    async def _send_ping(self) -> None:
        """Send ping to keep connection alive."""
        msg = TakerStreamRequest(message_type="ping")
        await self._send_raw(encode_grpc_message(msg))
    
    async def _receive_loop(self) -> None:
        """Receive and queue incoming messages."""
        while self._connected and self._ws:
            try:
                data = await self._ws.recv()
                
                # Skip string messages (headers)
                if isinstance(data, str):
                    logger.debug(f"Received header: {data}")
                    continue
                
                response = decode_grpc_message(data, TakerStreamResponse)
                if response is None:
                    continue
                
                # Handle different message types
                msg_type = response.message_type
                
                if msg_type == "pong":
                    # Silent pong, just connection keepalive
                    pass
                
                elif msg_type == "quote":
                    quote = response.quote
                    logger.info(f"Received quote: RFQ#{quote.rfq_id} price={quote.price} from {quote.maker}")
                    await self._message_queue.put(("quote", quote))
                
                elif msg_type == "request_ack":
                    ack = response.request_ack
                    logger.debug(f"Request ACK: RFQ#{ack.rfq_id} status={ack.status}")
                    await self._message_queue.put(("request_ack", ack))
                
                elif msg_type == "error":
                    err = response.error
                    logger.error(f"Stream error: code={err.code} message={err.message}")
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
    
    async def send_request(self, request_data: dict, wait_for_response: bool = False, response_timeout: float = 2.0) -> Optional[dict]:
        """Send an RFQ request.
        
        Args:
            request_data: Request parameters (rfq_id, market_id, direction, etc.)
            wait_for_response: If True, wait for ACK or error response
            response_timeout: Timeout for waiting for response
            
        Returns:
            Response dict if wait_for_response=True, None otherwise
            
        Raises:
            IndexerValidationError: If server returns an error
        """
        # Handle direction - convert to string if it's an int
        direction = request_data.get("direction", "")
        if isinstance(direction, int):
            direction = str(direction)
        
        # Generate client_id if not provided (used for request correlation)
        client_id = request_data.get("client_id") or str(uuid.uuid4())
        
        # Server gets request_address from TakerStream connection metadata; request body uses CreateRFQRequestType
        request = CreateRFQRequestType(
            client_id=client_id,
            market_id=request_data.get("market_id", ""),
            direction=direction,
            margin=str(request_data.get("margin", "")),
            quantity=str(request_data.get("quantity", "")),
            worst_price=str(request_data.get("worst_price", "0")),
            expiry=int(request_data.get("expiry", int(time.time() * 1000) + 300_000)),
        )
        msg = TakerStreamRequest(message_type="request", request=request)
        
        logger.info(f"Signing request: client_id={client_id} {request.direction} qty={request.quantity}")
        await self._send_raw(encode_grpc_message(msg))
        
        if wait_for_response:
            return await self._wait_for_response(request.rfq_id, response_timeout)
        return None
    
    async def _wait_for_response(self, rfq_id: int, timeout: float) -> dict:
        """Wait for ACK or error response after sending a request.
        
        Args:
            rfq_id: Request ID to wait for
            timeout: Maximum wait time
            
        Returns:
            Response dict with type and data
            
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
                    logger.info(f"Request ACK received: RFQ#{data.rfq_id} client_id={data.client_id} status={data.status}")
                    return response
                
                if msg_type == "error":
                    error_msg = f"{data.code}: {data.message}"
                    logger.warning(f"Request error received: {error_msg}")
                    raise IndexerValidationError(error_msg)
                
                # Put back other messages (e.g., quotes)
                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)
                
            except asyncio.TimeoutError:
                break
        
        # No response received - log this
        logger.warning(f"No response received for RFQ#{rfq_id} within {timeout}s (no ACK, no error)")
        return {"type": "no_response", "rfq_id": rfq_id}
    
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
                    return {"rfq_id": data.rfq_id, "client_id": data.client_id, "status": data.status}
                
                if msg_type == "error":
                    raise IndexerValidationError(f"{data.code}: {data.message}")
                
                # Put back other messages
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
                
                # Put back other messages
                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)
                
            except asyncio.TimeoutError:
                break
        
        raise IndexerTimeoutError(f"No quote for request {rfq_id} within {timeout}s")
    
    async def get_next_event(self, timeout: float = 1.0) -> Optional[tuple]:
        """Get the next stream event (request_ack, quote, error). Returns None on timeout.
        
        Useful for monitoring the stream without accepting quotes.
        """
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
                    timeout=min(remaining, 1.0),  # Check every second
                )
                
                if msg_type == "quote" and data.rfq_id == rfq_id:
                    quotes.append(self._quote_to_dict(data))
                    if len(quotes) >= min_quotes:
                        # Got minimum, wait a bit more for additional quotes
                        await asyncio.sleep(0.5)
                        # Drain any remaining quotes
                        while not self._message_queue.empty():
                            try:
                                msg_type, data = self._message_queue.get_nowait()
                                if msg_type == "quote" and data.rfq_id == rfq_id:
                                    quotes.append(self._quote_to_dict(data))
                            except asyncio.QueueEmpty:
                                break
                        break
                else:
                    # Put back other messages
                    await self._message_queue.put((msg_type, data))
                    
            except asyncio.TimeoutError:
                if quotes:
                    break
                continue
        
        return quotes
    
    def _quote_to_dict(self, quote: RFQQuoteType) -> dict:
        """Convert quote protobuf to dict."""
        return {
            "rfq_id": str(quote.rfq_id),
            "market_id": quote.market_id,
            "taker_direction": quote.taker_direction,
            "margin": quote.margin,
            "quantity": quote.quantity,
            "price": quote.price,
            "expiry": quote.expiry,
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
            # Wait for requests
            async for request in client.requests(timeout=60):
                # Build and send quote
                await client.send_quote(quote_data)
    """
    
    @property
    def stream_path(self) -> str:
        return "/MakerStream"
    
    async def _send_ping(self) -> None:
        """Send ping to keep connection alive."""
        msg = MakerStreamRequest(message_type="ping")
        await self._send_raw(encode_grpc_message(msg))
    
    async def _receive_loop(self) -> None:
        """Receive and queue incoming messages."""
        while self._connected and self._ws:
            try:
                data = await self._ws.recv()
                
                # Skip string messages (headers)
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
                
                elif msg_type == "error":
                    err = response.error
                    logger.error(f"Stream error: code={err.code} message={err.message}")
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
        # Indexer requires "long" or "short" (not 0/1 or "0"/"1")
        taker_direction = quote_data.get("taker_direction", quote_data.get("direction", ""))
        if taker_direction in (0, "0"):
            taker_direction = "long"
        elif taker_direction in (1, "1"):
            taker_direction = "short"
        elif isinstance(taker_direction, str) and taker_direction.lower() in ("long", "short"):
            taker_direction = taker_direction.lower()
        else:
            taker_direction = str(taker_direction).lower() if isinstance(taker_direction, str) else "long"
        
        # Indexer (Go hexutil.Decode) expects hex with 0x prefix
        signature = quote_data.get("signature", "")
        if signature and not signature.startswith("0x"):
            signature = "0x" + signature

        quote = RFQQuoteType(
            chain_id=quote_data.get("chain_id", ""),
            contract_address=quote_data.get("contract_address", ""),
            market_id=quote_data.get("market_id", ""),
            rfq_id=int(quote_data.get("rfq_id", 0)),
            taker_direction=taker_direction,
            margin=str(quote_data.get("margin", "")),
            quantity=str(quote_data.get("quantity", "")),
            price=str(quote_data.get("price", "")),
            expiry=int(quote_data.get("expiry", 0)),
            maker=quote_data.get("maker", ""),
            taker=quote_data.get("taker", ""),
            signature=signature,
            status="pending",
            transaction_time=int(time.time() * 1000),
        )
        
        msg = MakerStreamRequest(message_type="quote", quote=quote)
        
        logger.info(f"Sending quote: RFQ#{quote.rfq_id} price={quote.price}")
        await self._send_raw(encode_grpc_message(msg))
        
        if wait_for_response:
            return await self._wait_for_quote_response(quote.rfq_id, response_timeout)
        return None
    
    async def _wait_for_quote_response(self, rfq_id: int, timeout: float) -> dict:
        """Wait for ACK or error response after sending a quote.
        
        Args:
            rfq_id: RFQ ID the quote is for
            timeout: Maximum wait time
            
        Returns:
            Response dict with type and data
            
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
                
                if msg_type == "quote_ack":
                    response = {
                        "type": "ack",
                        "rfq_id": data.rfq_id,
                        "status": data.status,
                    }
                    logger.info(f"Quote ACK received: RFQ#{data.rfq_id} status={data.status}")
                    return response
                
                if msg_type == "error":
                    error_msg = f"{data.code}: {data.message}"
                    logger.warning(f"Quote error received: {error_msg}")
                    raise IndexerValidationError(error_msg)
                
                # Put back other messages (e.g., requests)
                await self._message_queue.put((msg_type, data))
                await asyncio.sleep(0.01)
                
            except asyncio.TimeoutError:
                break
        
        # No response received - log this
        logger.warning(f"No response received for quote on RFQ#{rfq_id} within {timeout}s (no ACK, no error)")
        return {"type": "no_response", "rfq_id": rfq_id}
    
    async def wait_for_request(self, timeout: float = 30.0) -> dict:
        """Wait for an RFQ request.
        
        Args:
            timeout: Maximum wait time
            
        Returns:
            Request data as dict
        """
        start = time.monotonic()
        while (time.monotonic() - start) < timeout:
            try:
                msg_type, data = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=timeout - (time.monotonic() - start),
                )
                
                if msg_type == "request":
                    return self._request_to_dict(data)
                
                if msg_type == "error":
                    raise IndexerValidationError(f"{data.code}: {data.message}")
                
                # Put back other messages
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
                    logger.error(f"Stream error: {data.code}: {data.message}")
                else:
                    # Put back other messages
                    await self._message_queue.put((msg_type, data))
                    
            except asyncio.TimeoutError:
                continue
    
    def _request_to_dict(self, request: RFQRequestType) -> dict:
        """Convert request protobuf to dict."""
        return {
            "rfq_id": str(request.rfq_id),
            "client_id": request.client_id,
            "market_id": request.market_id,
            "direction": request.direction,
            "margin": request.margin,
            "quantity": request.quantity,
            "worst_price": request.worst_price,
            "request_address": request.request_address,
            "taker": request.request_address,  # Alias for compatibility
            "expiry": request.expiry,
            "status": request.status,
        }


# ============================================================
# Backwards Compatibility Alias
# ============================================================

# For gradual migration, keep WebSocketClient as an alias
# Tests and actors can use TakerStreamClient or MakerStreamClient directly
WebSocketClient = TakerStreamClient
