from __future__ import annotations

import csv
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.schemas.market_data import Bar, MarketStatus, OptionContractSchema, OptionContractSnapshot, Quote
from app.services.market_data.base import MarketDataProvider


class MockProvider(MarketDataProvider):
    name = "mock"

    def __init__(self, sample_data_dir: str | Path = "sample_data"):
        self.sample_data_dir = Path(sample_data_dir)

    def get_market_status(self) -> MarketStatus:
        now = datetime.utcnow()
        weekday = now.weekday()
        is_open = weekday < 5 and time(14, 30) <= now.time() <= time(21, 0)
        status = "open" if is_open else "after-hours"
        return MarketStatus(
            provider=self.name,
            status=status,
            timestamp=now,
            is_open=is_open,
            is_delayed=False,
            warnings=["Mock provider uses deterministic sample data."],
        )

    def get_quote(self, symbol: str) -> Quote:
        bars = self.get_price_history(symbol, 90, "1d")
        latest = bars[-1] if bars else None
        previous = bars[-2] if len(bars) > 1 else None
        status = self.get_market_status()
        price = latest.close if latest else self._fallback_price(symbol)
        return Quote(
            symbol=symbol.upper(),
            price=price,
            bid=round(price - 0.02, 2) if price else None,
            ask=round(price + 0.02, 2) if price else None,
            previous_close=previous.close if previous else None,
            timestamp=status.timestamp,
            provider=self.name,
            market_status=status.status,
            is_stale=False,
            warnings=status.warnings,
        )

    def get_price_history(self, symbol: str, lookback_days: int, interval: str) -> list[Bar]:
        symbol = symbol.upper()
        path = self.sample_data_dir / f"{symbol.lower()}_ohlcv_sample.csv"
        rows: list[Bar] = []
        if path.exists():
            with path.open(newline="") as fh:
                for row in csv.DictReader(fh):
                    rows.append(
                        Bar(
                            symbol=symbol,
                            date_time=datetime.fromisoformat(row["date_time"]),
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            volume=int(float(row.get("volume") or 0)),
                            interval=interval,
                            provider=self.name,
                        )
                    )
        if len(rows) < lookback_days:
            rows = self._synthetic_bars(symbol, lookback_days)
        return rows[-lookback_days:]

    def get_option_expirations(self, symbol: str) -> list[date]:
        chain = self._read_chain(symbol)
        expirations = sorted({contract.expiration for contract in chain})
        return expirations

    def get_option_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        contracts = [contract for contract in self._read_chain(symbol) if contract.expiration == expiration]
        if contracts:
            return contracts
        return self._synthetic_chain(symbol, expiration)

    def get_option_snapshot(self, symbol: str, option_symbol: str) -> OptionContractSnapshot:
        expirations = self.get_option_expirations(symbol)
        for expiration in expirations:
            for contract in self.get_option_chain(symbol, expiration):
                if contract.provider_symbol == option_symbol:
                    return OptionContractSnapshot(
                        symbol=symbol.upper(),
                        option_symbol=option_symbol,
                        contract=contract,
                        timestamp=datetime.utcnow(),
                        provider=self.name,
                        market_status=self.get_market_status().status,
                        is_stale=False,
                        warnings=["Mock option snapshot."],
                    )
        return OptionContractSnapshot(
            symbol=symbol.upper(),
            option_symbol=option_symbol,
            contract=None,
            timestamp=datetime.utcnow(),
            provider=self.name,
            market_status=self.get_market_status().status,
            is_stale=True,
            warnings=["Option symbol not found in mock chain."],
        )

    def _read_chain(self, symbol: str) -> list[OptionContractSchema]:
        symbol = symbol.upper()
        path = self.sample_data_dir / f"{symbol.lower()}_option_chain_sample.csv"
        if not path.exists():
            next_friday = self._next_friday()
            return self._synthetic_chain(symbol, next_friday)
        contracts: list[OptionContractSchema] = []
        status = self.get_market_status()
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                expiration = date.fromisoformat(row["expiration"])
                bid = self._float(row.get("bid"))
                ask = self._float(row.get("ask"))
                mid = self._float(row.get("mid"))
                if mid is None and bid is not None and ask is not None:
                    mid = round((bid + ask) / 2, 4)
                contracts.append(
                    OptionContractSchema(
                        underlying=symbol,
                        expiration=expiration,
                        option_type=row["option_type"],
                        strike=float(row["strike"]),
                        bid=bid,
                        ask=ask,
                        mid=mid,
                        last=self._float(row.get("last")),
                        volume=self._int(row.get("volume")),
                        open_interest=self._int(row.get("open_interest")),
                        implied_volatility=self._float(row.get("implied_volatility")),
                        delta=self._float(row.get("delta")),
                        gamma=self._float(row.get("gamma")),
                        theta=self._float(row.get("theta")),
                        vega=self._float(row.get("vega")),
                        dte=max((expiration - date.today()).days, 0),
                        provider_symbol=row.get("provider_symbol") or None,
                        liquidity_score=self._float(row.get("liquidity_score")),
                        provider=self.name,
                        timestamp=status.timestamp,
                        market_status=status.status,
                        is_stale=False,
                        warnings=status.warnings,
                    )
                )
        return contracts

    def _synthetic_bars(self, symbol: str, lookback_days: int) -> list[Bar]:
        base = self._fallback_price(symbol) * 0.75
        bars: list[Bar] = []
        start = datetime.utcnow() - timedelta(days=lookback_days + 5)
        for i in range(lookback_days):
            drift = i / max(lookback_days - 1, 1)
            wave = math.sin(i / 4.5) * 0.035
            close = base * (1 + 0.35 * drift + wave)
            open_ = close * (1 - 0.01 * math.sin(i / 3))
            high = max(open_, close) * 1.025
            low = min(open_, close) * 0.975
            bars.append(
                Bar(
                    symbol=symbol,
                    date_time=start + timedelta(days=i),
                    open=round(open_, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=round(close, 2),
                    volume=1_000_000 + i * 3211,
                    interval="1d",
                    provider=self.name,
                )
            )
        return bars

    def _synthetic_chain(self, symbol: str, expiration: date) -> list[OptionContractSchema]:
        quote = self.get_quote(symbol)
        spot = quote.price or self._fallback_price(symbol)
        strikes = [round(spot * m * 2) / 2 for m in (0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.18)]
        contracts: list[OptionContractSchema] = []
        dte = max((expiration - date.today()).days, 1)
        for strike in strikes:
            for option_type in ("call", "put"):
                moneyness = strike / spot
                if option_type == "call":
                    delta = max(0.05, min(0.85, 1.05 - moneyness))
                else:
                    delta = -max(0.05, min(0.85, moneyness - 0.15))
                intrinsic = max(spot - strike, 0) if option_type == "call" else max(strike - spot, 0)
                extrinsic = spot * 0.018 * math.sqrt(dte / 7) * (1 + abs(delta))
                mid = round(intrinsic + extrinsic, 2)
                spread = max(0.04, mid * 0.08)
                contracts.append(
                    OptionContractSchema(
                        underlying=symbol,
                        expiration=expiration,
                        option_type=option_type,
                        strike=strike,
                        bid=round(max(mid - spread / 2, 0.01), 2),
                        ask=round(mid + spread / 2, 2),
                        mid=mid,
                        last=mid,
                        volume=100,
                        open_interest=500,
                        implied_volatility=0.75 if symbol == "IBIT" else 0.95,
                        delta=round(delta, 2),
                        gamma=0.04,
                        theta=-0.05,
                        vega=0.02,
                        dte=dte,
                        provider_symbol=f"{symbol}{expiration:%y%m%d}{'C' if option_type == 'call' else 'P'}{strike:g}",
                        liquidity_score=75,
                        provider=self.name,
                        timestamp=datetime.utcnow(),
                        market_status=self.get_market_status().status,
                        is_stale=False,
                        warnings=["Synthetic mock option chain."],
                    )
                )
        return contracts

    @staticmethod
    def _next_friday() -> date:
        today = date.today()
        days = (4 - today.weekday()) % 7
        if days == 0:
            days = 7
        return today + timedelta(days=days)

    @staticmethod
    def _fallback_price(symbol: str) -> float:
        return {"IBIT": 41.63, "ASST": 17.67, "SATA": 100.01, "BSOL": 11.08}.get(symbol.upper(), 25.0)

    @staticmethod
    def _float(value: str | None) -> float | None:
        if value is None or str(value).strip() == "":
            return None
        return float(value)

    @staticmethod
    def _int(value: str | None) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
