"""Precompute the render payload for every heavy page on a schedule, so web requests render a
stored snapshot with zero computation and zero network calls.

The scheduled refresh job (`scripts/refresh_market_data.py`) and the upload handler call
`refresh_all`. Routers call `load_or_build`. The same `build_*` functions back both the cached
default view and the interactive POST variants (called directly with custom params), so there is a
single code path per page.

Payloads are pickled. Dataclasses/pydantic pickle cleanly; SQLAlchemy ORM objects do NOT (their
attributes expire when the session closes -> DetachedInstanceError on unpickle), so the few ORM
objects that reach a template (`snapshot`, `sata_settings`) are converted to plain namespaces with
`_detach` before they go into a payload.
"""

from __future__ import annotations

import pickle
from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.models.market_data import PrecomputeCache
from app.routers.common import get_sata_settings, latest_snapshot, snapshot_parts
from app.services.account_rollup import build_account_roll_recommendations
from app.services.historical_backtest import build_historical_readiness
from app.services.indicators import calculate_indicators
from app.services.market_data import get_provider
from app.services.market_data.base import MarketDataProvider
from app.services.market_data.cached_provider import latest_refresh_at
from app.services.monte_carlo import run_monte_carlo
from app.services.premium_allocation import build_account_premium_allocations, build_premium_allocation
from app.services.recommendation_engine import generate_recommendation
from app.services.risk_engine import calculate_dashboard_metrics
from app.services.roll_decision import build_roll_decision_rows
from app.services.sata_projection import project_multiple_horizons
from app.services.scenario_analyzer import analyze_default_scenarios
from app.services.strategy_optimizer import OptimizerSettings


# Pages whose payload does not depend on the uploaded portfolio (stored under snapshot_id 0).
PORTFOLIO_INDEPENDENT = {"indicators", "live_data"}


def _detach(obj):
    """Copy an ORM instance's column values into a picklable SimpleNamespace (templates only read
    column attributes off snapshot/sata_settings). Returns None/obj unchanged for non-ORM input."""
    if obj is None:
        return None
    try:
        cols = {attr.key: getattr(obj, attr.key) for attr in sa_inspect(obj).mapper.column_attrs}
    except Exception:
        return obj
    return SimpleNamespace(**cols)


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


# --------------------------------------------------------------------------------------------------
# Builders — each returns the data-only template context (no request, no callables). Routers re-add
# any callables (action_label, week_verdict) at render time.
# --------------------------------------------------------------------------------------------------


def build_week(db: Session, provider: MarketDataProvider) -> dict:
    snapshot = latest_snapshot(db)
    if not snapshot:
        return {"snapshot": None, "warnings": ["Upload a Fidelity positions CSV to populate This Week."]}
    holdings, options, cash_positions = snapshot_parts(db, snapshot)
    metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
    sata_settings = get_sata_settings(db)
    rows, roll_warnings = build_roll_decision_rows(metrics, options, provider, db)
    account_rows, account_warnings = build_account_roll_recommendations(db, rows, holdings, options)

    weekly_premium = sum(row.recurring_weekly_premium for row in rows)
    metrics.estimated_weekly_premium = weekly_premium
    metrics.estimated_annual_premium = weekly_premium * 52
    premium_allocation = build_premium_allocation(metrics, rows)
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
    return {
        "snapshot": _detach(snapshot),
        "metrics": metrics,
        "rows": rows,
        "premium_allocation": premium_allocation,
        "account_premium_allocations": account_premium_allocations,
        "projections": projections,
        "sata_settings": _detach(sata_settings),
        "warnings": metrics.warnings + roll_warnings + account_warnings,
    }


def build_roll(db: Session, provider: MarketDataProvider) -> dict:
    snapshot = latest_snapshot(db)
    if snapshot is None:
        return {"rows": [], "account_rows": [], "readiness": None, "warnings": ["Upload positions first."]}
    holdings, options, cash_positions = snapshot_parts(db, snapshot)
    metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
    warnings: list[str] = []
    rows, roll_warnings = build_roll_decision_rows(metrics, options, provider, db)
    warnings.extend(roll_warnings)
    account_rows, account_warnings = build_account_roll_recommendations(db, rows, holdings, options)
    warnings.extend(account_warnings)
    readiness = build_historical_readiness(db)
    return {"rows": rows, "account_rows": account_rows, "readiness": readiness, "warnings": warnings}


def build_portfolio(db: Session, provider: MarketDataProvider) -> dict:
    snapshot = latest_snapshot(db)
    if not snapshot:
        return {"snapshot": None, "warnings": ["Upload a Fidelity positions CSV to populate the portfolio detail."]}
    holdings, options, cash_positions = snapshot_parts(db, snapshot)
    metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
    sata_settings = get_sata_settings(db)
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
    return {
        "snapshot": _detach(snapshot),
        "metrics": metrics,
        "roll_rows": roll_rows,
        "premium_allocation": premium_allocation,
        "account_premium_allocations": account_premium_allocations,
        "projections": projections,
        "warnings": metrics.warnings + provider_warnings,
    }


def build_optimizer(db: Session, provider: MarketDataProvider, settings: OptimizerSettings | None = None) -> dict:
    settings = settings or OptimizerSettings()
    snapshot = latest_snapshot(db)
    warnings: list[str] = []
    recommendations = []
    metrics = None
    if snapshot:
        holdings, options, cash_positions = snapshot_parts(db, snapshot)
        metrics = calculate_dashboard_metrics(snapshot, holdings, options, cash_positions)
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
    return {"settings": settings, "snapshot": _detach(snapshot), "metrics": metrics, "recommendations": recommendations, "warnings": warnings}


def build_indicators(db: Session, provider: MarketDataProvider) -> dict:
    results = []
    warnings: list[str] = []
    for symbol in ("IBIT", "ASST"):
        try:
            bars = provider.get_price_history(symbol, 90, "1d")
            results.append(calculate_indicators(symbol, bars))
        except Exception as exc:
            warnings.append(f"{symbol}: {exc}")
    return {"results": results, "warnings": warnings}


def build_scenarios(db: Session, provider: MarketDataProvider, optioned_pct: float | None = None, expected_weekly_premium: float = 0.0) -> dict:
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
    return {"results": results, "warnings": warnings, "optioned_pct": optioned_pct, "expected_weekly_premium": expected_weekly_premium}


def build_monte_carlo(
    db: Session,
    provider: MarketDataProvider,
    paths: int = 5000,
    years: int = 5,
    optioned_pct: float | None = None,
    ibit_vol: float = 0.65,
    asst_vol: float = 0.85,
    drift: float = 0.08,
    correlation: float = 0.55,
    annual_premium_rate: float = 0.12,
    premium_variability: float = 0.35,
) -> dict:
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
    return {
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
    }


def build_live_data(db: Session, provider: MarketDataProvider, symbol: str = "IBIT", expiration: str | None = None) -> dict:
    warnings: list[str] = []
    quote = None
    expirations: list = []
    chain: list = []
    status = provider.get_market_status()
    try:
        quote = provider.get_quote(symbol)
        expirations = provider.get_option_expirations(symbol)
        selected = date.fromisoformat(expiration) if expiration else (expirations[0] if expirations else None)
        if selected:
            chain = provider.get_option_chain(symbol, selected)
    except Exception as exc:
        warnings.append(str(exc))
    return {
        "symbol": symbol.upper(),
        "quote": quote,
        "expirations": expirations,
        "selected_expiration": expiration,
        "chain": chain,
        "status": status,
        "warnings": warnings,
    }


BUILDERS = {
    "week": build_week,
    "roll": build_roll,
    "portfolio": build_portfolio,
    "optimizer": build_optimizer,
    "indicators": build_indicators,
    "scenarios": build_scenarios,
    "monte_carlo": build_monte_carlo,
    "live_data": build_live_data,
}


# --------------------------------------------------------------------------------------------------
# Store / load
# --------------------------------------------------------------------------------------------------


def _snapshot_id(page: str, db: Session) -> int:
    if page in PORTFOLIO_INDEPENDENT:
        return 0
    snapshot = latest_snapshot(db)
    return snapshot.id if snapshot else 0


# Bump whenever a payload dataclass gains fields the templates rely on: old pickles unpickle
# cleanly but without the new attributes, which would 500 the page. A mismatch is a cache miss.
CACHE_SCHEMA_VERSION = 3


def store(db: Session, page: str, snapshot_id: int, payload: dict, market_refreshed_at: datetime | None) -> None:
    blob = pickle.dumps({**payload, "__schema__": CACHE_SCHEMA_VERSION})
    existing = db.query(PrecomputeCache).filter_by(page=page, snapshot_id=snapshot_id).one_or_none()
    if existing is not None:
        existing.payload = blob
        existing.refreshed_at = datetime.utcnow()
        existing.market_refreshed_at = market_refreshed_at
    else:
        db.add(
            PrecomputeCache(
                page=page,
                snapshot_id=snapshot_id,
                payload=blob,
                refreshed_at=datetime.utcnow(),
                market_refreshed_at=market_refreshed_at,
            )
        )
    db.commit()


def load(db: Session, page: str, snapshot_id: int) -> dict | None:
    row = db.query(PrecomputeCache).filter_by(page=page, snapshot_id=snapshot_id).one_or_none()
    if row is None:
        return None
    try:
        payload = pickle.loads(row.payload)
    except Exception:
        # Pickle/version drift -> treat as a cache miss so the caller rebuilds; never a 500.
        return None
    if payload.pop("__schema__", None) != CACHE_SCHEMA_VERSION:
        return None
    return payload


def load_or_build(page: str, db: Session) -> dict:
    """Return the stored payload for a page's default view, or build it live (cold cache) via the
    current provider (CachedProvider when MARKET_DATA_CACHE is on)."""
    payload = load(db, page, _snapshot_id(page, db))
    if payload is not None:
        return payload
    return BUILDERS[page](db, get_provider())


def refresh_all(db: Session, provider: MarketDataProvider, market_refreshed_at: datetime | None = None) -> list[str]:
    """Rebuild and store every page's default payload. Used by the scheduled job and after uploads."""
    market_refreshed_at = market_refreshed_at or latest_refresh_at()
    snapshot = latest_snapshot(db)
    snapshot_id = snapshot.id if snapshot else 0
    errors: list[str] = []
    for page, builder in BUILDERS.items():
        try:
            payload = builder(db, provider)
            store(db, page, 0 if page in PORTFOLIO_INDEPENDENT else snapshot_id, payload, market_refreshed_at)
        except Exception as exc:  # one bad page must not abort the rest
            errors.append(f"{page}: {exc}")
    return errors
