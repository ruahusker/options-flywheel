from __future__ import annotations

from datetime import date, datetime

import httpx

from app.schemas.market_data import Bar, MarketStatus, OptionContractSchema, OptionContractSnapshot, Quote
from app.services.market_data.base import MarketDataProvider, ProviderConfigurationError, ProviderError


class TradierProvider(MarketDataProvider):
    name = "tradier"

    def __init__(self, token: str | None):
        self.token = token
        self.base_url = "https://api.tradier.com/v1"

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise ProviderConfigurationError("TRADIER_TOKEN is not configured.")
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def get_market_status(self) -> MarketStatus:
        if not self.token:
            return MarketStatus(provider=self.name, status="unconfigured", timestamp=datetime.utcnow(), warnings=["TRADIER_TOKEN is not configured."])
        try:
            response = httpx.get(f"{self.base_url}/markets/clock", headers=self._headers(), timeout=10)
            response.raise_for_status()
            clock = response.json().get("clock", {})
            state = clock.get("state", "unknown")
            return MarketStatus(provider=self.name, status=state, timestamp=datetime.utcnow(), is_open=state == "open")
        except Exception as exc:
            return MarketStatus(provider=self.name, status="error", timestamp=datetime.utcnow(), warnings=[str(exc)])

    def get_quote(self, symbol: str) -> Quote:
        status = self.get_market_status()
        try:
            response = httpx.get(
                f"{self.base_url}/markets/quotes",
                params={"symbols": symbol.upper(), "greeks": "false"},
                headers=self._headers(),
                timeout=10,
            )
            response.raise_for_status()
            quote = response.json().get("quotes", {}).get("quote", {})
            price = quote.get("last") or quote.get("close")
            return Quote(
                symbol=symbol.upper(),
                price=float(price) if price is not None else None,
                bid=_float(quote.get("bid")),
                ask=_float(quote.get("ask")),
                previous_close=_float(quote.get("prevclose")),
                timestamp=datetime.utcnow(),
                provider=self.name,
                market_status=status.status,
                is_stale=status.status != "open",
                warnings=status.warnings,
            )
        except ProviderConfigurationError:
            raise
        except Exception as exc:
            raise ProviderError(f"Tradier quote failed for {symbol}: {exc}") from exc

    def get_price_history(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]:
        if interval != "1d":
            interval = "daily"
        response = httpx.get(
            f"{self.base_url}/markets/history",
            params={"symbol": symbol.upper(), "interval": "daily"},
            headers=self._headers(),
            timeout=15,
        )
        response.raise_for_status()
        days = response.json().get("history", {}).get("day", []) or []
        bars = [
            Bar(
                symbol=symbol.upper(),
                date_time=datetime.fromisoformat(day["date"]),
                open=float(day["open"]),
                high=float(day["high"]),
                low=float(day["low"]),
                close=float(day["close"]),
                volume=int(day.get("volume") or 0),
                interval="1d",
                provider=self.name,
            )
            for day in days[-lookback_days:]
        ]
        return bars

    def get_option_expirations(self, symbol: str) -> list[date]:
        response = httpx.get(
            f"{self.base_url}/markets/options/expirations",
            params={"symbol": symbol.upper(), "includeAllRoots": "true", "strikes": "false"},
            headers=self._headers(),
            timeout=10,
        )
        response.raise_for_status()
        expirations = response.json().get("expirations", {}).get("date", []) or []
        return [date.fromisoformat(item) for item in expirations]

    def get_option_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        status = self.get_market_status()
        response = httpx.get(
            f"{self.base_url}/markets/options/chains",
            params={"symbol": symbol.upper(), "expiration": expiration.isoformat(), "greeks": "true"},
            headers=self._headers(),
            timeout=20,
        )
        response.raise_for_status()
        raw_options = response.json().get("options", {}).get("option", []) or []
        contracts: list[OptionContractSchema] = []
        dte = max((expiration - date.today()).days, 0)
        for raw in raw_options:
            greeks = raw.get("greeks") or {}
            bid = _float(raw.get("bid"))
            ask = _float(raw.get("ask"))
            mid = (bid + ask) / 2 if bid is not None and ask is not None else _float(raw.get("last"))
            contracts.append(
                OptionContractSchema(
                    underlying=symbol.upper(),
                    expiration=expiration,
                    option_type="call" if raw.get("option_type") == "call" else "put",
                    strike=float(raw["strike"]),
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    last=_float(raw.get("last")),
                    volume=_int(raw.get("volume")),
                    open_interest=_int(raw.get("open_interest")),
                    implied_volatility=_float(greeks.get("mid_iv") or greeks.get("smv_vol")),
                    delta=_float(greeks.get("delta")),
                    gamma=_float(greeks.get("gamma")),
                    theta=_float(greeks.get("theta")),
                    vega=_float(greeks.get("vega")),
                    dte=dte,
                    provider_symbol=raw.get("symbol"),
                    liquidity_score=None,
                    provider=self.name,
                    timestamp=datetime.utcnow(),
                    market_status=status.status,
                    is_stale=status.status != "open",
                    warnings=status.warnings,
                )
            )
        return contracts

    def get_option_snapshot(self, symbol: str, option_symbol: str) -> OptionContractSnapshot:
        quote = self.get_quote(option_symbol)
        _ = quote
        return OptionContractSnapshot(
            symbol=symbol.upper(),
            option_symbol=option_symbol,
            contract=None,
            timestamp=datetime.utcnow(),
            provider=self.name,
            market_status=self.get_market_status().status,
            is_stale=True,
            warnings=["Tradier snapshot endpoint is represented by chain normalization in this MVP."],
        )


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _int(value) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))
