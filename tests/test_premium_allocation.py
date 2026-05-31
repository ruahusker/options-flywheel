from types import SimpleNamespace

from app.services.account_rollup import AccountKey
from app.services.premium_allocation import build_account_premium_allocations, build_premium_allocation


def test_premium_allocation_keeps_sata_anchor_when_sata_is_small():
    metrics = SimpleNamespace(
        true_strategy_value=200_000,
        total_account_value=200_000,
        sata_value=2_000,
        cash_value=1_000,
        pending_activity=0,
    )
    rows = [
        _row("IBIT", 420, rsi=31, trend="bearish", current_covered_pct=97, coverage_pct=0.25),
        _row("ASST", 440, rsi=59, trend="neutral/chop", current_covered_pct=98, coverage_pct=0.35),
    ]

    plan = build_premium_allocation(metrics, rows)

    assert plan.amount_for("SATA") == 688
    assert round(plan.amount_for("IBIT"), 2) == 122.86
    assert round(plan.amount_for("ASST"), 2) == 49.14
    assert plan.legs[0].destination == "SATA"


def test_premium_allocation_uses_sata_only_when_underlyings_are_extended():
    metrics = SimpleNamespace(
        true_strategy_value=200_000,
        total_account_value=200_000,
        sata_value=50_000,
        cash_value=1_000,
        pending_activity=0,
    )
    rows = [
        _row("IBIT", 400, rsi=76, trend="bullish trend", current_covered_pct=20, coverage_pct=0.50),
        _row("ASST", 300, rsi=72, trend="weakening", current_covered_pct=20, coverage_pct=0.50),
    ]

    plan = build_premium_allocation(metrics, rows)

    assert plan.amount_for("SATA") == 700
    assert plan.amount_for("IBIT") == 0
    assert plan.amount_for("ASST") == 0


def test_account_premium_allocations_apply_portfolio_split_by_account():
    metrics = SimpleNamespace(
        true_strategy_value=200_000,
        total_account_value=200_000,
        sata_value=2_000,
        cash_value=1_000,
        pending_activity=0,
    )
    rows = [
        _row("IBIT", 420, rsi=31, trend="bearish", current_covered_pct=97, coverage_pct=0.25),
        _row("ASST", 440, rsi=59, trend="neutral/chop", current_covered_pct=98, coverage_pct=0.35),
    ]
    plan = build_premium_allocation(metrics, rows)
    steve = AccountKey("244172640")
    nicole = AccountKey("241405056")
    account_rows = [
        SimpleNamespace(account=steve, target_credit=200, basis="latest account-level positions"),
        SimpleNamespace(account=nicole, target_credit=660, basis="latest account-level positions"),
    ]

    account_plan = build_account_premium_allocations(plan, account_rows)

    assert [row.account for row in account_plan] == [nicole, steve]
    assert round(sum(row.weekly_premium for row in account_plan), 2) == 860
    assert round(sum(row.amount_for("SATA") for row in account_plan), 2) == 688
    assert round(account_plan[1].amount_for("IBIT"), 2) == 28.57
    assert round(account_plan[1].amount_for("ASST"), 2) == 11.43


def _row(symbol: str, credit: float, *, rsi: float, trend: str, current_covered_pct: float, coverage_pct: float):
    return SimpleNamespace(
        symbol=symbol,
        rsi_14=rsi,
        trend_state=trend,
        current_covered_pct=current_covered_pct,
        posture=SimpleNamespace(coverage_pct=coverage_pct),
        selected=SimpleNamespace(action="sell call", expected_credit=credit),
        recurring_weekly_premium=credit,
    )
