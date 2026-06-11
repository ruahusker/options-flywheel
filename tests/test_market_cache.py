from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
from app.services import precompute
from app.services.market_data import get_provider
from app.services.market_data.cached_provider import CachedProvider


def _shared_memory_sessionmaker():
    # StaticPool keeps a single connection so the in-memory DB is shared across sessions (CachedProvider
    # opens its own session, separate from the writer's).
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def test_get_provider_returns_cached_only_when_flag_set(monkeypatch):
    import dataclasses

    import app.services.market_data as md

    def with_flags(use_cache: bool):
        return dataclasses.replace(settings, use_market_cache=use_cache, market_data_provider="mock")

    monkeypatch.setattr(md, "settings", with_flags(True))
    assert md.get_provider().name == "cached"

    monkeypatch.setattr(md, "settings", with_flags(False))
    assert md.get_provider().name == "mock"

    # An explicit name always bypasses the cache, even with the flag on.
    monkeypatch.setattr(md, "settings", with_flags(True))
    assert md.get_provider("mock").name == "mock"


def test_cached_provider_roundtrips_written_market_data(monkeypatch):
    from app.services.market_data.mock_provider import MockProvider
    from app.services.market_refresh import fetch_and_cache
    from app.models.market_data import FocusedOptionSnapshot

    TestSession = _shared_memory_sessionmaker()
    # CachedProvider reads through its module-level SessionLocal; point it at the shared test DB.
    monkeypatch.setattr("app.services.market_data.cached_provider.SessionLocal", TestSession)

    db = TestSession()
    summary = fetch_and_cache(db, MockProvider(Path("sample_data")), symbols=("IBIT",), chain_count=1)
    focused_rows = db.query(FocusedOptionSnapshot).filter(FocusedOptionSnapshot.underlying == "IBIT").all()
    db.close()

    assert summary["symbols"]["IBIT"]["focused_snapshots"] == len(focused_rows)
    assert focused_rows
    assert any(row.delta is not None and row.implied_volatility is not None for row in focused_rows)

    cached = CachedProvider()
    quote = cached.get_quote("IBIT")
    assert quote.symbol == "IBIT"

    bars = cached.get_price_history("IBIT", 30, "1d")
    assert bars and bars[-1].symbol == "IBIT"

    expirations = cached.get_option_expirations("IBIT")
    assert expirations
    chain = cached.get_option_chain("IBIT", expirations[0])
    assert isinstance(chain, list)


def test_reference_symbols_use_smaller_default_chain_cache():
    from app.services.market_refresh import (
        CHAIN_EXPIRATIONS,
        REFERENCE_CHAIN_EXPIRATIONS,
        SYMBOLS,
        _chain_count_for_symbol,
    )

    assert {"SPY", "BSOL", "ETHA"}.issubset(set(SYMBOLS))
    assert _chain_count_for_symbol("IBIT", CHAIN_EXPIRATIONS) == CHAIN_EXPIRATIONS
    assert _chain_count_for_symbol("SPY", CHAIN_EXPIRATIONS) == REFERENCE_CHAIN_EXPIRATIONS
    assert _chain_count_for_symbol("SPY", 1) == 1


def test_focused_option_snapshot_upserts_same_15_minute_bucket():
    from app.models.market_data import FocusedOptionSnapshot
    from app.services.market_data.mock_provider import MockProvider
    from app.services.market_refresh import record_focused_option_snapshots

    TestSession = _shared_memory_sessionmaker()
    db = TestSession()
    provider = MockProvider(Path("sample_data"))
    expiration = provider.get_option_expirations("IBIT")[0]
    chain = provider.get_option_chain("IBIT", expiration)

    first_count = record_focused_option_snapshots(
        db,
        "IBIT",
        chain,
        underlying_price=41.63,
        captured_at=datetime(2026, 6, 1, 10, 2, 0),
        provider_name="tradier",
        market_status="open",
    )
    db.commit()
    second_count = record_focused_option_snapshots(
        db,
        "IBIT",
        chain,
        underlying_price=42.0,
        captured_at=datetime(2026, 6, 1, 10, 14, 0),
        provider_name="tradier",
        market_status="open",
    )
    db.commit()

    assert first_count == second_count
    assert db.query(FocusedOptionSnapshot).count() == first_count
    row = db.query(FocusedOptionSnapshot).first()
    assert row.captured_at == datetime(2026, 6, 1, 10, 0, 0)
    assert row.underlying_price == 42.0
    db.close()


def test_refresh_continues_when_option_expirations_are_unavailable():
    from app.models.market_data import MarketDataCache
    from app.services.market_data.mock_provider import MockProvider
    from app.services.market_refresh import fetch_and_cache

    class NoOptionsProvider(MockProvider):
        def get_option_expirations(self, symbol):
            raise RuntimeError("no options")

    TestSession = _shared_memory_sessionmaker()
    db = TestSession()
    summary = fetch_and_cache(db, NoOptionsProvider(Path("sample_data")), symbols=("IBIT",), chain_count=1)

    assert summary["symbols"]["IBIT"]["bars"] > 0
    assert summary["symbols"]["IBIT"]["expirations"] == 0
    assert summary["symbols"]["IBIT"]["chains_cached"] == 0
    assert summary["symbols"]["IBIT"]["focused_snapshots"] == 0
    assert summary["symbols"]["IBIT"]["warnings"]
    assert db.query(MarketDataCache).filter_by(symbol="IBIT", kind="quote").one_or_none() is not None
    db.close()


def test_cached_provider_missing_data_is_graceful(monkeypatch):
    TestSession = _shared_memory_sessionmaker()
    monkeypatch.setattr("app.services.market_data.cached_provider.SessionLocal", TestSession)

    cached = CachedProvider()
    quote = cached.get_quote("NOPE")
    assert quote.is_stale and quote.price is None and quote.warnings
    assert cached.get_price_history("NOPE", 30, "1d") == []
    assert cached.get_option_expirations("NOPE") == []


def test_precompute_store_load_roundtrip():
    TestSession = _shared_memory_sessionmaker()
    db = TestSession()
    payload = {"rows": [1, 2, 3], "warnings": ["ok"], "nested": {"a": 1.5}}
    ts = datetime(2026, 5, 31, 12, 0, 0)

    precompute.store(db, "roll", 7, payload, ts)
    loaded = precompute.load(db, "roll", 7)
    assert loaded == payload

    # Re-store overwrites (unique on page+snapshot_id).
    precompute.store(db, "roll", 7, {"rows": []}, ts)
    assert precompute.load(db, "roll", 7) == {"rows": []}

    # Missing entry -> None (cold cache).
    assert precompute.load(db, "week", 999) is None
    db.close()
