from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.services.options_math import norm_ppf


@dataclass
class MonteCarloResult:
    paths: int
    years: int
    median: float
    mean: float
    p5: float
    p25: float
    p75: float
    p95: float
    win_rate_vs_buy_hold: float
    probability_underperforming_by_threshold: float
    expected_sata_value: float
    expected_income_at_rate: float
    worst_simulated_drawdown: float
    upside_capture_ratio: float
    assignment_frequency: float
    time_in_cash: float
    time_fully_invested: float
    time_capped: float
    time_uncovered: float


def run_monte_carlo(
    starting_ibit_value: float,
    starting_asst_value: float,
    other_assets_value: float,
    sata_starting_value: float,
    years: int = 5,
    paths: int = 5000,
    ibit_vol: float = 0.65,
    asst_vol: float = 0.85,
    drift: float = 0.08,
    ibit_drift: float | None = None,
    asst_drift: float | None = None,
    correlation: float = 0.55,
    annual_premium_rate: float = 0.12,
    premium_variability: float = 0.35,
    optioned_pct: float = 0.35,
    call_delta: float = 0.30,
    sata_rate: float = 0.13,
    assignment_threshold_weekly_return: float | None = None,
    put_reentry_probability: float = 0.45,
    seed: int = 42,
) -> MonteCarloResult:
    rng = np.random.default_rng(seed)
    weeks = int(52 * years)
    dt = 1 / 52
    # Per-symbol drift: default each to the shared (deliberately modest) drift unless overridden.
    ibit_mu = drift if ibit_drift is None else ibit_drift
    asst_mu = drift if asst_drift is None else asst_drift
    cov = np.array([[ibit_vol**2, correlation * ibit_vol * asst_vol], [correlation * ibit_vol * asst_vol, asst_vol**2]])
    chol = np.linalg.cholesky(cov)

    strategy_end = np.zeros(paths)
    buy_hold_end = np.zeros(paths)
    sata_end = np.zeros(paths)
    drawdowns = np.zeros(paths)
    assignments = np.zeros(paths)
    cash_time = np.zeros(paths)
    capped_time = np.zeros(paths)

    starting_risky = starting_ibit_value + starting_asst_value
    ibit_weight = starting_ibit_value / starting_risky if starting_risky else 0.0
    asst_weight = starting_asst_value / starting_risky if starting_risky else 0.0
    optioned_capital = starting_risky * optioned_pct
    untouched_capital = starting_risky * (1 - optioned_pct)

    # Derive the weekly return that triggers assignment from the *chosen* call delta rather than a
    # fixed 8%: a delta-d call's strike sits ~N^{-1}(1-d) blended-weekly-sigmas above spot, so the
    # MC reflects the actual posture the optimizer selects.
    if assignment_threshold_weekly_return is None:
        blended_weekly_vol = (ibit_weight * ibit_vol + asst_weight * asst_vol) * np.sqrt(dt)
        if 0.0 < call_delta < 1.0 and blended_weekly_vol > 0:
            assignment_threshold_weekly_return = float(np.exp(blended_weekly_vol * norm_ppf(1.0 - call_delta)) - 1.0)
        else:
            assignment_threshold_weekly_return = 0.08

    for path in range(paths):
        ibit_price_index = 1.0
        asst_price_index = 1.0
        untouched = untouched_capital
        wheel_value = optioned_capital
        sata = sata_starting_value
        in_cash = False
        peak = starting_risky + other_assets_value + sata
        max_drawdown = 0.0
        assignment_count = 0
        cash_weeks = 0
        capped_weeks = 0

        for _ in range(weeks):
            z = rng.normal(size=2)
            shocks = chol @ z
            ibit_ret = (ibit_mu - 0.5 * ibit_vol**2) * dt + shocks[0] * np.sqrt(dt)
            asst_ret = (asst_mu - 0.5 * asst_vol**2) * dt + shocks[1] * np.sqrt(dt)
            ibit_week = float(np.exp(ibit_ret) - 1)
            asst_week = float(np.exp(asst_ret) - 1)
            blended_ret = ibit_weight * ibit_week + asst_weight * asst_week
            ibit_price_index *= 1 + ibit_week
            asst_price_index *= 1 + asst_week

            untouched *= 1 + blended_ret
            # Premium scales with the current sleeve value (covered calls when invested, cash-secured
            # puts on the same notional when in cash), not a frozen initial figure.
            premium = max(0.0, wheel_value * annual_premium_rate / 52 * rng.normal(1.0, premium_variability))
            sata = (sata + premium) * (1 + sata_rate / 52)

            if in_cash:
                cash_weeks += 1
                wheel_value *= 1.0
                if rng.random() < put_reentry_probability:
                    in_cash = False
            else:
                capped_weeks += 1
                capped_ret = min(blended_ret, assignment_threshold_weekly_return)
                wheel_value *= 1 + capped_ret
                if blended_ret > assignment_threshold_weekly_return:
                    in_cash = True
                    assignment_count += 1

            portfolio_value = untouched + wheel_value + other_assets_value + sata
            peak = max(peak, portfolio_value)
            max_drawdown = min(max_drawdown, portfolio_value / peak - 1)

        risky_buy_hold = starting_ibit_value * ibit_price_index + starting_asst_value * asst_price_index
        buy_hold_end[path] = risky_buy_hold + other_assets_value + sata_starting_value
        strategy_end[path] = untouched + wheel_value + other_assets_value + sata
        sata_end[path] = sata
        drawdowns[path] = max_drawdown
        assignments[path] = assignment_count
        cash_time[path] = cash_weeks / weeks
        capped_time[path] = capped_weeks / weeks

    diff = strategy_end - buy_hold_end
    threshold = 0.10 * (starting_risky + other_assets_value)
    buy_hold_gain = buy_hold_end - (starting_risky + other_assets_value + sata_starting_value)
    strategy_gain = strategy_end - (starting_risky + other_assets_value + sata_starting_value)
    positive_buy_hold_gain = buy_hold_gain > 0
    upside_capture = float(np.mean(strategy_gain[positive_buy_hold_gain] / buy_hold_gain[positive_buy_hold_gain])) if np.any(positive_buy_hold_gain) else 0.0

    return MonteCarloResult(
        paths=paths,
        years=years,
        median=float(np.percentile(strategy_end, 50)),
        mean=float(np.mean(strategy_end)),
        p5=float(np.percentile(strategy_end, 5)),
        p25=float(np.percentile(strategy_end, 25)),
        p75=float(np.percentile(strategy_end, 75)),
        p95=float(np.percentile(strategy_end, 95)),
        win_rate_vs_buy_hold=float(np.mean(diff > 0)),
        probability_underperforming_by_threshold=float(np.mean(diff < -threshold)),
        expected_sata_value=float(np.mean(sata_end)),
        expected_income_at_rate=float(np.mean(sata_end) * sata_rate),
        worst_simulated_drawdown=float(np.min(drawdowns)),
        upside_capture_ratio=upside_capture,
        assignment_frequency=float(np.mean(assignments)),
        time_in_cash=float(np.mean(cash_time)),
        time_fully_invested=float(1 - np.mean(cash_time)),
        time_capped=float(np.mean(capped_time)),
        time_uncovered=float(1 - optioned_pct),
    )
