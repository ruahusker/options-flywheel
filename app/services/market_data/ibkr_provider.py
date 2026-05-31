from __future__ import annotations

from datetime import date, datetime

from app.schemas.market_data import Bar, MarketStatus, OptionContractSchema, OptionContractSnapshot, Quote
from app.services.market_data.base import MarketDataProvider, ProviderConfigurationError


class IBKRProvider(MarketDataProvider):
    name = "ibkr"

    def __init__(self, host: str, port: int, client_id: int):
        self.host = host
        self.port = port
        self.client_id = client_id

    def get_market_status(self) -> MarketStatus:
        return MarketStatus(
            provider=self.name,
            status="local_gateway_required",
            timestamp=datetime.utcnow(),
            warnings=["IBKR requires a running local TWS/Gateway; integration is scaffolded in this MVP."],
        )

    def get_quote(self, symbol: str) -> Quote:
        raise ProviderConfigurationError("IBKR live market data requires a running local TWS/Gateway and is scaffolded in this MVP.")

    def get_price_history(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]:
        raise ProviderConfigurationError("IBKR price history is scaffolded in this MVP.")

    def get_option_expirations(self, symbol: str) -> list[date]:
        raise ProviderConfigurationError("IBKR option expirations are scaffolded in this MVP.")

    def get_option_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        raise ProviderConfigurationError("IBKR option computations are scaffolded in this MVP.")

    def get_option_snapshot(self, symbol: str, option_symbol: str) -> OptionContractSnapshot:
        raise ProviderConfigurationError("IBKR option snapshot is scaffolded in this MVP.")
