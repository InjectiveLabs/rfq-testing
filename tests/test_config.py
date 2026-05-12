from decimal import Decimal

from rfq_test.config import get_environment_config, get_settings, load_environment_config


def test_mainnet_config_is_doge_usdc_only():
    config = load_environment_config("mainnet")

    assert config.environment == "mainnet"
    assert config.chain.chain_id == "injective-1"
    assert config.chain.evm_chain_id == 1776
    assert config.contract.address == ""
    assert len(config.markets) == 1

    market = config.default_market
    assert market.symbol == "DOGE/USDC PERP"
    assert market.id == "0xb562b7a440a435e0065d1a22ab74f1f40f25e35476854cd62ee2897c444a287d"
    assert market.quote_denom == "erc20:0xa00C59fF5a080D2b954d0c75e46E22a0c371235a"
    assert market.min_price_tick == Decimal("0.0001")
    assert market.min_quantity_tick == Decimal("1")
    assert market.min_notional == Decimal("1")


def test_mainnet_env_overrides_contract_and_credentials(monkeypatch):
    get_settings.cache_clear()
    get_environment_config.cache_clear()
    monkeypatch.setenv("RFQ_ENV", "mainnet")
    monkeypatch.setenv("RFQ_CONTRACT_ADDRESS", "inj1mainnetcontract")
    monkeypatch.setenv("MAINNET_MM_PRIVATE_KEY", "11" * 32)
    monkeypatch.setenv("MAINNET_RETAIL_PRIVATE_KEY", "22" * 32)

    try:
        settings = get_settings()
        config = get_environment_config()
    finally:
        get_settings.cache_clear()
        get_environment_config.cache_clear()

    assert settings.mm_private_key == "11" * 32
    assert settings.retail_private_key == "22" * 32
    assert config.contract.address == "inj1mainnetcontract"
