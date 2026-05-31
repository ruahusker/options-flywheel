from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import get_sata_settings, latest_snapshot, snapshot_parts, templates
from app.services.account_rollup import build_account_roll_recommendations
from app.services.market_data import get_provider
from app.services.premium_allocation import build_account_premium_allocations, build_premium_allocation
from app.services.risk_engine import calculate_dashboard_metrics
from app.services.roll_decision import action_label, build_roll_decision_rows
from app.services.sata_projection import project_multiple_horizons


router = APIRouter()


@router.get("/portfolio")
def dashboard(request: Request, db: Session = Depends(get_db)):
    snapshot = latest_snapshot(db)
    if not snapshot:
        return templates.TemplateResponse(
            request,
            "portfolio.html",
            {"snapshot": None, "warnings": ["Upload a Fidelity positions CSV to populate the portfolio detail."]},
        )

    holdings, options, cash_positions = snapshot_parts(db, snapshot)
    metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
    sata_settings = get_sata_settings(db)
    provider = get_provider()
    provider_warnings: list[str] = []
    roll_rows, roll_warnings = build_roll_decision_rows(metrics, options, provider, db)
    provider_warnings.extend(roll_warnings)
    account_rows, account_warnings = build_account_roll_recommendations(db, roll_rows, holdings, options)
    provider_warnings.extend(account_warnings)

    weekly_premium = sum(row.recurring_weekly_premium for row in roll_rows)
    metrics.estimated_weekly_premium = weekly_premium
    metrics.estimated_annual_premium = weekly_premium * 52
    premium_allocation = build_premium_allocation(metrics, roll_rows)
    account_premium_allocations = build_account_premium_allocations(premium_allocation, account_rows)
    projections = project_multiple_horizons(
        initial_value=metrics.sata_value,
        weekly_contribution=premium_allocation.amount_for("SATA"),
        annual_rate=sata_settings.annual_dividend_rate,
        drip_enabled=sata_settings.drip_enabled,
        assumed_price=sata_settings.assumed_price,
        compounding_mode=sata_settings.compounding_mode,
        tax_rate=getattr(sata_settings, "tax_rate", 0.0) or 0.0,
    )

    return templates.TemplateResponse(
        request,
        "portfolio.html",
        {
            "snapshot": snapshot,
            "metrics": metrics,
            "roll_rows": roll_rows,
            "premium_allocation": premium_allocation,
            "account_premium_allocations": account_premium_allocations,
            "action_label": action_label,
            "projections": projections,
            "warnings": metrics.warnings + provider_warnings,
        },
    )
