"""Realized performance tracking: strategy net worth (incl. premiums/SATA) vs buy-and-hold counterfactual.

Uses the sequence of imported PortfolioSnapshots as checkpoints. For a chosen period,
computes a B&H benchmark as: the IBIT/ASST share counts from the *start* of the period,
marked to market at the prices prevailing at each later checkpoint, plus the sidecar value
(SATA + cash effects) from the start (i.e. without the benefit of option premiums collected
during the period). This highlights the actual economic difference attributable to the
covered-call / flywheel activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from app.models.journal import TradeJournalEntry
from app.models.portfolio import PortfolioSnapshot
from app.routers.common import snapshot_parts
from app.services.risk_engine import DashboardMetrics, calculate_dashboard_metrics


@dataclass
class PerformanceResult:
    checkpoints: list[dict[str, Any]]  # serializable for template + chart
    summary: dict[str, Any]
    journal_premiums: dict[str, Any]
    warnings: list[str]


def list_available_snapshots(db: Session) -> list[dict[str, Any]]:
    """Return lightweight list of snapshots for UI selectors (oldest first so start/end selects read naturally)."""
    snaps = (
        db.execute(
            select(PortfolioSnapshot).order_by(PortfolioSnapshot.created_at)
        )
        .scalars()
        .all()
    )
    out = []
    for s in snaps:
        out.append(
            {
                "id": s.id,
                "created_at": s.created_at,
                "source_filename": s.source_filename,
                "total_value": s.total_value,
            }
        )
    return out


def _price_for_symbol(holdings: list[Any], symbol: str) -> float:
    for h in holdings:
        if (h.symbol or "").upper() == symbol:
            return float(h.last_price or 0.0)
    return 0.0


def _shares_for_symbol(holdings: list[Any], symbol: str) -> float:
    total = 0.0
    for h in holdings:
        if (h.symbol or "").upper() == symbol:
            total += float(h.quantity or 0.0)
    return total


def _short_calls(options: list[Any]) -> int:
    return sum(o.contracts or 0 for o in options if o.side == "short" and o.option_type == "call")


def _short_puts(options: list[Any]) -> int:
    return sum(o.contracts or 0 for o in options if o.side == "short" and o.option_type == "put")


def compute_performance(
    db: Session,
    start_id: int | None = None,
    end_id: int | None = None,
) -> PerformanceResult:
    """Compute strategy vs B&H over the selected (or all) snapshot range.

    B&H uses fixed shares from the chosen start snapshot, marked using the prices
    recorded in each checkpoint's holdings. Sidecar (non-risky + initial effects)
    is held constant from the start so that premiums collected during the period
    explain much of the diff.
    """
    warnings: list[str] = []

    # Load ordered snapshots (oldest -> newest) for the full history first
    all_snaps = (
        db.execute(select(PortfolioSnapshot).order_by(PortfolioSnapshot.created_at))
        .scalars()
        .all()
    )
    if not all_snaps:
        return PerformanceResult(
            checkpoints=[],
            summary={"error": "No portfolio snapshots imported yet."},
            journal_premiums={"net_option_premium": 0.0, "count": 0},
            warnings=["Upload positions (or history) to build snapshots before tracking performance."],
        )

    # Determine the slice
    snap_ids = [s.id for s in all_snaps]
    if start_id is not None and start_id in snap_ids:
        start_idx = snap_ids.index(start_id)
    else:
        start_idx = 0
    if end_id is not None and end_id in snap_ids:
        end_idx = snap_ids.index(end_id)
    else:
        end_idx = len(all_snaps) - 1

    if end_idx < start_idx:
        end_idx = start_idx

    period_snaps = all_snaps[start_idx : end_idx + 1]

    checkpoints: list[dict[str, Any]] = []
    base_metrics: DashboardMetrics | None = None
    base_risky_value = 0.0
    base_sidecar = 0.0
    base_ibit_shares = 0.0
    base_asst_shares = 0.0

    for idx, snap in enumerate(period_snaps):
        holdings, options, cash = snapshot_parts(db, snap)
        metrics = calculate_dashboard_metrics(snap, holdings, options, cash)

        ibit_price = _price_for_symbol(holdings, "IBIT")
        asst_price = _price_for_symbol(holdings, "ASST")
        ibit_shares = metrics.shares_by_symbol.get("IBIT", 0.0)
        asst_shares = metrics.shares_by_symbol.get("ASST", 0.0)
        short_calls = _short_calls(options)
        short_puts = _short_puts(options)

        cp = {
            "id": snap.id,
            "created_at": snap.created_at,
            "source_filename": snap.source_filename,
            "total_value": snap.total_value,
            "strategy_value": metrics.true_strategy_value,
            "sata_value": metrics.sata_value,
            "ibit_shares": ibit_shares,
            "asst_shares": asst_shares,
            "ibit_price": ibit_price,
            "asst_price": asst_price,
            "short_calls": short_calls,
            "short_puts": short_puts,
            "net_sidecar_value": metrics.net_sidecar_value,
        }

        # On first (start) of the chosen period, capture the B&H base
        if idx == 0:
            base_metrics = metrics
            base_ibit_shares = ibit_shares
            base_asst_shares = asst_shares
            risky_value = (
                metrics.values_by_symbol.get("IBIT", 0.0) + metrics.values_by_symbol.get("ASST", 0.0)
            )
            base_risky_value = risky_value
            # Sidecar at start: everything in true_strategy_value except the current risky long value.
            # This keeps initial option MTM effects, initial SATA, cash, etc.
            base_sidecar = metrics.true_strategy_value - risky_value

        # B&H at this checkpoint's prices (fixed base shares + fixed start sidecar, no options)
        bnh_risky = (base_ibit_shares * ibit_price) + (base_asst_shares * asst_price)
        bnh_value = bnh_risky + base_sidecar
        strategy_val = cp["strategy_value"]
        diff = strategy_val - bnh_value

        checkpoints.append(
            {
                "id": cp["id"],
                "date": cp["created_at"].isoformat(),
                "date_label": cp["created_at"].strftime("%Y-%m-%d %H:%M"),
                "source": cp["source_filename"] or "",
                "strategy_value": round(strategy_val, 2),
                "bnh_value": round(bnh_value, 2),
                "diff": round(diff, 2),
                "sata_value": round(cp["sata_value"], 2),
                "ibit_shares": round(ibit_shares, 2),
                "asst_shares": round(asst_shares, 2),
                "ibit_price": round(ibit_price, 4),
                "asst_price": round(asst_price, 4),
                "short_calls": short_calls,
                "short_puts": short_puts,
                "net_sidecar": round(cp["net_sidecar_value"], 2),
            }
        )

    start_cp = checkpoints[0] if checkpoints else None
    end_cp = checkpoints[-1] if checkpoints else None

    # Overall summary for the period
    if start_cp and end_cp:
        strategy_pnl = end_cp["strategy_value"] - start_cp["strategy_value"]
        strategy_pct = (strategy_pnl / start_cp["strategy_value"] * 100.0) if start_cp["strategy_value"] else 0.0
        bnh_pnl = end_cp["bnh_value"] - start_cp["bnh_value"]
        outperformance = end_cp["diff"] - start_cp["diff"]  # how much the gap widened in strategy's favor
        summary = {
            "start_date": start_cp["date_label"],
            "end_date": end_cp["date_label"],
            "start_strategy_value": start_cp["strategy_value"],
            "end_strategy_value": end_cp["strategy_value"],
            "strategy_pnl": round(strategy_pnl, 2),
            "strategy_pct": round(strategy_pct, 2),
            "end_bnh_value": end_cp["bnh_value"],
            "bnh_pnl": round(bnh_pnl, 2),
            "outperformance_vs_bnh": round(outperformance, 2),
            "start_diff": start_cp["diff"],
            "end_diff": end_cp["diff"],
            "base_ibit_shares": round(base_ibit_shares, 2),
            "base_asst_shares": round(base_asst_shares, 2),
            "note": "B&H holds the starting IBIT/ASST share counts (from the period start snapshot) at the prices seen in each checkpoint, plus the starting sidecar value (SATA/cash/option effects) with no additional premiums. New capital added after the start is not included in this B&H figure.",
        }
    else:
        summary = {"error": "Insufficient snapshots in selected range."}

    # Journal premiums in the date window
    if start_cp and end_cp:
        start_dt = datetime.fromisoformat(start_cp["date"])
        end_dt = datetime.fromisoformat(end_cp["date"])
        prem_rows = db.execute(
            select(
                func.count(TradeJournalEntry.id),
                func.coalesce(func.sum(TradeJournalEntry.credit_debit), 0.0),
            ).where(
                and_(
                    TradeJournalEntry.contracts > 0,
                    TradeJournalEntry.created_at >= start_dt,
                    TradeJournalEntry.created_at <= end_dt,
                )
            )
        ).one()
        journal_premiums = {
            "count": int(prem_rows[0] or 0),
            "net_credit_debit": round(float(prem_rows[1] or 0.0), 2),
            "note": "Sum of credit/debit on option trades (positive = premiums received from sells; negative = cost of buy-to-closes). This is a primary driver of strategy vs B&H difference.",
        }
    else:
        journal_premiums = {"count": 0, "net_credit_debit": 0.0, "note": ""}

    # If we have very few checkpoints, add a warning
    if len(checkpoints) < 2:
        warnings.append("Only one snapshot in the selected range. Upload more historical positions to see changes over time.")

    return PerformanceResult(
        checkpoints=checkpoints,
        summary=summary,
        journal_premiums=journal_premiums,
        warnings=warnings,
    )
