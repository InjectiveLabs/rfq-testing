"""Configuration loader with environment support."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from rfq_test.models.config import EnvironmentConfig


class Settings(BaseSettings):
    """Application settings loaded from environment variables.
    
    Credentials are stored per-environment with prefixes:
    LOCAL_, TESTNET_
    
    The active environment is selected via RFQ_ENV, and the appropriate
    credentials are automatically used via computed properties.
    """
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # Environment selection: local | testnet
    rfq_env: str = Field(default="local", alias="RFQ_ENV")
    
    # ============================================================
    # Local Environment Credentials
    # ============================================================
    local_admin_private_key: Optional[str] = Field(default=None, alias="LOCAL_ADMIN_PRIVATE_KEY")
    local_retail_private_key: Optional[str] = Field(default=None, alias="LOCAL_RETAIL_PRIVATE_KEY")
    local_mm_private_key: Optional[str] = Field(default=None, alias="LOCAL_MM_PRIVATE_KEY")
    local_load_test_mm_seed_phrase: Optional[str] = Field(default=None, alias="LOCAL_LOAD_TEST_MM_SEED_PHRASE")
    local_load_test_retail_seed_phrase: Optional[str] = Field(default=None, alias="LOCAL_LOAD_TEST_RETAIL_SEED_PHRASE")
    

    
    # ============================================================
    # Testnet Environment Credentials
    # ============================================================
    testnet_admin_private_key: Optional[str] = Field(default=None, alias="TESTNET_ADMIN_PRIVATE_KEY")
    testnet_retail_private_key: Optional[str] = Field(default=None, alias="TESTNET_RETAIL_PRIVATE_KEY")
    testnet_mm_private_key: Optional[str] = Field(default=None, alias="TESTNET_MM_PRIVATE_KEY")
    testnet_load_test_mm_seed_phrase: Optional[str] = Field(default=None, alias="TESTNET_LOAD_TEST_MM_SEED_PHRASE")
    testnet_load_test_retail_seed_phrase: Optional[str] = Field(default=None, alias="TESTNET_LOAD_TEST_RETAIL_SEED_PHRASE")
    
    # ============================================================
    # Faucet & Endpoint Overrides
    # ============================================================
    faucet_api_url: Optional[str] = Field(default=None, alias="FAUCET_API_URL")
    
    # Optional endpoint overrides (takes precedence over YAML config)
    indexer_ws_url: Optional[str] = Field(default=None, alias="RFQ_WS_URL")
    indexer_http_url: Optional[str] = Field(default=None, alias="RFQ_HTTP_URL")
    chain_grpc_url: Optional[str] = Field(default=None, alias="CHAIN_GRPC_URL")
    chain_lcd_url: Optional[str] = Field(default=None, alias="CHAIN_LCD_URL")
    
    # ============================================================
    # Computed Properties - Return credentials for active environment
    # ============================================================
    def _get_env_credential(self, credential_type: str) -> Optional[str]:
        """Get credential for the active environment.
        
        Args:
            credential_type: One of 'admin_private_key', 'retail_private_key', 
                           'mm_private_key', 'load_test_mm_seed_phrase', 
                           'load_test_retail_seed_phrase'
        """
        env = self.rfq_env.lower()
        attr_name = f"{env}_{credential_type}"
        return getattr(self, attr_name, None)
    
    @computed_field
    @property
    def admin_private_key(self) -> Optional[str]:
        """Get admin private key for the active environment."""
        return self._get_env_credential("admin_private_key")
    
    @computed_field
    @property
    def retail_private_key(self) -> Optional[str]:
        """Get retail private key for the active environment."""
        return self._get_env_credential("retail_private_key")
    
    @computed_field
    @property
    def mm_private_key(self) -> Optional[str]:
        """Get MM private key for the active environment."""
        return self._get_env_credential("mm_private_key")
    
    @computed_field
    @property
    def load_test_mm_seed_phrase(self) -> Optional[str]:
        """Get MM seed phrase for load testing in the active environment."""
        return self._get_env_credential("load_test_mm_seed_phrase")
    
    @computed_field
    @property
    def load_test_retail_seed_phrase(self) -> Optional[str]:
        """Get retail seed phrase for load testing in the active environment."""
        return self._get_env_credential("load_test_retail_seed_phrase")


def load_environment_config(env_name: str, config_dir: Optional[Path] = None) -> EnvironmentConfig:
    """Load environment configuration from YAML file.
    
    Args:
        env_name: Environment name (local, testnet)
        config_dir: Optional path to configs directory
        
    Returns:
        Parsed EnvironmentConfig
    """
    if config_dir is None:
        # Default to configs/ in project root
        config_dir = Path(__file__).parent.parent.parent / "configs"
    
    config_file = config_dir / f"{env_name}.yaml"
    
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")
    
    with open(config_file) as f:
        data = yaml.safe_load(f)
    
    return EnvironmentConfig(**data)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


@lru_cache
def get_environment_config() -> EnvironmentConfig:
    """Get cached environment configuration.
    
    Environment variables can override YAML config:
    - RFQ_WS_URL: Override WebSocket endpoint
    - RFQ_HTTP_URL: Override HTTP endpoint
    - CHAIN_GRPC_URL: Override chain gRPC endpoint
    - CHAIN_LCD_URL: Override chain LCD endpoint
    """
    settings = get_settings()
    config = load_environment_config(settings.rfq_env)
    
    # Apply environment variable overrides
    if settings.indexer_ws_url:
        config.indexer.ws_endpoint = settings.indexer_ws_url
    if settings.indexer_http_url:
        config.indexer.http_endpoint = settings.indexer_http_url
    if settings.chain_grpc_url:
        config.chain.grpc_endpoint = settings.chain_grpc_url
    if settings.chain_lcd_url:
        config.chain.lcd_endpoint = settings.chain_lcd_url
    
    return config


def get_all_markets():
    """Get all configured markets for test parametrization."""
    config = get_environment_config()
    return config.markets


def get_market(symbol: str):
    """Get a specific market by symbol."""
    config = get_environment_config()
    return config.get_market(symbol)
