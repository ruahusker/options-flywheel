from __future__ import annotations

from dataclasses import dataclass, field
from math import floor

from app.schemas.market_data import OptionContractSchema, Quote
from app.services.indicators import IndicatorResult
from app.services.options_math import (
    cash_secured_put_breakeven,
    covered_call_breakeven,
    option_sale_edge_per_share,
    prob_price_above,
    risk_neutral_prob_itm,
)


@dataclass
class OptimizerSettings:
    optioned_pct: float = 0.35
    min_untouched_pct: float = 0.50
    call_delta_min: float = 0.25
    call_delta_max: float = 0.40
    put_delta_min: float = 0.35
    put_delta_max: float = 0.50
    dte_min: int = 1
    dte_max: int = 14
    min_weekly_premium: float = 25.0
    max_assignment_probability: float = 0.55
    max_spread_pct: float = 0.15
    min_open_interest: int = 10
    min_volume: int = 0
    objective: str = "balanced"
    allow_calls: bool = True
    allow_puts: bool = True
    allow_protected_call_spread: bool = True
    # Risk-free rate / dividend yield used for the risk-neutral P(ITM). IBIT and ASST pay no
    # dividend, so dividend_yield=0 is correct; both can be overridden per run if that changes.
    risk_free_rate: float = 0.04
    dividend_yield: float = 0.0
    # When selling, you realize closer to the bid than the mid. fill_slippage is the fraction of
    # the (mid - bid) half-spread you give up: 0.0 = fill at mid (optimistic), 1.0 = fill at bid.
    fill_slippage: float = 0.5
    # Score thresholds / anchors (previously hard-coded magic numbers).
    min_total_score: float = 45.0
    skip_score: float = 44.0
    premium_yield_full_score: float = 0.015  # weekly premium / notional that earns a 100 premium_score
    high_delta_gate: float = 0.39  # delta at/above which extra upside-loss warnings/penalties apply
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "ev": 0.30,        # expected value (premium vs. expected payout at forecast vol) — the core edge
            "upside": 0.20,    # upside preservation (lower delta keeps more uncapped stock)
            "premium": 0.15,   # raw premium yield
            "trend": 0.10,     # trend alignment
            "vrp": 0.10,       # variance risk premium: IV relative to realized vol
            "iv_rank": 0.05,   # IV relative to its own trailing range (rich vs. cheap)
            "liquidity": 0.05,
            "scenario": 0.05,
        }
    )


@dataclass
class StrategyCandidateResult:
    symbol: str
    action: str
    contracts: int
    expiration: object | None
    strike: float | None
    option_type: str | None
    side: str | None
    delta: float | None
    bid: float | None
    ask: float | None
    mid: float | None
    expected_credit: float
    collateral_required: float
    premium_yield_weekly: float
    premium_yield_annualized: float
    assignment_probability_proxy: float
    upside_cap: float | None
    upside_preserved_score: float
    liquidity_score: float
    trend_alignment_score: float
    iv_score: float
    scenario_score: float
    total_score: float
    reason: str
    expected_value: float = 0.0
    expected_value_annualized_yield: float = 0.0
    ev_score: float = 50.0
    vrp_ratio: float | None = None
    vrp_score: float = 50.0
    iv_rank: float | None = None
    iv_rank_score: float = 50.0
    breakeven: float | None = None
    profit_probability: float | None = None
    forecast_vol: float | None = None
    warnings: list[str] = field(default_factory=list)
    rejected: bool = False


def optioned_contracts(shares: float, optioned_pct: float) -> int:
    optioned_shares = floor((shares * optioned_pct) / 100) * 100
    return int(optioned_shares / 100)


def rank_candidates(
    symbol: str,
    shares: float,
    available_cash: float,
    quote: Quote,
    options: list[OptionContractSchema],
    indicators: IndicatorResult | None,
    settings: OptimizerSettings,
    wheel_in_cash: bool = False,
    existing_short_call_contracts: int = 0,
    iv_rank: float | None = None,
) -> list[StrategyCandidateResult]:
    candidates: list[StrategyCandidateResult] = []
    if settings.optioned_pct > 1:
        settings.optioned_pct = settings.optioned_pct / 100
    if settings.min_untouched_pct > 1:
        settings.min_untouched_pct = settings.min_untouched_pct / 100
    contracts = optioned_contracts(shares, settings.optioned_pct)
    if 1 - settings.optioned_pct < settings.min_untouched_pct:
        candidates.append(skip_candidate(symbol, "Minimum untouched core percentage would be violated."))
        return candidates
    if contracts <= 0 and not wheel_in_cash:
        candidates.append(skip_candidate(symbol, "Not enough optioned shares for a 100-share contract."))
        return candidates
    remaining_call_contracts = max(contracts - max(existing_short_call_contracts, 0), 0)
    if not wheel_in_cash and settings.allow_calls and existing_short_call_contracts >= contracts:
        candidates.append(
            skip_candidate(
                symbol,
                f"Existing short calls ({existing_short_call_contracts}) already meet or exceed the target "
                f"optioned sleeve ({contracts}); do not add more covered calls.",
            )
        )
        return candidates

    trend_state = indicators.trend_state if indicators else "unknown"
    for option in options:
        if option.dte is not None and not (settings.dte_min <= option.dte <= settings.dte_max):
            continue
        if option.mid is None:
            mid = ((option.bid or 0) + (option.ask or 0)) / 2 if option.bid is not None and option.ask is not None else option.last
        else:
            mid = option.mid
        if not mid or mid <= 0:
            continue
        spread_pct = ((option.ask or mid) - (option.bid or mid)) / mid if mid else 1
        warnings: list[str] = []
        rejected = False
        if spread_pct > settings.max_spread_pct:
            warnings.append("Wide bid/ask spread makes modeled premium less reliable.")
            rejected = True
        if (option.open_interest or 0) < settings.min_open_interest:
            warnings.append("Open interest is below the configured minimum.")
            rejected = True
        if (option.volume or 0) < settings.min_volume:
            warnings.append("Volume is below the configured minimum.")
            rejected = True
        if option.is_stale:
            warnings.append("Option chain data is stale.")

        delta = option.delta
        abs_delta = abs(delta) if delta is not None else None
        if option.option_type == "call" and not wheel_in_cash and settings.allow_calls:
            # The delta band is a scoring *tilt* (via upside_score), not a hard gate: evaluate every
            # call and let risk-adjusted expected value choose the best. This prevents hiding a
            # genuinely worthwhile trade just because it sits outside the technically-preferred band.
            if abs_delta is None:
                continue
            candidate_contracts = remaining_call_contracts
            if candidate_contracts <= 0:
                continue
            fill = _sell_fill_price(option, mid, settings)
            expected_credit = candidate_contracts * 100 * fill
            if expected_credit < settings.min_weekly_premium:
                warnings.append("Premium is below the configured minimum.")
                rejected = True
            candidates.append(
                score_candidate(
                    symbol=symbol,
                    action="sell call",
                    contracts=candidate_contracts,
                    option=option,
                    quote=quote,
                    fill_price=fill,
                    expected_credit=expected_credit,
                    collateral_required=0.0,
                    trend_state=trend_state,
                    settings=settings,
                    indicators=indicators,
                    iv_rank=iv_rank,
                    warnings=warnings,
                    rejected=rejected,
                )
            )
        elif option.option_type == "put" and wheel_in_cash and settings.allow_puts:
            if abs_delta is None or not (settings.put_delta_min <= abs_delta <= settings.put_delta_max):
                continue
            max_contracts = int(available_cash // (option.strike * 100))
            candidate_contracts = min(max_contracts, max(1, contracts))
            if candidate_contracts <= 0:
                continue
            fill = _sell_fill_price(option, mid, settings)
            expected_credit = candidate_contracts * 100 * fill
            collateral = candidate_contracts * 100 * option.strike
            candidates.append(
                score_candidate(
                    symbol=symbol,
                    action="sell put",
                    contracts=candidate_contracts,
                    option=option,
                    quote=quote,
                    fill_price=fill,
                    expected_credit=expected_credit,
                    collateral_required=collateral,
                    trend_state=trend_state,
                    settings=settings,
                    indicators=indicators,
                    iv_rank=iv_rank,
                    warnings=warnings,
                    rejected=rejected,
                )
            )
    viable = [candidate for candidate in candidates if not candidate.rejected and candidate.total_score >= settings.min_total_score]
    if not viable:
        reason = "No option candidate cleared liquidity, premium, trend, and scenario filters."
        if candidates:
            reason = "Candidates existed but were rejected or scored too poorly; skip trade is preferred."
        candidates.append(skip_candidate(symbol, reason))
    return sorted(candidates, key=lambda c: c.total_score, reverse=True)


def _sell_fill_price(option: OptionContractSchema, mid: float, settings: OptimizerSettings) -> float:
    """Realized price when *selling*: haircut the mid toward the bid by fill_slippage.

    Using the raw mid systematically overstates the credit, especially on wide markets. With
    fill_slippage=0.5 the modeled fill sits halfway between mid and bid.
    """
    bid = option.bid
    if bid is None or bid <= 0 or bid >= mid:
        return mid
    return mid - settings.fill_slippage * (mid - bid)


def _forecast_vol(indicators: IndicatorResult | None, implied_volatility: float | None) -> float | None:
    """Best available forecast of realized volatility for the EV/edge calculation."""
    if indicators is not None:
        for value in (indicators.realized_vol_20, indicators.realized_vol_60, indicators.realized_vol_10):
            if value and value > 0:
                return float(value)
    if implied_volatility and implied_volatility > 0:
        # No realized-vol history: assume the option is priced near fair (forecast == IV) so the
        # edge term is neutral rather than fabricating a variance-risk premium we cannot observe.
        return float(implied_volatility)
    return None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def score_candidate(
    symbol: str,
    action: str,
    contracts: int,
    option: OptionContractSchema,
    quote: Quote,
    fill_price: float,
    expected_credit: float,
    collateral_required: float,
    trend_state: str,
    settings: OptimizerSettings,
    indicators: IndicatorResult | None,
    iv_rank: float | None,
    warnings: list[str],
    rejected: bool,
) -> StrategyCandidateResult:
    mid = option.mid or ((option.bid or 0) + (option.ask or 0)) / 2 or option.last or 0.0
    underlying_price = quote.price or 0.0
    notional = max(underlying_price * 100 * contracts, 1.0)
    premium_weekly = expected_credit / notional
    premium_annualized = premium_weekly * 52
    abs_delta = abs(option.delta or 0.0)
    iv = option.implied_volatility
    dte = option.dte if option.dte and option.dte > 0 else 1
    time_years = max(dte / 365.0, 1.0 / 365.0)
    rate = settings.risk_free_rate
    qyield = settings.dividend_yield
    # Risk-neutral drift isolates the IV-vs-RV (volatility) mispricing without injecting a
    # bullish price drift assumption into the edge.
    drift = rate - qyield
    forecast_vol = _forecast_vol(indicators, iv)

    # Assignment probability: prefer the correct risk-neutral N(d2); fall back to the delta proxy.
    assignment_proxy = min(abs_delta, 0.95)
    if iv and iv > 0 and underlying_price > 0:
        rn_prob = risk_neutral_prob_itm(underlying_price, option.strike, time_years, rate, iv, option.option_type, qyield)
        if rn_prob is not None:
            assignment_proxy = min(max(rn_prob, 0.0), 0.99)

    # Expected value / edge of selling this option at the modeled fill, judged against the
    # forecast (realized) volatility. This is the variance-risk-premium harvested (or paid).
    # With no vol forecast at all the edge is unknowable: keep it None so the EV score stays
    # neutral (50) instead of treating the whole premium as pure edge.
    expected_value = 0.0
    edge_per_share: float | None = None
    if forecast_vol and underlying_price > 0:
        edge_per_share = option_sale_edge_per_share(
            underlying_price, option.strike, time_years, forecast_vol, option.option_type, fill_price, drift
        )
        expected_value = edge_per_share * 100 * contracts
    ev_annualized_yield = (
        (edge_per_share / underlying_price) * (365.0 / dte)
        if edge_per_share is not None and underlying_price > 0
        else 0.0
    )

    # Breakeven + probability of a profitable outcome (covered call / CSP both profit above breakeven).
    if option.option_type == "call":
        breakeven = covered_call_breakeven(underlying_price, fill_price)
    else:
        breakeven = cash_secured_put_breakeven(option.strike, fill_price)
    profit_probability = None
    if forecast_vol and underlying_price > 0:
        profit_probability = prob_price_above(underlying_price, breakeven, time_years, forecast_vol, drift)

    liquidity = option.liquidity_score if option.liquidity_score is not None else _liquidity_score(option)
    premium_score = min(premium_weekly / settings.premium_yield_full_score * 100, 100)
    upside_score = max(0.0, 100 - abs_delta * 180) if option.option_type == "call" else max(0.0, 100 - abs_delta * 90)
    trend_score = _trend_score(option.option_type, abs_delta, trend_state)
    iv_score = min((iv or 0.4) / 1.0 * 100, 100)

    # EV score: 100 when the premium fully exceeds the expected payout, 50 when fairly priced,
    # 0 when the expected payout wipes out the premium. Unknown edge stays neutral at 50.
    ev_score = _clamp(50.0 + (edge_per_share / fill_price) * 50.0) if (edge_per_share is not None and fill_price > 0) else 50.0
    # VRP score: IV relative to realized vol. Ratio 1.0 -> neutral 50, 1.25 -> 100, 0.75 -> 0.
    vrp_ratio = (iv / forecast_vol) if (iv and forecast_vol and forecast_vol > 0) else None
    vrp_score = _clamp((vrp_ratio - 1.0) * 200.0 + 50.0) if vrp_ratio is not None else 50.0
    iv_rank_score = _clamp(iv_rank * 100.0) if iv_rank is not None else 50.0
    scenario_score = _scenario_score(option.option_type, abs_delta, trend_state)

    w = settings.weights
    total = (
        w.get("ev", 0.30) * ev_score
        + w.get("upside", 0.20) * upside_score
        + w.get("premium", 0.15) * premium_score
        + w.get("trend", 0.10) * trend_score
        + w.get("vrp", 0.10) * vrp_score
        + w.get("iv_rank", 0.05) * iv_rank_score
        + w.get("liquidity", 0.05) * liquidity
        + w.get("scenario", 0.05) * scenario_score
    )
    if rejected:
        total *= 0.45
    if edge_per_share is not None and edge_per_share < 0:
        total *= 0.60
        warnings.append("Implied vol is below realized vol, so the modeled premium does not cover the expected payout (negative edge).")
    if option.option_type == "call" and "bullish breakout" in trend_state and abs_delta >= settings.high_delta_gate:
        total *= 0.75
        warnings.append("Higher-delta call penalized because trend is a bullish breakout.")
    if option.option_type == "call" and expected_credit > 0 and abs_delta >= settings.high_delta_gate:
        warnings.append("Higher premium comes with materially lower upside preservation.")
    if assignment_proxy > settings.max_assignment_probability:
        # Soft penalty, not a hard gate: a genuinely rich option can still win, but it must earn
        # its way past the configured assignment comfort level instead of merely warning about it.
        total *= 0.85
        warnings.append(f"Assignment probability proxy ({assignment_proxy:.0%}) exceeds the configured comfort level.")
    if option.option_type == "put":
        warnings.append("Wheel sleeve is in cash/put phase and has only delta-equivalent upside exposure.")
    if quote.is_stale:
        warnings.append("Quote is stale; refresh before acting.")

    reason = (
        f"{action.title()} is ranked on expected value (premium vs. expected payout at forecast vol), "
        f"upside preservation, IV richness, trend, liquidity, and scenario robustness — not premium alone."
    )
    upside_cap = option.strike if option.option_type == "call" else None
    return StrategyCandidateResult(
        symbol=symbol,
        action=action,
        contracts=contracts,
        expiration=option.expiration,
        strike=option.strike,
        option_type=option.option_type,
        side="short",
        delta=option.delta,
        bid=option.bid,
        ask=option.ask,
        mid=mid,
        expected_credit=expected_credit,
        collateral_required=collateral_required,
        premium_yield_weekly=premium_weekly,
        premium_yield_annualized=premium_annualized,
        assignment_probability_proxy=assignment_proxy,
        upside_cap=upside_cap,
        upside_preserved_score=upside_score,
        liquidity_score=liquidity,
        trend_alignment_score=trend_score,
        iv_score=iv_score,
        scenario_score=scenario_score,
        total_score=total,
        reason=reason,
        expected_value=expected_value,
        expected_value_annualized_yield=ev_annualized_yield,
        ev_score=ev_score,
        vrp_ratio=vrp_ratio,
        vrp_score=vrp_score,
        iv_rank=iv_rank,
        iv_rank_score=iv_rank_score,
        breakeven=breakeven,
        profit_probability=profit_probability,
        forecast_vol=forecast_vol,
        warnings=warnings,
        rejected=rejected,
    )


def skip_candidate(symbol: str, reason: str) -> StrategyCandidateResult:
    return StrategyCandidateResult(
        symbol=symbol,
        action="skip trade",
        contracts=0,
        expiration=None,
        strike=None,
        option_type=None,
        side=None,
        delta=None,
        bid=None,
        ask=None,
        mid=None,
        expected_credit=0.0,
        collateral_required=0.0,
        premium_yield_weekly=0.0,
        premium_yield_annualized=0.0,
        assignment_probability_proxy=0.0,
        upside_cap=None,
        upside_preserved_score=100.0,
        liquidity_score=100.0,
        trend_alignment_score=100.0,
        iv_score=0.0,
        scenario_score=100.0,
        total_score=44.0,
        reason=reason,
        warnings=["Skip trade is valid when premium does not compensate for risk."],
        rejected=False,
    )


def _liquidity_score(option: OptionContractSchema) -> float:
    oi_score = min((option.open_interest or 0) / 500 * 60, 60)
    volume_score = min((option.volume or 0) / 100 * 30, 30)
    mid = option.mid or option.last or 0.01
    spread = ((option.ask or mid) - (option.bid or mid)) / mid if mid else 1
    spread_score = max(0.0, 10 - spread * 25)
    return min(100.0, oi_score + volume_score + spread_score)


def _trend_score(option_type: str, abs_delta: float, trend_state: str) -> float:
    if option_type == "call":
        if trend_state == "bullish breakout":
            return 85 if abs_delta <= 0.30 else 45
        if trend_state == "bullish trend":
            return 80 if abs_delta <= 0.35 else 60
        if trend_state in {"neutral/chop", "weakening"}:
            return 85 if 0.30 <= abs_delta <= 0.40 else 65
        if trend_state == "bearish":
            return 50
    if option_type == "put":
        if trend_state == "bearish":
            return 35
        if trend_state in {"bullish trend", "neutral/chop"}:
            return 75
    return 60


def _scenario_score(option_type: str, abs_delta: float, trend_state: str) -> float:
    score = 100 - abs_delta * 100
    if option_type == "call" and trend_state in {"bullish breakout", "bullish trend"}:
        score -= abs_delta * 60
    if option_type == "put":
        score -= abs_delta * 20
    return max(0.0, min(100.0, score))
