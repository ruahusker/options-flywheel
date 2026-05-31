from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services import precompute
from app.services.market_data import get_provider


router = APIRouter(prefix="/scenarios", tags=["scenarios"])


@router.get("")
def scenarios_page(request: Request, db: Session = Depends(get_db)):
    payload = precompute.load_or_build("scenarios", db)
    return templates.TemplateResponse(request, "scenarios.html", payload)


@router.post("")
def scenarios_post(
    request: Request,
    optioned_pct: float = Form(0.35),
    expected_weekly_premium: float = Form(0.0),
    db: Session = Depends(get_db),
):
    payload = precompute.build_scenarios(db, get_provider(), optioned_pct, expected_weekly_premium)
    return templates.TemplateResponse(request, "scenarios.html", payload)
