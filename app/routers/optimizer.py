from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import latest_snapshot, snapshot_parts, templates
from app.services.indicators import calculate_indicators
from app.services.market_data import get_provider
from app.services.recommendation_engine import generate_recommendation
from app.services.risk_engine import calculate_dashboard_metrics
from app.services.strategy_optimizer import OptimizerSettings


# AI explanations now live in one place — the This Week cockpit's "Explain & Ask" assistant
# (app/routers/week.py). The optimizer is the deterministic ranking surface.


router = APIRouter(prefix="/optimizer", tags=["optimizer"])


@router.get("")
def optimizer_page(request: Request, db: Session = Depends(get_db)):
    return _render_optimizer(request, db, OptimizerSettings())


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
    return _render_optimizer(request, db, settings)


def _render_optimizer(request: Request, db: Session, settings: OptimizerSettings):
    snapshot = latest_snapshot(db)
    warnings: list[str] = []
    recommendations = []
    metrics = None
    if snapshot:
        holdings, options, cash_positions = snapshot_parts(db, snapshot)
        metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
        provider = get_provider()
        for symbol in ("IBIT", "ASST"):
            shares = metrics.shares_by_symbol.get(symbol, 0.0)
            if shares <= 0:
                continue
            try:
                quote = provider.get_quote(symbol)
                expirations = provider.get_option_expirations(symbol)
                chain = provider.get_option_chain(symbol, expirations[0]) if expirations else []
                indicator = calculate_indicators(symbol, provider.get_price_history(symbol, 90, "1d"))
                recommendations.append(
                    generate_recommendation(
                        symbol=symbol,
                        shares=shares,
                        available_cash=metrics.cash_value + metrics.pending_activity,
                        quote=quote,
                        chain=chain,
                        indicators=indicator,
                        settings=settings,
                        existing_short_call_contracts=int(metrics.option_exposure.get(symbol, {}).get("short_calls", 0)),
                    )
                )
            except Exception as exc:
                warnings.append(f"{symbol}: {exc}")
    else:
        warnings.append("Upload positions before running the optimizer.")
    return templates.TemplateResponse(
        request,
        "optimizer.html",
        {"settings": settings, "snapshot": snapshot, "metrics": metrics, "recommendations": recommendations, "warnings": warnings},
    )


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
