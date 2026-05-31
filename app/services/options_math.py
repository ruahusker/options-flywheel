from __future__ import annotations

import math
from dataclasses import dataclass


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's rational approximation, ~1e-9 accuracy)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def _d1_d2(spot: float, strike: float, time_years: float, rate: float, volatility: float, dividend_yield: float):
    if spot <= 0 or strike <= 0 or time_years <= 0 or volatility <= 0:
        raise ValueError("spot, strike, time, and volatility must be positive")
    vol_sqrt_t = volatility * math.sqrt(time_years)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * volatility**2) * time_years) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return d1, d2


def black_scholes_merton(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    volatility: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> Greeks:
    d1, d2 = _d1_d2(spot, strike, time_years, rate, volatility, dividend_yield)
    df_r = math.exp(-rate * time_years)
    df_q = math.exp(-dividend_yield * time_years)
    if option_type == "call":
        price = spot * df_q * norm_cdf(d1) - strike * df_r * norm_cdf(d2)
        delta = df_q * norm_cdf(d1)
        theta = (
            -(spot * df_q * norm_pdf(d1) * volatility) / (2 * math.sqrt(time_years))
            - rate * strike * df_r * norm_cdf(d2)
            + dividend_yield * spot * df_q * norm_cdf(d1)
        ) / 365
        rho = strike * time_years * df_r * norm_cdf(d2) / 100
    else:
        price = strike * df_r * norm_cdf(-d2) - spot * df_q * norm_cdf(-d1)
        delta = -df_q * norm_cdf(-d1)
        theta = (
            -(spot * df_q * norm_pdf(d1) * volatility) / (2 * math.sqrt(time_years))
            + rate * strike * df_r * norm_cdf(-d2)
            - dividend_yield * spot * df_q * norm_cdf(-d1)
        ) / 365
        rho = -strike * time_years * df_r * norm_cdf(-d2) / 100
    gamma = df_q * norm_pdf(d1) / (spot * volatility * math.sqrt(time_years))
    vega = spot * df_q * norm_pdf(d1) * math.sqrt(time_years) / 100
    return Greeks(price=price, delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def implied_volatility(
    market_price: float,
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    option_type: str,
    dividend_yield: float = 0.0,
    low: float = 0.01,
    high: float = 5.0,
    max_iter: int = 100,
) -> float | None:
    if market_price <= 0 or spot <= 0 or strike <= 0 or time_years <= 0:
        return None
    lo = low
    hi = high
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        price = black_scholes_merton(spot, strike, time_years, rate, mid, option_type, dividend_yield).price
        if abs(price - market_price) < 1e-4:
            return mid
        if price > market_price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def covered_call_payoff(underlying_price_at_expiration: float, strike: float, contracts: int, premium_received: float) -> float:
    opportunity_cost = max(underlying_price_at_expiration - strike, 0.0) * 100 * contracts
    return premium_received - opportunity_cost


def cash_secured_put_payoff(underlying_price_at_expiration: float, strike: float, contracts: int, premium_received: float) -> float:
    put_loss = max(strike - underlying_price_at_expiration, 0.0) * 100 * contracts
    return premium_received - put_loss


def call_spread_payoff(
    underlying_price_at_expiration: float,
    short_strike: float,
    long_strike: float,
    contracts: int,
    net_credit: float,
) -> float:
    short_loss = max(underlying_price_at_expiration - short_strike, 0.0) * 100 * contracts
    long_gain = max(underlying_price_at_expiration - long_strike, 0.0) * 100 * contracts
    return net_credit - short_loss + long_gain


def call_spread_max_give_up(short_strike: float, long_strike: float, contracts: int, net_credit: float) -> float:
    return max((long_strike - short_strike) * 100 * contracts - net_credit, 0.0)


def effective_delta(untouched_pct: float, optioned_pct: float, sleeve_delta: float) -> float:
    return untouched_pct * 1.0 + optioned_pct * sleeve_delta


def risk_neutral_prob_itm(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    volatility: float,
    option_type: str,
    dividend_yield: float = 0.0,
) -> float | None:
    """Risk-neutral probability the option finishes in the money.

    This is N(d2) for a call and N(-d2) for a put, which is the mathematically correct
    P(ITM) under the risk-neutral measure. Delta (N(d1)) is a biased proxy that overstates
    a call's assignment probability, so prefer this when IV and time are available.
    """
    try:
        _, d2 = _d1_d2(spot, strike, time_years, rate, volatility, dividend_yield)
    except ValueError:
        return None
    return norm_cdf(d2) if option_type == "call" else norm_cdf(-d2)


def expected_terminal_intrinsic(
    spot: float,
    strike: float,
    time_years: float,
    forecast_vol: float,
    option_type: str,
    drift: float,
) -> float:
    """E[max(S_T - K, 0)] (call) or E[max(K - S_T, 0)] (put), undiscounted, per share.

    Uses a lognormal terminal price S_T = spot * exp((drift - 0.5*vol^2)*T + vol*sqrt(T)*Z).
    Pass the *forecast* (realized/expected) volatility here — not the implied vol the option is
    priced at — so that comparing this expected cost against the premium received reveals the
    variance-risk-premium edge (premium is set by IV; expected payout is governed by RV).
    """
    if time_years <= 0 or forecast_vol <= 0 or spot <= 0 or strike <= 0:
        if option_type == "call":
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)
    forward = spot * math.exp(drift * time_years)
    sd = forecast_vol * math.sqrt(time_years)
    d1 = (math.log(forward / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if option_type == "call":
        return forward * norm_cdf(d1) - strike * norm_cdf(d2)
    return strike * norm_cdf(-d2) - forward * norm_cdf(-d1)


def option_sale_edge_per_share(
    spot: float,
    strike: float,
    time_years: float,
    forecast_vol: float,
    option_type: str,
    premium_per_share: float,
    drift: float,
) -> float:
    """Expected profit (per share) of selling one option: premium kept minus expected payout.

    Positive => the premium more than compensates the expected cost given the vol forecast
    (i.e. the option is rich / IV > RV). Negative => you are being underpaid for the risk.
    For a covered call the stock leg is unchanged whether or not you sell, so this short-call
    edge is exactly the incremental expected value of the covered-call overlay vs. just holding.
    """
    expected_payout = expected_terminal_intrinsic(spot, strike, time_years, forecast_vol, option_type, drift)
    return premium_per_share - expected_payout


def prob_price_above(spot: float, level: float, time_years: float, forecast_vol: float, drift: float) -> float:
    """P(S_T > level) under a lognormal terminal price with the given drift and forecast vol."""
    if level <= 0:
        return 1.0
    if time_years <= 0 or forecast_vol <= 0 or spot <= 0:
        return 1.0 if spot > level else 0.0
    sd = forecast_vol * math.sqrt(time_years)
    d = (math.log(spot / level) + (drift - 0.5 * forecast_vol * forecast_vol) * time_years) / sd
    return norm_cdf(d)


def covered_call_breakeven(underlying_price: float, premium_per_share: float) -> float:
    """Downside breakeven of long stock + short call: you keep the premium as a cushion."""
    return underlying_price - premium_per_share


def cash_secured_put_breakeven(strike: float, premium_per_share: float) -> float:
    """Breakeven if assigned on a short put: effective cost basis is strike minus premium."""
    return strike - premium_per_share


def true_strategy_value(
    long_position_value: float,
    sata_value: float,
    cash_value: float,
    pending_activity: float,
    short_option_mark_to_market_liability: float,
    long_option_mark_to_market_value: float,
) -> float:
    return (
        long_position_value
        + sata_value
        + cash_value
        + pending_activity
        - short_option_mark_to_market_liability
        + long_option_mark_to_market_value
    )


def net_sidecar_value(
    sata_value: float,
    option_premium_cash: float,
    cash_reserve: float,
    short_option_mark_to_market_liability: float,
    long_option_mark_to_market_value: float,
) -> float:
    return sata_value + option_premium_cash + cash_reserve - short_option_mark_to_market_liability + long_option_mark_to_market_value
