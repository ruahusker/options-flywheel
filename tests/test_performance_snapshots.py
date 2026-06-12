from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.portfolio import HISTORY_KIND, POSITIONS_KIND, PortfolioSnapshot, is_position_snapshot
from app.routers.common import latest_snapshot
from app.services.performance import _checkpoint_snapshots


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def snap(session, created_at, filename, notes=None, total=100_000.0):
    s = PortfolioSnapshot(created_at=created_at, source_filename=filename, notes=notes, total_value=total)
    session.add(s)
    session.flush()
    return s


def test_is_position_snapshot_uses_notes_then_filename():
    s = PortfolioSnapshot(notes=HISTORY_KIND, source_filename="whatever.csv")
    assert not is_position_snapshot(s)
    s = PortfolioSnapshot(notes=POSITIONS_KIND, source_filename="Accounts_History (1).csv")
    assert is_position_snapshot(s)
    # Legacy rows without a stored kind fall back to the filename.
    assert not is_position_snapshot(PortfolioSnapshot(source_filename="Accounts_History (2).csv"))
    assert is_position_snapshot(PortfolioSnapshot(source_filename="Portfolio_Positions_Jun-10-2026.csv"))


def test_latest_snapshot_skips_newer_history_import():
    session = make_session()
    positions = snap(session, datetime(2026, 6, 11, 2, 43, 19), "Portfolio_Positions_Jun-10-2026.csv")
    snap(session, datetime(2026, 6, 11, 2, 43, 44), "Accounts_History (11).csv")
    session.commit()
    assert latest_snapshot(session).id == positions.id


def test_latest_snapshot_falls_back_to_history_when_nothing_else():
    session = make_session()
    history = snap(session, datetime(2026, 6, 10), "Accounts_History (1).csv")
    session.commit()
    assert latest_snapshot(session).id == history.id


def test_checkpoint_snapshots_filter_history_sample_and_superseded_uploads():
    session = make_session()
    snap(session, datetime(2026, 5, 30, 1), "portfolio_sample_fidelity.csv")
    real_1 = snap(session, datetime(2026, 5, 30, 16), "Portfolio_Positions_May-30-2026.csv")
    snap(session, datetime(2026, 6, 1, 13, 49), "Portfolio_Positions_Jun-01-2026.csv")  # partial upload
    re_upload = snap(session, datetime(2026, 6, 1, 14, 18), "Portfolio_Positions_Jun-01-2026.csv")
    other_file = snap(session, datetime(2026, 6, 1, 14, 50), "Portfolio_Positions_Jun-01-2026 (1).csv")
    snap(session, datetime(2026, 6, 1, 19), "Accounts_History (2).csv")
    session.commit()

    ids = [s.id for s in _checkpoint_snapshots(session)]
    assert ids == [real_1.id, re_upload.id, other_file.id]
