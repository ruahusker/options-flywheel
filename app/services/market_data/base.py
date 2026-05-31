from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.schemas.market_data import Bar, MarketStatus, OptionContractSchema, OptionContractSnapshot, Quote


class ProviderError(Exception):
    pass


class ProviderConfigurationError(ProviderError):
    pass


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    @abstractmethod
    def get_price_history(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]:
        raise NotImplementedError

    @abstractmethod
    def get_option_expirations(self, symbol: str) -> list[date]:
        raise NotImplementedError

    @abstractmethod
    def get_option_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        raise NotImplementedError

    @abstractmethod
    def get_option_snapshot(self, symbol: str, option_symbol: str) -> OptionContractSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_market_status(self) -> MarketStatus:
        raise NotImplementedError
