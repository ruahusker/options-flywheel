from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.config import settings as app_settings
from app.schemas.market_data import Bar, MarketStatus, OptionContractSchema, OptionContractSnapshot, Quote
from app.services.market_data.base import MarketDataProvider, ProviderError
from app.services.options_math import black_scholes_merton, implied_volatility


class YahooProvider(MarketDataProvider):
    name = "yahoo"

    def __init__(self):
        self._crumb: str | None = None
        self._cookies = httpx.Cookies()
        self._headers = {
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "Mozilla/5.0",
        }

    def get_market_status(self) -> MarketStatus:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        is_weekday = now_et.weekday() < 5
        is_open = is_weekday and time(9, 30) <= now_et.time() <= time(16, 0)
        status = "open" if is_open else ("after-hours" if is_weekday else "weekend")
        return MarketStatus(
            provider=self.name,
            status=status,
            timestamp=datetime.utcnow(),
            is_open=is_open,
            is_delayed=True,
            warnings=[
                "Yahoo Finance public market data may be delayed or rate-limited; confirm quotes in brokerage before trading.",
                "Market status uses a weekday/hour approximation and does not account for exchange holidays.",
            ],
        )

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        status = self.get_market_status()
        result = self._chart_result(symbol, range_="5d", interval="1d")
        meta = result.get("meta", {})
        timestamps = result.get("timestamp") or []
        quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quotes.get("close") or []
        price = _float(meta.get("regularMarketPrice")) or _last_number(closes)
        previous_close = _float(meta.get("chartPreviousClose")) or _previous_number(closes)
        quote_time = _datetime_from_unix(meta.get("regularMarketTime") or (timestamps[-1] if timestamps else None))
        market_state = str(meta.get("marketState") or status.status).lower()
        warnings = list(status.warnings)
        if market_state not in {"regular", "open"}:
            warnings.append("Latest quote is outside regular market hours.")
        return Quote(
            symbol=symbol,
            price=price,
            bid=_float(meta.get("bid")),
            ask=_float(meta.get("ask")),
            previous_close=previous_close,
            timestamp=quote_time or status.timestamp,
            provider=self.name,
            market_status=market_state,
            is_stale=market_state not in {"regular", "open"},
            warnings=warnings,
        )

    def get_price_history(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]:
        symbol = symbol.upper()
        yahoo_interval = "1d" if interval in {"1d", "daily"} else interval
        result = self._chart_result(symbol, range_=_range_for_lookback(lookback_days), interval=yahoo_interval)
        timestamps = result.get("timestamp") or []
        quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
        bars: list[Bar] = []
        for idx, ts in enumerate(timestamps):
            open_ = _list_float(quotes.get("open"), idx)
            high = _list_float(quotes.get("high"), idx)
            low = _list_float(quotes.get("low"), idx)
            close = _list_float(quotes.get("close"), idx)
            if open_ is None or high is None or low is None or close is None:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    date_time=_datetime_from_unix(ts) or datetime.utcnow(),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=_list_int(quotes.get("volume"), idx),
                    interval="1d",
                    provider=self.name,
                )
            )
        return bars[-lookback_days:]

    def get_option_expirations(self, symbol: str) -> list[date]:
        result = self._option_chain_result(symbol.upper())
        raw_dates = result.get("expirationDates") or []
        return sorted(_date_from_unix(item) for item in raw_dates if _date_from_unix(item) is not None)

    def get_option_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        symbol = symbol.upper()
        result = self._option_chain_result(symbol, expiration)
        quote = result.get("quote") or {}
        spot = _float(quote.get("regularMarketPrice")) or _float(quote.get("postMarketPrice"))
        status_text = str(quote.get("marketState") or self.get_market_status().status).lower()
        option_blocks = result.get("options") or []
        if not option_blocks:
            return []
        block = option_blocks[0]
        contracts: list[OptionContractSchema] = []
        for option_type, raw_options in (("call", block.get("calls") or []), ("put", block.get("puts") or [])):
            for raw in raw_options:
                contract = self._normalize_contract(symbol, expiration, option_type, raw, spot, status_text)
                if contract is not None:
                    contracts.append(contract)
        return contracts

    def get_option_snapshot(self, symbol: str, option_symbol: str) -> OptionContractSnapshot:
        symbol = symbol.upper()
        expiration = _expiration_from_yahoo_contract(option_symbol)
        if expiration is None:
            return OptionContractSnapshot(
                symbol=symbol,
                option_symbol=option_symbol,
                contract=None,
                timestamp=datetime.utcnow(),
                provider=self.name,
                market_status=self.get_market_status().status,
                is_stale=True,
                warnings=["Could not parse Yahoo option symbol expiration."],
            )
        for contract in self.get_option_chain(symbol, expiration):
            if contract.provider_symbol == option_symbol:
                return OptionContractSnapshot(
                    symbol=symbol,
                    option_symbol=option_symbol,
                    contract=contract,
                    timestamp=datetime.utcnow(),
                    provider=self.name,
                    market_status=contract.market_status,
                    is_stale=contract.is_stale,
                    warnings=contract.warnings,
                )
        return OptionContractSnapshot(
            symbol=symbol,
            option_symbol=option_symbol,
            contract=None,
            timestamp=datetime.utcnow(),
            provider=self.name,
            market_status=self.get_market_status().status,
            is_stale=True,
            warnings=["Option contract was not found in Yahoo option chain."],
        )

    def _normalize_contract(
        self,
        symbol: str,
        expiration: date,
        option_type: str,
        raw: dict,
        spot: float | None,
        market_status: str,
    ) -> OptionContractSchema | None:
        strike = _float(raw.get("strike"))
        if strike is None:
            return None
        bid = _float(raw.get("bid"))
        ask = _float(raw.get("ask"))
        last = _float(raw.get("lastPrice"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None and ask > 0 else last
        iv = _float(raw.get("impliedVolatility"))
        dte = max((expiration - date.today()).days, 0)
        greeks = None
        if spot and dte >= 0 and mid and strike > 0:
            time_years = max(dte / 365.0, 1.0 / 365.0)
            rate = app_settings.risk_free_rate
            # IBIT/ASST pay no dividend, so dividend_yield defaults to 0.
            volatility = iv or implied_volatility(mid, spot, strike, time_years, rate, option_type)
            if volatility:
                try:
                    greeks = black_scholes_merton(spot, strike, time_years, rate, volatility, option_type)
                    iv = volatility
                except Exception:
                    greeks = None
        warnings = [
            "Yahoo Finance public option data may be delayed or rate-limited; confirm chain in brokerage before trading."
        ]
        if greeks is None:
            warnings.append("Greeks were unavailable and could not be calculated from available fields.")
        else:
            warnings.append("Greeks are calculated locally with Black-Scholes-Merton using Yahoo IV/price inputs.")
        return OptionContractSchema(
            underlying=symbol,
            expiration=expiration,
            option_type=option_type,
            strike=strike,
            bid=bid,
            ask=ask,
            mid=mid,
            last=last,
            volume=_int(raw.get("volume")),
            open_interest=_int(raw.get("openInterest")),
            implied_volatility=iv,
            delta=greeks.delta if greeks else None,
            gamma=greeks.gamma if greeks else None,
            theta=greeks.theta if greeks else None,
            vega=greeks.vega if greeks else None,
            dte=dte,
            provider_symbol=raw.get("contractSymbol"),
            liquidity_score=None,
            provider=self.name,
            timestamp=_datetime_from_unix(raw.get("lastTradeDate")) or datetime.utcnow(),
            market_status=market_status,
            is_stale=market_status not in {"regular", "open"},
            warnings=warnings,
        )

    def _chart_result(self, symbol: str, *, range_: str, interval: str) -> dict:
        data = self._request_json(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
            {"range": range_, "interval": interval},
            needs_crumb=False,
        )
        error = data.get("chart", {}).get("error")
        if error:
            raise ProviderError(f"Yahoo chart failed for {symbol}: {error}")
        results = data.get("chart", {}).get("result") or []
        if not results:
            raise ProviderError(f"Yahoo chart returned no data for {symbol}.")
        return results[0]

    def _option_chain_result(self, symbol: str, expiration: date | None = None) -> dict:
        params: dict[str, str | int] = {}
        if expiration is not None:
            params["date"] = int(datetime(expiration.year, expiration.month, expiration.day, tzinfo=timezone.utc).timestamp())
        data = self._request_json(
            f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}",
            params,
            needs_crumb=True,
        )
        error = data.get("optionChain", {}).get("error")
        if error:
            raise ProviderError(f"Yahoo option chain failed for {symbol}: {error}")
        results = data.get("optionChain", {}).get("result") or []
        if not results:
            raise ProviderError(f"Yahoo option chain returned no data for {symbol}.")
        return results[0]

    def _request_json(self, url: str, params: dict | None, *, needs_crumb: bool) -> dict:
        params = dict(params or {})
        if needs_crumb:
            params["crumb"] = self._ensure_crumb()
        try:
            with httpx.Client(headers=self._headers, cookies=self._cookies, follow_redirects=True, timeout=20) as client:
                response = client.get(url, params=params)
                if needs_crumb and response.status_code in {401, 403}:
                    self._crumb = None
                    params["crumb"] = self._ensure_crumb()
                    response = client.get(url, params=params)
                response.raise_for_status()
                self._cookies = client.cookies
                data = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"Yahoo request failed with HTTP {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except Exception as exc:
            raise ProviderError(f"Yahoo request failed: {exc}") from exc
        return data

    def _ensure_crumb(self) -> str:
        if self._crumb:
            return self._crumb
        try:
            with httpx.Client(headers=self._headers, cookies=self._cookies, follow_redirects=True, timeout=15) as client:
                client.get("https://fc.yahoo.com")
                response = client.get("https://query1.finance.yahoo.com/v1/test/getcrumb")
                response.raise_for_status()
                self._cookies = client.cookies
                crumb = response.text.strip()
        except Exception as exc:
            raise ProviderError(f"Yahoo crumb request failed: {exc}") from exc
        if not crumb or crumb.startswith("{"):
            raise ProviderError("Yahoo crumb request did not return a usable token.")
        self._crumb = crumb
        return crumb


def _range_for_lookback(lookback_days: int) -> str:
    if lookback_days <= 30:
        return "1mo"
    if lookback_days <= 90:
        return "3mo"
    if lookback_days <= 180:
        return "6mo"
    if lookback_days <= 365:
        return "1y"
    return "2y"


def _datetime_from_unix(value) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _date_from_unix(value) -> date | None:
    dt = _datetime_from_unix(value)
    return dt.date() if dt else None


def _expiration_from_yahoo_contract(symbol: str) -> date | None:
    match = re.match(r"^[A-Z]+(\d{6})[CP]\d{8}$", symbol.strip().upper())
    if not match:
        return None
    raw = match.group(1)
    return date(2000 + int(raw[:2]), int(raw[2:4]), int(raw[4:6]))


def _float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _list_float(values, index: int) -> float | None:
    if values is None or index >= len(values):
        return None
    return _float(values[index])


def _list_int(values, index: int) -> int | None:
    if values is None or index >= len(values):
        return None
    return _int(values[index])


def _last_number(values) -> float | None:
    if not values:
        return None
    for value in reversed(values):
        parsed = _float(value)
        if parsed is not None:
            return parsed
    return None


def _previous_number(values) -> float | None:
    seen = 0
    if not values:
        return None
    for value in reversed(values):
        parsed = _float(value)
        if parsed is None:
            continue
        seen += 1
        if seen == 2:
            return parsed
    return None
