from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import monotonic, sleep
from typing import Callable, Iterator
from urllib.parse import quote

import httpx

from app.config import settings


class MassiveClientError(Exception):
    pass


class MassiveCallBudgetExhausted(MassiveClientError):
    pass


@dataclass(frozen=True)
class MassiveOptionContract:
    provider_symbol: str
    underlying: str
    expiration: date
    option_type: str
    strike: float
    shares_per_contract: float | None = None
    exercise_style: str | None = None


@dataclass(frozen=True)
class MassiveBar:
    symbol: str
    date_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    vwap: float | None = None
    transactions: int | None = None


class MassiveRateLimiter:
    def __init__(
        self,
        calls_per_minute: float = 5,
        *,
        clock: Callable[[], float] = monotonic,
        sleep_func: Callable[[float], None] = sleep,
    ):
        if calls_per_minute <= 0:
            raise ValueError("calls_per_minute must be positive.")
        self.min_interval_seconds = 60.0 / calls_per_minute
        self.clock = clock
        self.sleep_func = sleep_func
        self._last_call_started_at: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last_call_started_at is not None:
            delay = self.min_interval_seconds - (now - self._last_call_started_at)
            if delay > 0:
                self.sleep_func(delay)
                now = self.clock()
        self._last_call_started_at = now


class MassiveClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout_seconds: float = 30,
        calls_per_minute: float | None = None,
        max_calls: int | None = None,
        rate_limiter: MassiveRateLimiter | None = None,
        http_client: httpx.Client | None = None,
    ):
        self.api_key = api_key or settings.massive_api_key
        self.base_url = (base_url or settings.massive_base_url).rstrip("/")
        self.max_calls = max_calls
        self.calls_made = 0
        self.rate_limiter = rate_limiter or MassiveRateLimiter(calls_per_minute or settings.massive_calls_per_minute)
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> MassiveClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def iter_option_contract_pages(
        self,
        underlying: str,
        *,
        as_of: date | None = None,
        expired: bool | None = None,
        contract_type: str | None = None,
        expiration: date | None = None,
        expiration_gte: date | None = None,
        expiration_lte: date | None = None,
        limit: int = 1000,
    ) -> Iterator[list[MassiveOptionContract]]:
        params: dict[str, object] = {
            "underlying_ticker": underlying.upper(),
            "limit": limit,
            "sort": "expiration_date",
            "order": "asc",
        }
        if as_of is not None:
            params["as_of"] = as_of.isoformat()
        if expired is not None:
            params["expired"] = "true" if expired else "false"
        if contract_type:
            params["contract_type"] = contract_type.lower()
        if expiration is not None:
            params["expiration_date"] = expiration.isoformat()
        if expiration_gte is not None:
            params["expiration_date.gte"] = expiration_gte.isoformat()
        if expiration_lte is not None:
            params["expiration_date.lte"] = expiration_lte.isoformat()

        next_url: str | None = None
        requested_underlying = underlying.upper()
        while True:
            payload = self._get(next_url or "/v3/reference/options/contracts", None if next_url else params)
            page = [
                contract
                for item in payload.get("results") or []
                if (contract := self._parse_contract(item)) and contract.underlying == requested_underlying
            ]
            yield page
            next_url = payload.get("next_url")
            if not next_url:
                break

    def get_option_bars(
        self,
        option_symbol: str,
        from_date: date,
        to_date: date,
        *,
        multiplier: int = 1,
        timespan: str = "day",
        adjusted: bool = True,
        limit: int = 50000,
    ) -> list[MassiveBar]:
        encoded_symbol = quote(option_symbol, safe=":")
        payload = self._get(
            f"/v2/aggs/ticker/{encoded_symbol}/range/{multiplier}/{timespan}/{from_date.isoformat()}/{to_date.isoformat()}",
            {"adjusted": "true" if adjusted else "false", "sort": "asc", "limit": limit},
        )
        return [bar for item in payload.get("results") or [] if (bar := self._parse_bar(option_symbol, item))]

    def get_stock_bars(
        self,
        symbol: str,
        from_date: date,
        to_date: date,
        *,
        multiplier: int = 1,
        timespan: str = "day",
        adjusted: bool = True,
        limit: int = 50000,
    ) -> list[MassiveBar]:
        encoded_symbol = quote(symbol.upper(), safe="")
        payload = self._get(
            f"/v2/aggs/ticker/{encoded_symbol}/range/{multiplier}/{timespan}/{from_date.isoformat()}/{to_date.isoformat()}",
            {"adjusted": "true" if adjusted else "false", "sort": "asc", "limit": limit},
        )
        return [bar for item in payload.get("results") or [] if (bar := self._parse_bar(symbol.upper(), item))]

    def _get(self, path_or_url: str, params: dict[str, object] | None = None) -> dict:
        if not self.api_key:
            raise MassiveClientError("MASSIVE_API_KEY is not configured.")
        if self.max_calls is not None and self.calls_made >= self.max_calls:
            raise MassiveCallBudgetExhausted(f"Massive call budget exhausted after {self.calls_made} calls.")

        self.rate_limiter.wait()
        self.calls_made += 1
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        response = self._client.get(url, params=self._clean_params(params), headers={"Authorization": f"Bearer {self.api_key}"})
        if response.status_code == 429:
            raise MassiveClientError("Massive rate limit returned HTTP 429; reduce calls_per_minute or run smaller chunks.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:300]
            raise MassiveClientError(f"Massive request failed with HTTP {response.status_code}: {detail}") from exc
        return response.json()

    @staticmethod
    def _clean_params(params: dict[str, object] | None) -> dict[str, object]:
        return {key: value for key, value in (params or {}).items() if value is not None}

    @staticmethod
    def _parse_contract(item: dict) -> MassiveOptionContract | None:
        try:
            provider_symbol = item["ticker"]
            underlying = item["underlying_ticker"].upper()
            expiration = date.fromisoformat(item["expiration_date"])
            option_type = item["contract_type"].lower()
            strike = float(item["strike_price"])
        except (KeyError, TypeError, ValueError):
            return None
        return MassiveOptionContract(
            provider_symbol=provider_symbol,
            underlying=underlying,
            expiration=expiration,
            option_type=option_type,
            strike=strike,
            shares_per_contract=_optional_float(item.get("shares_per_contract")),
            exercise_style=item.get("exercise_style"),
        )

    @staticmethod
    def _parse_bar(symbol: str, item: dict) -> MassiveBar | None:
        try:
            timestamp_ms = int(item["t"])
            date_time = datetime.fromtimestamp(timestamp_ms / 1000, UTC).replace(tzinfo=None)
            return MassiveBar(
                symbol=symbol,
                date_time=date_time,
                open=float(item["o"]),
                high=float(item["h"]),
                low=float(item["l"]),
                close=float(item["c"]),
                volume=_optional_int(item.get("v")),
                vwap=_optional_float(item.get("vw")),
                transactions=_optional_int(item.get("n")),
            )
        except (KeyError, TypeError, ValueError, OSError):
            return None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
