from __future__ import annotations

from sqlalchemy import func, select
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.database import get_db
from app.models.journal import TradeJournalEntry
from app.routers.common import latest_snapshot, snapshot_parts, templates
from app.services.indicators import calculate_indicators
from app.services.market_data import get_provider
from app.services.recommendation_engine import generate_recommendation
from app.services.risk_engine import calculate_dashboard_metrics
from app.services.sata_projection import project_multiple_horizons
from app.services.situation_brief import answer_situation_question, candidate_dict, indicator_dict, quote_dict
from app.services.strategy_optimizer import OptimizerSettings, optioned_contracts


# The standalone Situation Brief page has been folded into the This Week cockpit, which now hosts the
# single 'Explain & Ask' assistant. This module is retained for its reusable context builder
# (_build_context) and helpers, which week.py imports. The page itself redirects to the cockpit.
router = APIRouter(prefix="/situation", tags=["situation"])


@router.get("")
def situation_page():
    return RedirectResponse(url=f"{app_settings.base_path}/", status_code=307)


def _settings_from_form(
    optioned_pct: float = 0.35,
    min_untouched_pct: float = 0.50,
    call_delta_min: float = 0.20,
    call_delta_max: float = 0.35,
    min_weekly_premium: float = 25.0,
) -> OptimizerSettings:
    return OptimizerSettings(
        optioned_pct=optioned_pct,
        min_untouched_pct=min_untouched_pct,
        call_delta_min=call_delta_min,
        call_delta_max=call_delta_max,
        put_delta_min=0.35,
        put_delta_max=0.50,
        min_weekly_premium=min_weekly_premium,
        objective="high upside with premiums routed to SATA",
    )


def _build_context(db: Session, settings: OptimizerSettings):
    snapshot = latest_snapshot(db)
    warnings: list[str] = []
    if snapshot is None:
        return None, warnings

    holdings, options, cash_positions = snapshot_parts(db, snapshot)
    metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
    actual_optioned_pct = _actual_optioned_pct(metrics)
    provider = get_provider()
    trade_rows = []
    indicators_by_symbol = {}

    for symbol in ("IBIT", "ASST"):
        shares = metrics.shares_by_symbol.get(symbol, 0.0)
        if shares <= 0:
            continue
        try:
            quote = provider.get_quote(symbol)
            expirations = provider.get_option_expirations(symbol)
            chain = provider.get_option_chain(symbol, expirations[0]) if expirations else []
            indicator = calculate_indicators(symbol, provider.get_price_history(symbol, 90, "1d"))
            indicators_by_symbol[symbol] = indicator
            existing_short_calls = int(metrics.option_exposure.get(symbol, {}).get("short_calls", 0))
            current_rec = generate_recommendation(
                symbol=symbol,
                shares=shares,
                available_cash=metrics.cash_value + metrics.pending_activity,
                quote=quote,
                chain=chain,
                indicators=indicator,
                settings=settings,
                existing_short_call_contracts=existing_short_calls,
            )
            target_rec = generate_recommendation(
                symbol=symbol,
                shares=shares,
                available_cash=metrics.cash_value + metrics.pending_activity,
                quote=quote,
                chain=chain,
                indicators=indicator,
                settings=settings,
                existing_short_call_contracts=0,
            )
            sleeve_comparison = _sleeve_comparison(
                symbol=symbol,
                shares=shares,
                available_cash=metrics.cash_value + metrics.pending_activity,
                quote=quote,
                chain=chain,
                indicator=indicator,
                base_settings=settings,
            )
            assignment_cash = _assignment_cash(symbol, options)
            assigned_shares = min(shares, existing_short_calls * 100)
            put_reentry_rec = generate_recommendation(
                symbol=symbol,
                shares=assigned_shares,
                available_cash=assignment_cash,
                quote=quote,
                chain=chain,
                indicators=indicator,
                settings=_put_reentry_settings(settings),
                wheel_in_cash=True,
                existing_short_call_contracts=0,
            )
            trade_rows.append(
                {
                    "symbol": symbol,
                    "shares": shares,
                    "value": metrics.values_by_symbol.get(symbol, 0.0),
                    "quote": quote_dict(quote),
                    "trend_state": indicator.trend_state,
                    "recommendation_bias": indicator.recommendation_bias,
                    "existing_short_calls": existing_short_calls,
                    "target_contracts": optioned_contracts(shares, settings.optioned_pct),
                    "current_recommendation": candidate_dict(current_rec.best),
                    "current_warnings": current_rec.warnings,
                    "target_setup": candidate_dict(target_rec.best),
                    "target_setup_alternatives": [candidate_dict(item) for item in target_rec.alternatives[:3]],
                    "target_warnings": target_rec.warnings,
                    "sleeve_comparison": sleeve_comparison,
                    "assignment_reentry_plan": {
                        "assumes_assignment_of_short_calls": existing_short_calls,
                        "assigned_shares": assigned_shares,
                        "estimated_assignment_cash": assignment_cash,
                        "put_delta_band": f"{settings.put_delta_min:.0%}-{settings.put_delta_max:.0%}",
                        "recommendation": candidate_dict(put_reentry_rec.best),
                        "alternatives": [candidate_dict(item) for item in put_reentry_rec.alternatives[:3]],
                        "warnings": put_reentry_rec.warnings,
                    },
                    "indicator": indicator_dict(indicator),
                }
            )
        except Exception as exc:
            warnings.append(f"{symbol}: {exc}")

    setup_weekly_credit = sum(
        row["target_setup"]["expected_credit"]
        for row in trade_rows
        if row["target_setup"]["action"] != "skip trade"
    )
    projections = project_multiple_horizons(metrics.sata_value, setup_weekly_credit)
    journal_summary = _journal_summary(db)
    context = {
        "snapshot": {
            "id": snapshot.id,
            "created_at": snapshot.created_at,
            "source_filename": snapshot.source_filename,
        },
        "goal": {
            "high_upside_first": True,
            "premiums_to_sata": True,
            "use_delta_as_probability_proxy": True,
            "if_assigned_use_cash_secured_puts_for_reentry": True,
        },
        "strategy": {
            "actual_optioned_pct": actual_optioned_pct,
            "target_optioned_pct": settings.optioned_pct,
            "min_untouched_pct": settings.min_untouched_pct,
            "call_delta_min": settings.call_delta_min,
            "call_delta_max": settings.call_delta_max,
            "put_delta_min": settings.put_delta_min,
            "put_delta_max": settings.put_delta_max,
            "best_chance_delta_band": f"{settings.call_delta_min:.0%}-{settings.call_delta_max:.0%}",
            "put_reentry_delta_band": f"{settings.put_delta_min:.0%}-{settings.put_delta_max:.0%}",
            "min_weekly_premium": settings.min_weekly_premium,
            "sleeves_to_compare": [0.25, 0.35, 0.50],
        },
        "portfolio": {
            "total_account_value": metrics.total_account_value,
            "true_strategy_value": metrics.true_strategy_value,
            "net_sidecar_value": metrics.net_sidecar_value,
            "sata_value": metrics.sata_value,
            "cash_value": metrics.cash_value,
            "pending_activity": metrics.pending_activity,
            "short_option_liability": metrics.short_option_liability,
            "long_option_value": metrics.long_option_value,
            "shares_by_symbol": metrics.shares_by_symbol,
            "values_by_symbol": metrics.values_by_symbol,
            "option_exposure": metrics.option_exposure,
            "open_options": [
                {
                    "underlying": option.underlying,
                    "side": option.side,
                    "option_type": option.option_type,
                    "contracts": option.contracts,
                    "strike": option.strike,
                    "expiration": str(option.expiration),
                    "last_price": option.last_price,
                    "current_value": option.current_value,
                }
                for option in options
            ],
        },
        "journal_summary": journal_summary,
        "trade_rows": trade_rows,
        "sata_projection_if_target_setup_credit_repeats": [
            {
                "years": projection.years,
                "ending_value": projection.ending_value,
                "total_contributions": projection.total_contributions,
                "dividend_growth": projection.dividend_growth,
                "annual_income_at_rate": projection.annual_income_at_rate,
            }
            for projection in projections
        ],
    }
    return context, warnings


def _sleeve_comparison(
    symbol: str,
    shares: float,
    available_cash: float,
    quote,
    chain,
    indicator,
    base_settings: OptimizerSettings,
) -> list[dict]:
    rows = []
    for sleeve in (0.25, 0.35, 0.50):
        sleeve_settings = _settings_from_form(
            optioned_pct=sleeve,
            min_untouched_pct=base_settings.min_untouched_pct,
            call_delta_min=base_settings.call_delta_min,
            call_delta_max=base_settings.call_delta_max,
            min_weekly_premium=base_settings.min_weekly_premium,
        )
        rec = generate_recommendation(
            symbol=symbol,
            shares=shares,
            available_cash=available_cash,
            quote=quote,
            chain=chain,
            indicators=indicator,
            settings=sleeve_settings,
            existing_short_call_contracts=0,
        )
        candidate = candidate_dict(rec.best)
        rows.append(
            {
                "optioned_pct": sleeve,
                "target_contracts": optioned_contracts(shares, sleeve),
                "untouched_pct": 1 - sleeve,
                "recommendation": candidate,
                "warnings": rec.warnings,
            }
        )
    return rows


def _put_reentry_settings(base_settings: OptimizerSettings) -> OptimizerSettings:
    settings = _settings_from_form(
        optioned_pct=base_settings.optioned_pct,
        min_untouched_pct=base_settings.min_untouched_pct,
        call_delta_min=base_settings.call_delta_min,
        call_delta_max=base_settings.call_delta_max,
        min_weekly_premium=base_settings.min_weekly_premium,
    )
    settings.objective = "cash-secured put re-entry after assignment"
    settings.allow_calls = False
    settings.allow_puts = True
    return settings


def _assignment_cash(symbol: str, options) -> float:
    return sum(
        option.contracts * 100 * option.strike
        for option in options
        if option.underlying == symbol and option.option_type == "call" and option.side == "short"
    )


def _actual_optioned_pct(metrics) -> float:
    risky_value = metrics.values_by_symbol.get("IBIT", 0.0) + metrics.values_by_symbol.get("ASST", 0.0)
    if risky_value <= 0:
        return 0.0
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


def _journal_summary(db: Session) -> dict:
    option_rows = select(
        func.count(TradeJournalEntry.id),
        func.coalesce(func.sum(TradeJournalEntry.credit_debit), 0.0),
    ).where(TradeJournalEntry.contracts > 0)
    option_count, net_option_premium = db.execute(option_rows).one()
    sata_contributions = db.execute(
        select(func.coalesce(func.sum(TradeJournalEntry.sata_contribution), 0.0))
    ).scalar_one()
    first_entry = db.execute(select(func.min(TradeJournalEntry.created_at))).scalar_one()
    last_entry = db.execute(select(func.max(TradeJournalEntry.created_at))).scalar_one()
    return {
        "option_entry_count": option_count,
        "net_option_premium": float(net_option_premium or 0.0),
        "sata_contributions": float(sata_contributions or 0.0),
        "first_entry_at": first_entry,
        "last_entry_at": last_entry,
    }
