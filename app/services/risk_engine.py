from __future__ import annotations

from dataclasses import dataclass

from app.models.portfolio import CashPosition, Holding, OptionPosition, PortfolioSnapshot
from app.services.options_math import net_sidecar_value, true_strategy_value


@dataclass
class DashboardMetrics:
    total_account_value: float
    long_position_value: float
    sata_value: float
    cash_value: float
    pending_activity: float
    short_option_liability: float
    long_option_value: float
    true_strategy_value: float
    net_sidecar_value: float
    cash_collateral: float
    estimated_weekly_premium: float
    estimated_annual_premium: float
    shares_by_symbol: dict[str, float]
    values_by_symbol: dict[str, float]
    option_exposure: dict[str, dict[str, float]]
    warnings: list[str]


def calculate_dashboard_metrics(
    snapshot: PortfolioSnapshot,
    holdings: list[Holding],
    options: list[OptionPosition],
    cash_positions: list[CashPosition],
) -> DashboardMetrics:
    shares_by_symbol: dict[str, float] = {}
    values_by_symbol: dict[str, float] = {}
    for holding in holdings:
        symbol = holding.symbol.upper()
        shares_by_symbol[symbol] = shares_by_symbol.get(symbol, 0.0) + (holding.quantity or 0.0)
        values_by_symbol[symbol] = values_by_symbol.get(symbol, 0.0) + (holding.current_value or 0.0)

    sata_value = values_by_symbol.get("SATA", 0.0)
    pending_activity = sum(c.current_value or 0.0 for c in cash_positions if c.symbol == "Pending activity")
    cash_value = sum(c.current_value or 0.0 for c in cash_positions if c.symbol != "Pending activity")
    long_position_value = sum(
        h.current_value or 0.0
        for h in holdings
        if h.asset_class not in {"cash", "pending"} and h.symbol.upper() != "SATA"
    )
    short_liability = 0.0
    long_option_value = 0.0
    cash_collateral = 0.0
    exposure: dict[str, dict[str, float]] = {}
    for option in options:
        bucket = exposure.setdefault(
            option.underlying,
            {
                "short_calls": 0,
                "long_calls": 0,
                "short_puts": 0,
                "long_puts": 0,
                "optioned_shares": 0,
                "uncovered_shares": 0,
                "optioned_percentage": 0,
            },
        )
        key = f"{option.side}_{option.option_type}s"
        bucket[key] = bucket.get(key, 0) + option.contracts
        if option.side == "short":
            if option.current_value is not None:
                short_liability += abs(option.current_value)
            elif option.last_price is not None:
                short_liability += abs(option.quantity) * 100 * option.last_price
            if option.option_type == "put":
                cash_collateral += option.contracts * 100 * option.strike
        else:
            long_option_value += option.current_value or ((option.last_price or 0.0) * option.contracts * 100)

    for symbol, bucket in exposure.items():
        shares = shares_by_symbol.get(symbol, 0.0)
        optioned_shares = bucket.get("short_calls", 0) * 100
        bucket["optioned_shares"] = optioned_shares
        bucket["uncovered_shares"] = shares - optioned_shares
        bucket["optioned_percentage"] = optioned_shares / shares if shares else 0.0

    strategy_value = true_strategy_value(
        long_position_value=long_position_value,
        sata_value=sata_value,
        cash_value=cash_value,
        pending_activity=pending_activity,
        short_option_mark_to_market_liability=short_liability,
        long_option_mark_to_market_value=long_option_value,
    )
    sidecar = net_sidecar_value(
        sata_value=sata_value,
        option_premium_cash=0.0,
        cash_reserve=cash_value,
        short_option_mark_to_market_liability=short_liability,
        long_option_mark_to_market_value=long_option_value,
    )
    warnings: list[str] = []
    return DashboardMetrics(
        total_account_value=snapshot.total_value or strategy_value,
        long_position_value=long_position_value,
        sata_value=sata_value,
        cash_value=cash_value,
        pending_activity=pending_activity,
        short_option_liability=short_liability,
        long_option_value=long_option_value,
        true_strategy_value=strategy_value,
        net_sidecar_value=sidecar,
        cash_collateral=cash_collateral,
        estimated_weekly_premium=0.0,
        estimated_annual_premium=0.0,
        shares_by_symbol=shares_by_symbol,
        values_by_symbol=values_by_symbol,
        option_exposure=exposure,
        warnings=warnings,
    )
