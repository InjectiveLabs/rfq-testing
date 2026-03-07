"""Helpers for indexer (TakerStream/MakerStream) tests."""

import time
from typing import Optional, TYPE_CHECKING

from rfq_test.clients.websocket import TakerStreamClient
from rfq_test.factories.request import RequestFactory

if TYPE_CHECKING:
    from rfq_test.models.config import EnvironmentConfig
    from rfq_test.crypto.wallet import Wallet


async def create_request_via_taker(
    env_config: "EnvironmentConfig",
    wallet: "Wallet",
    client_id: Optional[str] = None,
) -> dict:
    """Create a valid RFQ request via TakerStream so the indexer has the request.

    Uses RequestFactory.create_indexer_request with direction "long".
    Returns the request data dict that was sent (including rfq_id).

    Args:
        env_config: Environment configuration
        wallet: Taker wallet
        client_id: Correlation ID (UUID). Pass the same value to
                   MakerStreamClient.wait_for_request() to filter on shared environments.
    """
    market = env_config.default_market
    factory = RequestFactory(default_market=market)
    request_data = factory.create_indexer_request(
        taker_address=wallet.inj_address,
        market=market,
        direction="long",
        margin=1000,
        quantity=100,
        worst_price=10,
        rfq_id=int(time.time() * 1000),
    )
    if client_id:
        request_data["client_id"] = client_id
    # Indexer TakerStream expects request expiry in milliseconds.
    request_data["expiry"] = int(time.time() * 1000) + 300_000
    async with TakerStreamClient(
        env_config.indexer.ws_endpoint,
        request_address=wallet.inj_address,
        timeout=10.0,
    ) as taker:
        response = await taker.send_request(request_data, wait_for_response=True)
    if response.get("type") != "ack" or response.get("status") != "success":
        raise RuntimeError(f"Failed to create request for quote test: {response}")
    return request_data
