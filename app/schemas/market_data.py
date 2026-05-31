from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class MarketStatus(BaseModel):
    provider: str
    status: str
    timestamp: datetime
    is_open: bool = False
    is_delayed: bool = False
    warnings: list[str] = Field(default_factory=list)


class Quote(BaseModel):
    symbol: str
    price: float | None = None
    bid: float | None = None
    ask: float | None = None
    previous_close: float | None = None
    timestamp: datetime
    provider: str
    market_status: str = "unknown"
    is_stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class Bar(BaseModel):
    symbol: str
    date_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int | None = None
    interval: str = "1d"
    provider: str = "mock"


class OptionContractSchema(BaseModel):
    underlying: str
    expiration: date
    option_type: str
    strike: float
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    dte: int | None = None
    provider_symbol: str | None = None
    liquidity_score: float | None = None
    provider: str = "mock"
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    market_status: str = "unknown"
    is_stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class OptionContractSnapshot(BaseModel):
    symbol: str
    option_symbol: str
    contract: OptionContractSchema | None = None
    timestamp: datetime
    provider: str
    market_status: str = "unknown"
    is_stale: bool = False
    warnings: list[str] = Field(default_factory=list)
