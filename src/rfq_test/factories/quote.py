"""Factory for generating quote test data (v2-only EIP-712 signing)."""

import time
from decimal import Decimal
from typing import Optional

from rfq_test.crypto.eip712 import sign_quote_v2
from rfq_test.models.config import MarketConfig
from rfq_test.models.types import Direction


class QuoteFactory:
    """Factory for creating quote test data.

    Produces v2 (EIP-712) signed quotes. Supports intentionally-invalid
    quotes for validation testing (tampered signature, wrong signer).
    """

    def __init__(
        self,
        default_market: Optional[MarketConfig] = None,
        default_validity_seconds: int = 20,
    ):
        self.default_market = default_market
        self.default_validity_seconds = default_validity_seconds

    def create(
        self,
        maker_private_key: str,
        maker_address: str,
        request: dict,
        evm_chain_id: int,
        contract_address: str,
        chain_id: Optional[str] = None,
        price: Optional[Decimal] = None,
        margin: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
        expiry: Optional[int] = None,
        validity_seconds: Optional[int] = None,
        **overrides,
    ) -> dict:
        """Create a v2-signed quote.

        Args:
            maker_private_key: Maker's private key (hex)
            maker_address: Maker's `inj1...` address
            request: The RFQ request being quoted
            evm_chain_id: EVM chain ID for the EIP-712 domain (testnet/devnet=1439)
            contract_address: RFQ contract bech32 address (verifying contract)
            chain_id: Cosmos chain_id, included in the wire payload only.
                The v2 signature does NOT bind it (the domain separator does).
            price: Quote price (defaults to market price if available)
            margin: Maker margin (defaults to taker margin)
            quantity: Maker quantity (defaults to taker quantity)
            expiry: Expiry as Unix timestamp ms (defaults to validity_seconds from now)
            validity_seconds: Quote validity window when expiry is None
            **overrides: Apply on the returned dict after signing — note that
                overriding signed fields will invalidate the signature.

        Returns:
            Quote dict with `signature`, `sign_mode="v2"`, and all wire fields.
        """
        rfq_id = request["rfq_id"]
        market_id = request["market_id"]
        taker = request.get("taker") or request.get("request_address", "")
        direction = request["direction"]
        taker_margin = request["margin"]
        taker_quantity = request["quantity"]

        if margin is None:
            margin = Decimal(taker_margin)
        if quantity is None:
            quantity = Decimal(taker_quantity)
        if price is None:
            market = self.default_market
            if market and market.price:
                price = market.price
            else:
                price = Decimal("1.0")
        if expiry is None:
            validity = validity_seconds or self.default_validity_seconds
            expiry = int(time.time() * 1000) + (validity * 1000)

        maker_subaccount_nonce = int(overrides.pop("maker_subaccount_nonce", 0))
        min_fill_quantity = overrides.pop("min_fill_quantity", None)
        if min_fill_quantity is not None:
            min_fill_quantity = str(min_fill_quantity)

        signature = sign_quote_v2(
            private_key=maker_private_key,
            evm_chain_id=evm_chain_id,
            verifying_contract_bech32=contract_address,
            market_id=market_id,
            rfq_id=int(rfq_id),
            taker=taker,
            direction=direction,
            taker_margin=taker_margin,
            taker_quantity=taker_quantity,
            maker=maker_address,
            maker_margin=str(margin),
            maker_quantity=str(quantity),
            price=str(price),
            expiry_ms=expiry,
            maker_subaccount_nonce=maker_subaccount_nonce,
            min_fill_quantity=min_fill_quantity,
        )

        quote = {
            "rfq_id": rfq_id,
            "market_id": market_id,
            "taker_direction": direction,
            "taker": taker,
            "margin": str(margin),
            "quantity": str(quantity),
            "price": str(price),
            "expiry": expiry,
            "maker": maker_address,
            "signature": signature,
            "sign_mode": "v2",
            "maker_subaccount_nonce": maker_subaccount_nonce,
            "contract_address": contract_address,
        }
        if min_fill_quantity is not None:
            quote["min_fill_quantity"] = min_fill_quantity
        if chain_id is not None:
            quote["chain_id"] = chain_id

        quote.update(overrides)
        return quote

    def create_indexer_quote(self, *args, **kwargs) -> dict:
        """Create a quote in the shape expected by the indexer (MakerStream).

        Same as `create()` but renames `taker_direction` -> `direction`.
        """
        quote = self.create(*args, **kwargs)
        if "taker_direction" in quote:
            quote["direction"] = quote.pop("taker_direction")
        return quote

    def create_expired(
        self,
        maker_private_key: str,
        maker_address: str,
        request: dict,
        expired_seconds_ago: int = 60,
        **kwargs,
    ) -> dict:
        """Create a quote that's already expired."""
        expiry = int(time.time() * 1000) - (expired_seconds_ago * 1000)
        return self.create(
            maker_private_key=maker_private_key,
            maker_address=maker_address,
            request=request,
            expiry=expiry,
            **kwargs,
        )

    def create_with_invalid_signature(
        self,
        maker_private_key: str,
        maker_address: str,
        request: dict,
        **kwargs,
    ) -> dict:
        """Create a quote with a tampered signature."""
        quote = self.create(
            maker_private_key=maker_private_key,
            maker_address=maker_address,
            request=request,
            **kwargs,
        )

        sig = quote["signature"]
        if sig:
            sig_bytes = bytes.fromhex(sig.replace("0x", ""))
            tampered = bytes([sig_bytes[0] ^ 0xFF]) + sig_bytes[1:]
            quote["signature"] = "0x" + tampered.hex()
        return quote

    def create_with_wrong_signer(
        self,
        wrong_private_key: str,
        maker_address: str,
        request: dict,
        **kwargs,
    ) -> dict:
        """Sign with a different key but claim to be `maker_address`."""
        return self.create(
            maker_private_key=wrong_private_key,
            maker_address=maker_address,
            request=request,
            **kwargs,
        )
