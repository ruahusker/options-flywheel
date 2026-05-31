from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.common import get_sata_settings, latest_snapshot, snapshot_parts, templates
from app.routers.situation import _build_context as build_ask_context
from app.routers.situation import _settings_from_form as ask_settings
from app.services.ai_rationale import RationaleResult, generate_rationale
from app.services.indicators import calculate_indicators
from app.services.iv_history import iv_rank_for_symbol, record_atm_iv
from app.services.market_data import get_provider
from app.services.premium_allocation import build_premium_allocation
from app.services.recommendation_engine import generate_recommendation
from app.services.risk_engine import calculate_dashboard_metrics
from app.services.roll_decision import (
    action_label,
    build_roll_decision_rows,
    recommend_roll_posture,
    settings_for_posture,
    week_verdict,
)
from app.services.sata_projection import project_multiple_horizons
from app.services.situation_brief import answer_situation_question


router = APIRouter()


@router.get("/")
def week(request: Request, db: Session = Depends(get_db)):
    """Primary weekly cockpit: per-symbol verdict, the supporting edge, the trade, and routing."""
    snapshot = latest_snapshot(db)
    if not snapshot:
        return templates.TemplateResponse(
            request,
            "week.html",
            {"snapshot": None, "warnings": ["Upload a Fidelity positions CSV to populate This Week."]},
        )

    holdings, options, cash_positions = snapshot_parts(db, snapshot)
    metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
    sata_settings = get_sata_settings(db)
    provider = get_provider()
    rows, roll_warnings = build_roll_decision_rows(metrics, options, provider, db)

    weekly_premium = sum(row.recurring_weekly_premium for row in rows)
    metrics.estimated_weekly_premium = weekly_premium
    metrics.estimated_annual_premium = weekly_premium * 52
    premium_allocation = build_premium_allocation(metrics, rows)
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
        "week.html",
        {
            "snapshot": snapshot,
            "metrics": metrics,
            "rows": rows,
            "premium_allocation": premium_allocation,
            "projections": projections,
            "sata_settings": sata_settings,
            "action_label": action_label,
            "week_verdict": week_verdict,
            "warnings": metrics.warnings + roll_warnings,
        },
    )


@router.post("/week/explain")
def explain_decision(request: Request, symbol: str = Form(...), db: Session = Depends(get_db)):
    """Single AI 'Explain this decision' tied to one symbol's weekly trade. Reuses generate_rationale."""
    symbol = symbol.upper()
    snapshot = latest_snapshot(db)
    if not snapshot:
        return _rationale_partial(request, symbol, "Upload positions before explaining a decision.")

    holdings, options, cash_positions = snapshot_parts(db, snapshot)
    metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
    provider = get_provider()
    try:
        quote = provider.get_quote(symbol)
        expirations = provider.get_option_expirations(symbol)
        indicator = calculate_indicators(symbol, provider.get_price_history(symbol, 120, "1d"))
        posture = recommend_roll_posture(indicator)
        expiration = expirations[0] if expirations else None
        chain = provider.get_option_chain(symbol, expiration) if expiration else []
        iv_rank = None
        if chain:
            try:
                atm_iv = record_atm_iv(db, symbol, chain, quote.price)
                iv_rank = iv_rank_for_symbol(db, symbol, atm_iv)
            except Exception:
                iv_rank = None
        rec = generate_recommendation(
            symbol=symbol,
            shares=metrics.shares_by_symbol.get(symbol, 0.0),
            available_cash=metrics.cash_value + metrics.pending_activity,
            quote=quote,
            chain=chain,
            indicators=indicator,
            settings=settings_for_posture(posture),
            existing_short_call_contracts=0,
            iv_rank=iv_rank,
        )
        rationale = generate_rationale(
            symbol=symbol,
            candidate=rec.best,
            alternatives=rec.alternatives,
            indicators=indicator,
            metrics=metrics,
            optioned_pct=posture.coverage_pct,
            objective="weekly flywheel decision",
        )
    except Exception as exc:
        return _rationale_partial(request, symbol, f"Could not explain this decision: {exc}")
    return templates.TemplateResponse(request, "partials/rationale.html", {"rationale": rationale})


@router.post("/week/ask")
def ask_followup(request: Request, question: str = Form(...), db: Session = Depends(get_db)):
    """Single follow-up Q&A box. Reuses the situation context builder + answer_situation_question."""
    context, warnings = build_ask_context(db, ask_settings())
    if context is None:
        answer = None
        warnings.append("Upload positions before asking a follow-up question.")
    else:
        answer = answer_situation_question(context, question)
        warnings.extend(answer.warnings)
    return templates.TemplateResponse(
        request,
        "partials/situation_answer.html",
        {"answer": answer, "warnings": warnings},
    )


def _rationale_partial(request: Request, symbol: str, text: str):
    rationale = RationaleResult(symbol=symbol, provider="local", model=None, text=text, warnings=[], used_ai=False)
    return templates.TemplateResponse(request, "partials/rationale.html", {"rationale": rationale})
