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

    TestSession = _shared_memory_sessionmaker()
    # CachedProvider reads through its module-level SessionLocal; point it at the shared test DB.
    monkeypatch.setattr("app.services.market_data.cached_provider.SessionLocal", TestSession)

    db = TestSession()
    fetch_and_cache(db, MockProvider(Path("sample_data")), symbols=("IBIT",), chain_count=1)
    db.close()

    cached = CachedProvider()
    quote = cached.get_quote("IBIT")
    assert quote.symbol == "IBIT"

    bars = cached.get_price_history("IBIT", 30, "1d")
    assert bars and bars[-1].symbol == "IBIT"

    expirations = cached.get_option_expirations("IBIT")
    assert expirations
    chain = cached.get_option_chain("IBIT", expirations[0])
    assert isinstance(chain, list)


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
