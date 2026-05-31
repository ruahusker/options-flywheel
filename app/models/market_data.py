from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, Index, Integer, LargeBinary, String, Text, UniqueConstraint

from app.database import Base


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(32), nullable=False, index=True)
    date_time = Column(DateTime, nullable=False, index=True)
    interval = Column(String(20), nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer)
    provider = Column(String(50), nullable=False)


class IndicatorSnapshot(Base):
    __tablename__ = "indicator_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(32), nullable=False, index=True)
    calculated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    price = Column(Float)
    sma_5 = Column(Float)
    sma_10 = Column(Float)
    sma_20 = Column(Float)
    sma_50 = Column(Float)
    ema_8 = Column(Float)
    ema_21 = Column(Float)
    rsi_14 = Column(Float)
    macd_line = Column(Float)
    macd_signal = Column(Float)
    macd_histogram = Column(Float)
    bollinger_upper = Column(Float)
    bollinger_middle = Column(Float)
    bollinger_lower = Column(Float)
    atr_14 = Column(Float)
    realized_vol_10 = Column(Float)
    realized_vol_20 = Column(Float)
    realized_vol_60 = Column(Float)
    price_vs_20d_high = Column(Float)
    price_vs_20d_low = Column(Float)
    trend_state = Column(String(50))
    recommendation_bias = Column(String(100))


class HistoricalOptionContract(Base):
    __tablename__ = "historical_option_contracts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_symbol", name="uq_historical_option_contract_provider_symbol"),
        Index("ix_historical_option_contract_lookup", "provider", "underlying", "expiration", "option_type"),
    )

    id = Column(Integer, primary_key=True)
    provider = Column(String(50), nullable=False)
    provider_symbol = Column(String(128), nullable=False)
    underlying = Column(String(32), nullable=False, index=True)
    expiration = Column(Date, nullable=False, index=True)
    option_type = Column(String(10), nullable=False, index=True)
    strike = Column(Float, nullable=False, index=True)
    shares_per_contract = Column(Float)
    exercise_style = Column(String(32))
    first_seen_as_of = Column(Date)
    last_seen_as_of = Column(Date)
    bars_fetched_at = Column(DateTime)
    bars_fetched_interval = Column(String(20))
    bars_fetched_through = Column(Date)
    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class OptionPriceBar(Base):
    __tablename__ = "option_price_bars"
    __table_args__ = (
        UniqueConstraint("provider", "option_symbol", "date_time", "interval", name="uq_option_price_bar_provider_symbol_time_interval"),
        Index("ix_option_price_bar_lookup", "provider", "underlying", "expiration", "option_type", "date_time"),
    )

    id = Column(Integer, primary_key=True)
    provider = Column(String(50), nullable=False)
    option_symbol = Column(String(128), nullable=False, index=True)
    underlying = Column(String(32), nullable=False, index=True)
    expiration = Column(Date, nullable=False, index=True)
    option_type = Column(String(10), nullable=False, index=True)
    strike = Column(Float, nullable=False, index=True)
    date_time = Column(DateTime, nullable=False, index=True)
    interval = Column(String(20), nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer)
    vwap = Column(Float)
    transactions = Column(Integer)
    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MarketDataCache(Base):
    """Latest live-provider payload (quote/history/expirations/chain/status), refreshed on a schedule.

    The web path reads these rows through CachedProvider instead of calling the live provider, so
    page loads make no external calls. `kind` selects the payload type; `key` is the expiration ISO
    date for chains and "" otherwise. `payload_json` is the pydantic schema serialized with
    model_dump_json (a JSON array for list payloads like history/chain/expirations).
    """

    __tablename__ = "market_data_cache"
    __table_args__ = (
        UniqueConstraint("symbol", "kind", "key", name="uq_market_data_cache_symbol_kind_key"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(32), nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    key = Column(String(32), nullable=False, default="")
    payload_json = Column(Text, nullable=False)
    refreshed_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PrecomputeCache(Base):
    """Precomputed render payload for a page, produced by the scheduled job (or an upload rebuild).

    `payload` is a pickled blob of the page's computed objects so routers can render with zero
    computation. `snapshot_id` is the portfolio snapshot the payload was built against (0 for
    portfolio-independent pages like indicators/optimizer/live-data). `market_refreshed_at` records
    when the underlying market data was pulled, for the "Data as of …" indicator.
    """

    __tablename__ = "precompute_cache"
    __table_args__ = (
        UniqueConstraint("page", "snapshot_id", name="uq_precompute_cache_page_snapshot"),
    )

    id = Column(Integer, primary_key=True)
    page = Column(String(40), nullable=False, index=True)
    snapshot_id = Column(Integer, nullable=False, default=0)
    payload = Column(LargeBinary, nullable=False)
    refreshed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    market_refreshed_at = Column(DateTime)
