from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import templates
from app.services import precompute
from app.services.market_data import get_provider
from app.services.strategy_optimizer import OptimizerSettings


# AI explanations now live in one place — the This Week cockpit's "Explain & Ask" assistant
# (app/routers/week.py). The optimizer is the deterministic ranking surface.


router = APIRouter(prefix="/optimizer", tags=["optimizer"])


@router.get("")
def optimizer_page(request: Request, db: Session = Depends(get_db)):
    # Default settings view comes from the precomputed cache.
    payload = precompute.load_or_build("optimizer", db)
    return templates.TemplateResponse(request, "optimizer.html", payload)


@router.post("")
def run_optimizer(
    request: Request,
    optioned_pct: float = Form(0.35),
    min_untouched_pct: float = Form(0.50),
    call_delta_min: float = Form(0.25),
    call_delta_max: float = Form(0.40),
    put_delta_min: float = Form(0.35),
    put_delta_max: float = Form(0.50),
    min_weekly_premium: float = Form(25.0),
    objective: str = Form("balanced"),
    db: Session = Depends(get_db),
):
    settings = _settings_from_form(
        optioned_pct,
        min_untouched_pct,
        call_delta_min,
        call_delta_max,
        put_delta_min,
        put_delta_max,
        min_weekly_premium,
        objective,
    )
    # Custom run: compute live against cached market data (CachedProvider) — fast, no network.
    payload = precompute.build_optimizer(db, get_provider(), settings)
    return templates.TemplateResponse(request, "optimizer.html", payload)


def _settings_from_form(
    optioned_pct: float,
    min_untouched_pct: float,
    call_delta_min: float,
    call_delta_max: float,
    put_delta_min: float,
    put_delta_max: float,
    min_weekly_premium: float,
    objective: str,
) -> OptimizerSettings:
    return OptimizerSettings(
        optioned_pct=optioned_pct,
        min_untouched_pct=min_untouched_pct,
        call_delta_min=call_delta_min,
        call_delta_max=call_delta_max,
        put_delta_min=put_delta_min,
        put_delta_max=put_delta_max,
        min_weekly_premium=min_weekly_premium,
        objective=objective,
    )
