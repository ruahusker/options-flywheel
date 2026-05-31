from __future__ import annotations

from datetime import date

from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.portfolio import CashPosition, Holding, OptionPosition, PortfolioSnapshot
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


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
templates.env.filters["pct_points"] = pct_points
templates.env.filters["num"] = num
templates.env.globals["base_path"] = settings.base_path


def latest_snapshot(db: Session) -> PortfolioSnapshot | None:
    return db.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.created_at))).scalars().first()


def snapshot_parts(db: Session, snapshot: PortfolioSnapshot):
    holdings = db.execute(select(Holding).where(Holding.snapshot_id == snapshot.id)).scalars().all()
    options = db.execute(select(OptionPosition).where(OptionPosition.snapshot_id == snapshot.id)).scalars().all()
    cash = db.execute(select(CashPosition).where(CashPosition.snapshot_id == snapshot.id)).scalars().all()
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
