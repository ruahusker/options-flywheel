from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import latest_snapshot, snapshot_parts, templates
from app.services.monte_carlo import run_monte_carlo
from app.services.risk_engine import calculate_dashboard_metrics


router = APIRouter(prefix="/monte-carlo", tags=["monte-carlo"])


@router.get("")
def monte_carlo_page(request: Request, db: Session = Depends(get_db)):
    return _render(request, db, paths=5000, years=5, optioned_pct=None)


@router.post("")
def monte_carlo_post(
    request: Request,
    paths: int = Form(5000),
    years: int = Form(5),
    ibit_vol: float = Form(0.65),
    asst_vol: float = Form(0.85),
    drift: float = Form(0.08),
    correlation: float = Form(0.55),
    annual_premium_rate: float = Form(0.12),
    premium_variability: float = Form(0.35),
    optioned_pct: float = Form(0.35),
    db: Session = Depends(get_db),
):
    return _render(request, db, paths, years, optioned_pct, ibit_vol, asst_vol, drift, correlation, annual_premium_rate, premium_variability)


def _render(
    request: Request,
    db: Session,
    paths: int,
    years: int,
    optioned_pct: float | None,
    ibit_vol: float = 0.65,
    asst_vol: float = 0.85,
    drift: float = 0.08,
    correlation: float = 0.55,
    annual_premium_rate: float = 0.12,
    premium_variability: float = 0.35,
):
    snapshot = latest_snapshot(db)
    warnings: list[str] = []
    result = None
    if snapshot:
        holdings, options, cash = snapshot_parts(db, snapshot)
        metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash)
        if optioned_pct is None:
            optioned_pct = _actual_optioned_pct(metrics)
        risky_value = metrics.values_by_symbol.get("IBIT", 0.0) + metrics.values_by_symbol.get("ASST", 0.0)
        other_assets_value = metrics.true_strategy_value - risky_value - metrics.sata_value
        result = run_monte_carlo(
            starting_ibit_value=metrics.values_by_symbol.get("IBIT", 0.0),
            starting_asst_value=metrics.values_by_symbol.get("ASST", 0.0),
            other_assets_value=other_assets_value,
            sata_starting_value=metrics.sata_value,
            years=years,
            paths=paths,
            ibit_vol=ibit_vol,
            asst_vol=asst_vol,
            drift=drift,
            correlation=correlation,
            annual_premium_rate=annual_premium_rate,
            premium_variability=premium_variability,
            optioned_pct=optioned_pct,
        )
    else:
        optioned_pct = optioned_pct if optioned_pct is not None else 0.35
        warnings.append("Upload positions before running Monte Carlo.")
    return templates.TemplateResponse(
        request,
        "monte_carlo.html",
        {
            "result": result,
            "warnings": warnings,
            "paths": paths,
            "years": years,
            "optioned_pct": optioned_pct,
            "ibit_vol": ibit_vol,
            "asst_vol": asst_vol,
            "drift": drift,
            "correlation": correlation,
            "annual_premium_rate": annual_premium_rate,
            "premium_variability": premium_variability,
        },
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
