"""Factory for generating quote test data."""

import time
from decimal import Decimal
from typing import Optional

from rfq_test.crypto.signing import sign_quote
from rfq_test.models.config import MarketConfig
from rfq_test.models.types import Direction


class QuoteFactory:
    """Factory for creating quote test data.
    
    Supports creating valid signed quotes and intentionally
    invalid quotes for validation testing.
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
        price: Optional[Decimal] = None,
        margin: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
        expiry: Optional[int] = None,
        validity_seconds: Optional[int] = None,
        chain_id: Optional[str] = None,
        contract_address: Optional[str] = None,
        **overrides,
    ) -> dict:
        """Create a valid signed quote.

        For contract verification pass chain_id and contract_address (e.g. from env_config.signing_context()).

        Args:
            maker_private_key: Maker's private key for signing
            maker_address: Maker's Injective address
            request: The request being quoted
            price: Quote price (derives from market if None)
            margin: Maker margin (same as taker if None)
            quantity: Maker quantity (same as taker if None)
            expiry: Expiry timestamp (calculates if None)
            validity_seconds: Quote validity (uses default if None)
            chain_id: Chain ID for contract verification
            contract_address: Contract address for contract verification
            **overrides: Override any field after signing

        Returns:
            Quote data dict with signature (and chain_id/contract_address when provided)
        """
        # Extract request data
        rfq_id = request["rfq_id"]
        market_id = request["market_id"]
        taker = request.get("taker") or request.get("request_address", "")
        direction = request["direction"]
        taker_margin = request["margin"]
        taker_quantity = request["quantity"]
        
        # Default values
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
        
        # Sign the quote (include chain_id/contract_address for contract verification)
        signature = sign_quote(
            private_key=maker_private_key,
            rfq_id=rfq_id,
            market_id=market_id,
            direction=direction,
            taker=taker,
            taker_margin=taker_margin,
            taker_quantity=taker_quantity,
            maker=maker_address,
            maker_margin=str(margin),
            maker_quantity=str(quantity),
            price=str(price),
            expiry=expiry,
            chain_id=chain_id,
            contract_address=contract_address,
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
            "maker_subaccount_nonce": maker_subaccount_nonce,
        }
        if min_fill_quantity is not None:
            quote["min_fill_quantity"] = min_fill_quantity
        if chain_id is not None:
            quote["chain_id"] = chain_id
        if contract_address is not None:
            quote["contract_address"] = contract_address

        # Apply overrides (note: this may invalidate signature)
        quote.update(overrides)
        return quote

    def create_indexer_quote(
        self,
        maker_private_key: str,
        maker_address: str,
        request: dict,
        price: Optional[Decimal] = None,
        margin: Optional[Decimal] = None,
        quantity: Optional[Decimal] = None,
        expiry: Optional[int] = None,
        chain_id: Optional[str] = None,
        contract_address: Optional[str] = None,
        **overrides,
    ) -> dict:
        """Create a quote in the shape expected by the indexer (MakerStream).

        Same as create() but uses 'direction' key (not 'taker_direction') for indexer compatibility.
        Pass chain_id and contract_address for indexer validation and contract-compatible signature.
        """
        quote = self.create(
            maker_private_key=maker_private_key,
            maker_address=maker_address,
            request=request,
            price=price,
            margin=margin,
            quantity=quantity,
            expiry=expiry,
            chain_id=chain_id,
            contract_address=contract_address,
            **overrides,
        )
        # Indexer expects "direction" not "taker_direction"
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
        """Create a quote that's already expired.
        
        Args:
            maker_private_key: Maker's private key
            maker_address: Maker's address
            request: The request
            expired_seconds_ago: How long ago it expired
            **kwargs: Additional parameters
            
        Returns:
            Expired quote
        """
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
        """Create a quote with tampered signature.
        
        Args:
            maker_private_key: Maker's private key
            maker_address: Maker's address
            request: The request
            **kwargs: Additional parameters
            
        Returns:
            Quote with invalid signature
        """
        quote = self.create(
            maker_private_key=maker_private_key,
            maker_address=maker_address,
            request=request,
            **kwargs,
        )
        
        # Tamper with signature
        sig = quote["signature"]
        if sig:
            # Flip a byte
            sig_bytes = bytes.fromhex(sig.replace("0x", ""))
            tampered = bytes([sig_bytes[0] ^ 0xFF]) + sig_bytes[1:]
            quote["signature"] = tampered.hex()
        
        return quote
    
    def create_with_wrong_signer(
        self,
        wrong_private_key: str,
        maker_address: str,
        request: dict,
        **kwargs,
    ) -> dict:
        """Create a quote signed by wrong private key.
        
        The signature is valid but made by a different key
        than the maker address.
        
        Args:
            wrong_private_key: Different private key (not maker's)
            maker_address: Maker's address (mismatched)
            request: The request
            **kwargs: Additional parameters
            
        Returns:
            Quote with mismatched signer
        """
        # Sign with wrong key but claim to be maker_address
        return self.create(
            maker_private_key=wrong_private_key,
            maker_address=maker_address,
            request=request,
            **kwargs,
        )
