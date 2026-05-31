from __future__ import annotations

from datetime import date, datetime

from app.schemas.market_data import Bar, MarketStatus, OptionContractSchema, OptionContractSnapshot, Quote
from app.services.market_data.base import MarketDataProvider, ProviderConfigurationError


class PolygonProvider(MarketDataProvider):
    name = "polygon"

    def __init__(self, api_key: str | None):
        self.api_key = api_key

    def _unconfigured(self) -> ProviderConfigurationError:
        return ProviderConfigurationError("POLYGON_API_KEY is not configured.")

    def get_market_status(self) -> MarketStatus:
        if not self.api_key:
            return MarketStatus(provider=self.name, status="unconfigured", timestamp=datetime.utcnow(), warnings=["POLYGON_API_KEY is not configured."])
        return MarketStatus(provider=self.name, status="configured", timestamp=datetime.utcnow(), warnings=["Polygon live calls are scaffolded in this MVP."])

    def get_quote(self, symbol: str) -> Quote:
        if not self.api_key:
            raise self._unconfigured()
        raise ProviderConfigurationError("Polygon live quote normalization is scaffolded; use mock or Tradier for MVP live data.")

    def get_price_history(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]:
        if not self.api_key:
            raise self._unconfigured()
        raise ProviderConfigurationError("Polygon price history is scaffolded in this MVP.")

    def get_option_expirations(self, symbol: str) -> list[date]:
        if not self.api_key:
            raise self._unconfigured()
        raise ProviderConfigurationError("Polygon option expirations are scaffolded in this MVP.")

    def get_option_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        if not self.api_key:
            raise self._unconfigured()
        raise ProviderConfigurationError("Polygon option chain snapshots are scaffolded in this MVP.")

    def get_option_snapshot(self, symbol: str, option_symbol: str) -> OptionContractSnapshot:
        if not self.api_key:
            raise self._unconfigured()
        raise ProviderConfigurationError("Polygon option snapshot is scaffolded in this MVP.")
