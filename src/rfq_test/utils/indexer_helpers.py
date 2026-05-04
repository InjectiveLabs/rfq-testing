"""Helpers for indexer (TakerStream/MakerStream) tests."""

import uuid
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
    Returns the request data dict plus the indexer-assigned rfq_id.

    Args:
        env_config: Environment configuration
        wallet: Taker wallet
        client_id: Correlation ID (UUID). Use the returned rfq_id for quote
                   collection, quote signing, and settlement.
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
        client_id=client_id or str(uuid.uuid4()),
    )
    async with TakerStreamClient(
        env_config.indexer.ws_endpoint,
        request_address=wallet.inj_address,
        timeout=10.0,
    ) as taker:
        response = await taker.send_request(request_data, wait_for_response=True)
    if response.get("type") != "ack" or response.get("status") != "success":
        raise RuntimeError(f"Failed to create request for quote test: {response}")
    return {
        **request_data,
        "rfq_id": str(response["rfq_id"]),
        "status": response["status"],
    }
