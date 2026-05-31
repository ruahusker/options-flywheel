from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import latest_snapshot, snapshot_parts, templates
from app.services.account_rollup import build_account_roll_recommendations
from app.services.historical_backtest import build_historical_readiness
from app.services.market_data import get_provider
from app.services.risk_engine import calculate_dashboard_metrics
from app.services.roll_decision import action_label, build_roll_decision_rows


router = APIRouter(prefix="/roll", tags=["roll"])


@router.get("")
def roll_page(request: Request, db: Session = Depends(get_db)):
    snapshot = latest_snapshot(db)
    warnings: list[str] = []
    if snapshot is None:
        return templates.TemplateResponse(request, "roll.html", {"rows": [], "readiness": None, "warnings": ["Upload positions first."]})

    holdings, options, cash_positions = snapshot_parts(db, snapshot)
    metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
    provider = get_provider()
    rows, roll_warnings = build_roll_decision_rows(metrics, options, provider, db)
    warnings.extend(roll_warnings)
    account_rows, account_warnings = build_account_roll_recommendations(db, rows, holdings, options)
    warnings.extend(account_warnings)

    readiness = build_historical_readiness(db)
    return templates.TemplateResponse(
        request,
        "roll.html",
        {
            "rows": rows,
            "account_rows": account_rows,
            "readiness": readiness,
            "warnings": warnings,
            "action_label": action_label,
        },
    )
