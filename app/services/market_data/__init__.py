from __future__ import annotations

from app.config import settings
from app.services.market_data.alpaca_provider import AlpacaProvider
from app.services.market_data.base import MarketDataProvider
from app.services.market_data.ibkr_provider import IBKRProvider
from app.services.market_data.mock_provider import MockProvider
from app.services.market_data.polygon_provider import PolygonProvider
from app.services.market_data.tradier_provider import TradierProvider
from app.services.market_data.yahoo_provider import YahooProvider


def get_provider(name: str | None = None) -> MarketDataProvider:
    provider_name = (name or settings.market_data_provider or "mock").lower()
    if provider_name == "tradier":
        return TradierProvider(settings.tradier_token)
    if provider_name in {"yahoo", "yahoo_finance"}:
        return YahooProvider()
    if provider_name == "polygon":
        return PolygonProvider(settings.polygon_api_key)
    if provider_name == "massive":
        return PolygonProvider(settings.massive_api_key or settings.polygon_api_key)
    if provider_name == "alpaca":
        return AlpacaProvider(settings.alpaca_key_id, settings.alpaca_secret_key)
    if provider_name == "ibkr":
        return IBKRProvider(settings.ibkr_host, settings.ibkr_port, settings.ibkr_client_id)
    return MockProvider(settings.sample_data_dir)
