from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScenarioResult:
    name: str
    years: int
    scenario_return: float
    buy_hold_value: float
    strategy_value: float
    sata_sidecar_value: float
    premium_collected: float
    lost_upside: float
    cash_drag_effect: float
    net_vs_buy_hold: float
    gain_vs_starting_capital: float
    upside_capture_ratio: float
    warning: str | None = None


DEFAULT_SCENARIOS = {
    "Bearish": -0.20,
    "Flat": 0.0,
    "Moderate bullish": 0.40,
    "Heavy bullish": 1.50,
}


def analyze_scenario(
    name: str,
    scenario_return: float,
    years: int,
    starting_ibit_asst_value: float,
    other_assets_value: float,
    optioned_pct: float,
    expected_annual_premium: float,
    sata_projected_value: float,
    sleeve_effective_delta: float,
    sata_starting_value: float = 0.0,
    annual_cap: float = 0.35,
) -> ScenarioResult:
    buy_hold_value = starting_ibit_asst_value * (1 + scenario_return) + other_assets_value + sata_starting_value
    total_start = starting_ibit_asst_value + other_assets_value + sata_starting_value
    optioned_value = starting_ibit_asst_value * optioned_pct
    untouched_value = starting_ibit_asst_value * (1 - optioned_pct)
    # A covered-call sleeve OWNS the stock: it participates ~fully (delta ~1) up to the strike,
    # then is flat above it. Capping the return at the call's cap models that truncation directly,
    # so we must NOT additionally scale by sleeve_effective_delta (that double-counted the cap and
    # understated the sleeve). The cap is compounded over the horizon to match the total-return
    # scenarios, not applied linearly.
    # annual_cap approximates how far the weekly-rolled covered strikes let the sleeve run per
    # year before assignment truncates the move; tune it to the posture's delta if needed.
    compound_cap = (1.0 + annual_cap) ** years - 1.0
    capped_return = min(scenario_return, compound_cap)
    optioned_strategy_value = optioned_value * (1 + capped_return)
    untouched_strategy_value = untouched_value * (1 + scenario_return)
    premium_collected = expected_annual_premium * years
    # Informational only (not part of strategy_value): premium is counted once via the SATA sidecar.
    lost_upside = max(optioned_value * (scenario_return - capped_return), 0.0)
    cash_drag_effect = max(optioned_value * scenario_return * (1 - sleeve_effective_delta), 0.0)
    strategy_value = untouched_strategy_value + optioned_strategy_value + other_assets_value + sata_projected_value
    net = strategy_value - buy_hold_value
    upside_capture = (strategy_value - total_start) / (buy_hold_value - total_start) if buy_hold_value != total_start else 1.0
    warning = None
    if scenario_return >= 1.0 and net < 0:
        warning = "Strategy underperforms buy-and-hold in a straight-line heavy bull market."
    return ScenarioResult(
        name=name,
        years=years,
        scenario_return=scenario_return,
        buy_hold_value=buy_hold_value,
        strategy_value=strategy_value,
        sata_sidecar_value=sata_projected_value,
        premium_collected=premium_collected,
        lost_upside=lost_upside,
        cash_drag_effect=cash_drag_effect,
        net_vs_buy_hold=net,
        gain_vs_starting_capital=strategy_value - total_start,
        upside_capture_ratio=upside_capture,
        warning=warning,
    )


def analyze_default_scenarios(
    starting_ibit_asst_value: float,
    other_assets_value: float,
    optioned_pct: float,
    expected_annual_premium: float,
    sata_values_by_year: dict[int, float],
    sleeve_effective_delta: float = 0.65,
    sata_starting_value: float = 0.0,
    annual_cap: float = 0.35,
) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for years in (1, 3, 5):
        for name, scenario_return in DEFAULT_SCENARIOS.items():
            results.append(
                analyze_scenario(
                    name=name,
                    scenario_return=scenario_return,
                    years=years,
                    starting_ibit_asst_value=starting_ibit_asst_value,
                    other_assets_value=other_assets_value,
                    optioned_pct=optioned_pct,
                    expected_annual_premium=expected_annual_premium,
                    sata_projected_value=sata_values_by_year.get(years, 0.0),
                    sleeve_effective_delta=sleeve_effective_delta,
                    sata_starting_value=sata_starting_value,
                    annual_cap=annual_cap,
                )
            )
    return results
