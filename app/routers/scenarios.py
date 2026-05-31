from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import get_sata_settings, latest_snapshot, snapshot_parts, templates
from app.services.risk_engine import calculate_dashboard_metrics
from app.services.sata_projection import project_multiple_horizons
from app.services.scenario_analyzer import analyze_default_scenarios


router = APIRouter(prefix="/scenarios", tags=["scenarios"])


@router.get("")
def scenarios_page(request: Request, db: Session = Depends(get_db)):
    return _render(request, db, None, 0.0)


@router.post("")
def scenarios_post(
    request: Request,
    optioned_pct: float = Form(0.35),
    expected_weekly_premium: float = Form(0.0),
    db: Session = Depends(get_db),
):
    return _render(request, db, optioned_pct, expected_weekly_premium)


def _render(request: Request, db: Session, optioned_pct: float | None, expected_weekly_premium: float):
    snapshot = latest_snapshot(db)
    warnings: list[str] = []
    results = []
    if snapshot:
        holdings, options, cash = snapshot_parts(db, snapshot)
        metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash)
        if optioned_pct is None:
            optioned_pct = _actual_optioned_pct(metrics)
        settings = get_sata_settings(db)
        projections = project_multiple_horizons(
            metrics.sata_value,
            expected_weekly_premium,
            settings.annual_dividend_rate,
            settings.drip_enabled,
            settings.assumed_price,
            compounding_mode=settings.compounding_mode,
            tax_rate=getattr(settings, "tax_rate", 0.0) or 0.0,
        )
        sata_by_year = {projection.years: projection.ending_value for projection in projections}
        risky_value = metrics.values_by_symbol.get("IBIT", 0.0) + metrics.values_by_symbol.get("ASST", 0.0)
        other_value = metrics.true_strategy_value - risky_value - metrics.sata_value
        results = analyze_default_scenarios(
            starting_ibit_asst_value=risky_value,
            other_assets_value=other_value,
            optioned_pct=optioned_pct,
            expected_annual_premium=expected_weekly_premium * 52,
            sata_values_by_year=sata_by_year,
            sleeve_effective_delta=0.65,
            sata_starting_value=metrics.sata_value,
        )
    else:
        optioned_pct = optioned_pct if optioned_pct is not None else 0.35
        warnings.append("Upload positions before running scenarios.")
    return templates.TemplateResponse(
        request,
        "scenarios.html",
        {"results": results, "warnings": warnings, "optioned_pct": optioned_pct, "expected_weekly_premium": expected_weekly_premium},
    )


def _actual_optioned_pct(metrics) -> float:
    risky_value = metrics.values_by_symbol.get("IBIT", 0.0) + metrics.values_by_symbol.get("ASST", 0.0)
    if risky_value <= 0:
        return 0.35
    optioned_value = 0.0
    for symbol in ("IBIT", "ASST"):
        shares = metrics.shares_by_symbol.get(symbol, 0.0)
        value = metrics.values_by_symbol.get(symbol, 0.0)
        if shares <= 0 or value <= 0:
            continue
        exposure = metrics.option_exposure.get(symbol, {})
        optioned_shares = min(float(exposure.get("optioned_shares", 0.0)), shares)
        optioned_value += optioned_shares * (value / shares)
    return max(0.0, min(optioned_value / risky_value, 1.0))
