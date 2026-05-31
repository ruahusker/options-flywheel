from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.market_data import HistoricalOptionContract
from app.services.historical_backtest import build_historical_readiness, focused_backfill_preview
from app.services.massive_backfill import upsert_contracts, upsert_option_bars, upsert_stock_bars
from app.services.massive_client import MassiveBar, MassiveOptionContract


def test_historical_readiness_reports_cache_and_focused_preview():
    session = make_session()
    start = date(2026, 1, 1)
    bars = [
        MassiveBar("IBIT", datetime.combine(start + timedelta(days=index), datetime.min.time()), 100, 102, 99, 100 + index * 0.25)
        for index in range(70)
    ]
    upsert_stock_bars(session, bars, interval="1d")
    contract = MassiveOptionContract("O:IBIT260306C00118000", "IBIT", date(2026, 3, 6), "call", 118.0)
    upsert_contracts(session, [contract])
    stored_contract = session.query(HistoricalOptionContract).one()
    upsert_option_bars(
        session,
        stored_contract,
        [MassiveBar(contract.provider_symbol, datetime(2026, 3, 2), 1.0, 1.2, 0.9, 1.1)],
        interval="1d",
    )
    session.commit()

    readiness = build_historical_readiness(session, underlyings=("IBIT",), start=start, end=date(2026, 3, 10))
    preview = focused_backfill_preview(session, underlyings=("IBIT",), start=start, end=date(2026, 3, 10))

    assert readiness.rows[0].underlying_bars == 70
    assert readiness.rows[0].contracts == 1
    assert readiness.rows[0].option_contracts_with_bars == 1
    assert readiness.regimes[0].bars == 70
    assert preview[0].provider_symbol == contract.provider_symbol


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()
