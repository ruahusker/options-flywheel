from __future__ import annotations

from datetime import date

from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.portfolio import CashPosition, Holding, OptionPosition, PortfolioSnapshot, is_position_snapshot
from app.models.settings import SATASettings


templates = Jinja2Templates(directory="app/templates")


def money(value) -> str:
    if value is None:
        return "-"
    return f"${float(value):,.2f}"


def pct(value) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def pct_points(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f}%"


def num(value, places: int = 2) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{places}f}"


def data_asof_iso() -> str:
    """ISO-8601 UTC timestamp of the most recent market-data refresh (or "" if none yet).

    Rendered into a data attribute in the topbar; app.js formats it to local time and a live
    'N min ago', and flags staleness client-side so the label stays current between requests.
    """
    from app.services.market_data.cached_provider import latest_refresh_at

    ts = latest_refresh_at()
    return (ts.isoformat() + "Z") if ts else ""


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
templates.env.filters["pct_points"] = pct_points
templates.env.filters["num"] = num
templates.env.globals["base_path"] = settings.base_path
templates.env.globals["data_asof_iso"] = data_asof_iso


def latest_snapshot(db: Session) -> PortfolioSnapshot | None:
    """Newest positions-export snapshot. History-derived snapshots carry stale transaction
    prices and must not become the dashboard's view of the portfolio; they are only a fallback
    when no positions export has ever been imported."""
    snapshots = db.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.created_at))).scalars().all()
    for snapshot in snapshots:
        if is_position_snapshot(snapshot):
            return snapshot
    return snapshots[0] if snapshots else None


def snapshot_parts(db: Session, snapshot: PortfolioSnapshot):
    holdings = db.execute(select(Holding).where(Holding.snapshot_id == snapshot.id)).scalars().all()
    options = db.execute(select(OptionPosition).where(OptionPosition.snapshot_id == snapshot.id)).scalars().all()
    cash = db.execute(select(CashPosition).where(CashPosition.snapshot_id == snapshot.id)).scalars().all()
    # An option whose expiration predates the snapshot was already settled (expired/assigned)
    # when the snapshot was taken — it is not an open position. History-derived imports can
    # leave such legs behind; filtering here protects every consumer (dashboard, roll, week,
    # performance) without distorting historical checkpoints, which compare against their own date.
    snapshot_date = snapshot.created_at.date() if snapshot.created_at else None
    if snapshot_date is not None:
        options = [o for o in options if o.expiration is None or o.expiration >= snapshot_date]
    return holdings, options, cash


def get_sata_settings(db: Session) -> SATASettings:
    settings = db.execute(select(SATASettings).order_by(desc(SATASettings.id))).scalars().first()
    if settings:
        return settings
    settings = SATASettings()
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)
