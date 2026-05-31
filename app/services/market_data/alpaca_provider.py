from __future__ import annotations

from datetime import date, datetime

from app.schemas.market_data import Bar, MarketStatus, OptionContractSchema, OptionContractSnapshot, Quote
from app.services.market_data.base import MarketDataProvider, ProviderConfigurationError


class AlpacaProvider(MarketDataProvider):
    name = "alpaca"

    def __init__(self, key_id: str | None, secret_key: str | None):
        self.key_id = key_id
        self.secret_key = secret_key

    def _check(self) -> None:
        if not self.key_id or not self.secret_key:
            raise ProviderConfigurationError("ALPACA_KEY_ID and ALPACA_SECRET_KEY are not configured.")

    def get_market_status(self) -> MarketStatus:
        if not self.key_id or not self.secret_key:
            return MarketStatus(provider=self.name, status="unconfigured", timestamp=datetime.utcnow(), warnings=["Alpaca credentials are not configured."])
        return MarketStatus(provider=self.name, status="configured", timestamp=datetime.utcnow(), warnings=["Alpaca live calls are scaffolded in this MVP."])

    def get_quote(self, symbol: str) -> Quote:
        self._check()
        raise ProviderConfigurationError("Alpaca quote normalization is scaffolded in this MVP.")

    def get_price_history(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]:
        self._check()
        raise ProviderConfigurationError("Alpaca price history is scaffolded in this MVP.")

    def get_option_expirations(self, symbol: str) -> list[date]:
        self._check()
        raise ProviderConfigurationError("Alpaca option expirations are scaffolded in this MVP.")

    def get_option_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        self._check()
        raise ProviderConfigurationError("Alpaca option chain is scaffolded in this MVP.")

    def get_option_snapshot(self, symbol: str, option_symbol: str) -> OptionContractSnapshot:
        self._check()
        raise ProviderConfigurationError("Alpaca option snapshot is scaffolded in this MVP.")
