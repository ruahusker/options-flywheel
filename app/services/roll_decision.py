from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from app.services.indicators import IndicatorResult
from app.services.historical_roll_backtest import HistoricalRollBacktestHint, build_historical_roll_backtest
from app.services.indicators import calculate_indicators
from app.services.iv_history import iv_rank_for_symbol, record_atm_iv
from app.services.recommendation_engine import generate_recommendation
from app.services.strategy_optimizer import OptimizerSettings, StrategyCandidateResult, optioned_contracts


# Hard ceiling on days-to-expiration: never sell a call further out than this. The owner trades the
# weekly cycle and does not want to sell upside more than a week ahead.
MAX_DTE = 7
# Buy-to-close once at least half the original credit has been captured. Closing early at ~50% locks
# in the bulk of the premium while shedding the assignment/gamma risk of holding into expiration.
TAKE_PROFIT_TRIGGER_PCT = 0.50
CHEAP_BUYBACK_PER_SHARE = 0.05
# A shorter/different expiration is only surfaced as the better trade when its per-day value beats
# the standard weekly target's by more than this fraction — small noise should not trigger a roll
# suggestion, but the comparison is per *calendar day* so short-dated weeklies compete fairly.
ALTERNATIVE_PER_DAY_MARGIN = 0.20


def candidate_per_day_value(candidate: StrategyCandidateResult, dte: int) -> float:
    """Dollars of value per calendar day of capital commitment.

    The flywheel re-sells as soon as a position expires, so trades on different expirations must be
    compared per day: a 2-DTE collecting 40% of a 7-DTE's premium is the better trade. Uses the
    model's expected value (premium minus expected payout) when a vol forecast exists, otherwise
    falls back to raw credit.
    """
    days = max(dte, 1)
    if candidate.forecast_vol:
        return candidate.expected_value / days
    return candidate.expected_credit / days


@dataclass(frozen=True)
class RollPosture:
    coverage_pct: float
    call_delta_min: float
    call_delta_max: float
    dte_min: int
    dte_max: int
    label: str
    reason: str


@dataclass(frozen=True)
class CoveredCallManagement:
    action: str
    label: str
    reason: str
    contracts: int = 0
    average_strike: float | None = None
    expiration: date | None = None
    original_credit: float | None = None
    buyback_cost: float | None = None
    open_profit: float | None = None
    captured_pct: float | None = None
    buyback_mark: float | None = None
    replacement: StrategyCandidateResult | None = None
    # True when the re-sell/roll target sits on a different expiration than the calls being closed —
    # i.e. a genuine calendar roll (often into a shorter, richer weekly) rather than a same-week resell.
    roll_to_different_expiration: bool = False


@dataclass(frozen=True)
class RollDecisionRow:
    symbol: str
    shares: float
    quote_price: float | None
    chosen_expiration: date | None
    chosen_dte: int | None
    trend_state: str
    rsi_14: float | None
    price_vs_20d_high: float | None
    price_vs_20d_low: float | None
    existing_short_calls: int
    current_covered_pct: float
    posture: RollPosture
    selected: StrategyCandidateResult
    current_add_on: StrategyCandidateResult
    call_management: CoveredCallManagement
    sleeve_rows: list[dict]
    put_reentry: StrategyCandidateResult | None
    backtest_hint: HistoricalRollBacktestHint | None
    warnings: list[str]
    # Expiration of the calls you already hold, and the weekly the reset targets (the one after).
    current_call_expiry: date | None = None
    reset_expiration: date | None = None
    # Representative weekly premium the flywheel generates by rolling the target sleeve on the nearest
    # liquid weekly. Drives the SATA projection/routing so it does not collapse to $0 when you are
    # already covered this week or when the far-dated reset chain is too stale to price.
    recurring_weekly_premium: float = 0.0
    # Set when the selected call sits well above the posture's delta band because the in-band weekly
    # strikes were too wide/illiquid to model and the optimizer fell back to the nearest liquid
    # (usually in-the-money) strike. Surfaced as a caution; it normally clears as the week's quotes fill in.
    offband_delta_note: str | None = None
    # Best risk-adjusted call found across *all* weeklies within the 7-day ceiling (including short
    # 1-4 DTE expirations), surfaced only when it out-scores the standard weekly target. Lets a juicy
    # short-dated option be recommended instead of forcing the 5-7 DTE weekly. None when the standard
    # weekly is already the best available.
    sharper_alternative: StrategyCandidateResult | None = None
    sharper_alternative_dte: int | None = None
    # Active cash-secured puts — the wheel's cash phase. Imported short puts get the same
    # take-profit/roll management as covered calls, and a symbol that is ALL puts (no shares
    # after being called away) still gets a row instead of disappearing from the cockpit.
    existing_short_puts: int = 0
    put_collateral: float = 0.0
    put_management: CoveredCallManagement | None = None


def choose_roll_expiration(expirations: list[date], *, today: date | None = None, dte_min: int = 5, dte_max: int = MAX_DTE) -> date | None:
    today = today or date.today()
    cap = min(dte_max, MAX_DTE)  # never sell past the hard ceiling, regardless of posture/backtest
    within_cap = [(expiration, (expiration - today).days) for expiration in sorted(expirations) if 1 <= (expiration - today).days <= cap]
    if not within_cap:
        return None  # no expiration on or before the ceiling -> do not sell this week
    in_band = [expiration for expiration, dte in within_cap if dte >= dte_min]
    if in_band:
        return in_band[0]
    # Nothing as far out as dte_min, but something within the ceiling: take the furthest allowed.
    return within_cap[-1][0]


def expirations_within_ceiling(
    expirations: list[date], *, today: date | None = None, dte_min: int = 1, dte_max: int = MAX_DTE
) -> list[date]:
    """Every weekly expiration from dte_min..dte_max days out (capped at the 7-day ceiling), nearest
    first. Unlike choose_roll_expiration this keeps the *whole* set so shorter weeklies can compete."""
    today = today or date.today()
    cap = min(dte_max, MAX_DTE)
    return [exp for exp in sorted(expirations) if dte_min <= (exp - today).days <= cap]


def best_roll_recommendation(
    *,
    symbol: str,
    shares: float,
    available_cash: float,
    quote,
    indicator,
    posture: RollPosture,
    provider,
    expirations: list[date],
    today: date,
    existing_short_call_contracts: int,
    iv_rank: float | None,
    near_exp: date | None = None,
    near_chain=None,
) -> tuple[StrategyCandidateResult | None, date | None]:
    """Evaluate the best sell-call candidate on *every* weekly within the ceiling — including short
    1-4 DTE expirations the standard 5-7 DTE search skips — and return the best one with its
    expiration. Within a chain the optimizer's risk-adjusted score picks the strike; *across*
    expirations candidates are compared on per-calendar-day value, because the flywheel redeploys
    immediately after expiry (raw per-trade score systematically favors the longest weekly).
    Returns (None, None) if nothing clears.
    """
    best: StrategyCandidateResult | None = None
    best_exp: date | None = None
    best_key: tuple[float, float] | None = None
    for exp in expirations_within_ceiling(expirations, today=today, dte_min=1, dte_max=posture.dte_max):
        chain = near_chain if (near_exp is not None and exp == near_exp) else provider.get_option_chain(symbol, exp)
        if not chain:
            continue
        settings = settings_for_posture(posture)
        settings.dte_min = 1  # let short-dated weeklies through; quality is enforced by the score
        rec = generate_recommendation(
            symbol=symbol,
            shares=shares,
            available_cash=available_cash,
            quote=quote,
            chain=chain,
            indicators=indicator,
            settings=settings,
            existing_short_call_contracts=existing_short_call_contracts,
            iv_rank=iv_rank,
        )
        if rec.best.action == "skip trade":
            continue
        dte = max((exp - today).days, 1)
        key = (candidate_per_day_value(rec.best, dte), rec.best.total_score)
        if best_key is None or key > best_key:
            best, best_exp, best_key = rec.best, exp, key
    return best, best_exp


def recommend_roll_posture(indicator: IndicatorResult | None) -> RollPosture:
    if indicator is None:
        return RollPosture(
            coverage_pct=0.35,
            call_delta_min=0.25,
            call_delta_max=0.35,
            dte_min=5,
            dte_max=7,
            label="Balanced",
            reason="Technical context is unavailable, so use the middle sleeve and avoid forcing high-delta premium.",
        )

    rsi = indicator.rsi_14
    near_high = indicator.price_vs_20d_high is not None and indicator.price_vs_20d_high > -0.02
    near_low = indicator.price_vs_20d_low is not None and indicator.price_vs_20d_low < 0.05
    trend = indicator.trend_state

    if rsi is not None and rsi >= 70:
        return RollPosture(0.50, 0.30, 0.40, 5, 7, "Extended", "RSI is overbought; sell more coverage and accept a higher delta to harvest premium.")
    if rsi is not None and rsi <= 35:
        return RollPosture(0.25, 0.20, 0.30, 5, 7, "Oversold", "RSI is washed out; keep coverage light so a rebound is not overly capped.")
    if trend == "weakening" or (near_high and rsi is not None and rsi >= 60):
        return RollPosture(0.50, 0.30, 0.40, 5, 7, "Fading", "Price is extended or momentum is weakening; favor more premium while keeping half the stack uncapped.")
    if trend in {"bullish breakout", "bullish trend"}:
        return RollPosture(0.25, 0.20, 0.30, 5, 7, "Upside-first", "Trend is constructive; keep most shares uncovered and use lower-delta calls.")
    if trend == "bearish" or near_low:
        return RollPosture(0.25, 0.20, 0.30, 5, 7, "Defensive", "Trend is bearish or near recent lows; avoid selling too much upside into a potential snapback.")
    return RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "Neutral/choppy setup; use the middle sleeve for premium without over-capping the stack.")


@dataclass(frozen=True)
class WeekVerdict:
    headline: str      # plain-language call to action, e.g. "Sell call" / "Hold" / "No trade"
    tone: str          # "sell" | "hold" | "skip" — drives the verdict card color
    edge_label: str    # short edge read, e.g. "rich · +EV" / "cheap · thin edge"
    edge_tone: str     # "positive" | "negative" | "neutral"


def _edge_read(candidate: StrategyCandidateResult | None) -> tuple[str, str]:
    """Variance-risk-premium read on a candidate: is the option rich (worth selling) or cheap?"""
    if candidate is None or candidate.action == "skip trade":
        return "no setup", "neutral"
    ev = candidate.expected_value or 0.0
    vrp = candidate.vrp_ratio
    if vrp is not None and vrp > 1 and ev > 0:
        return "rich · +EV", "positive"
    if (vrp is not None and vrp < 1) or ev < 0:
        return "cheap · thin edge", "negative"
    return "fair", "neutral"


def week_verdict(row: "RollDecisionRow") -> WeekVerdict:
    """Plain-language weekly verdict.

    The headline answers "what do I do THIS week, given what I already hold" — it is driven by
    current_add_on (which nets existing short calls against the target), NOT the reset trade. The
    edge chip reports the variance-risk-premium read on the reset trade so you can see whether the
    sleeve is worth re-selling when the current calls expire.
    """
    current = row.current_add_on
    edge_label, edge_tone = _edge_read(row.selected)

    if row.call_management.action == "buy_to_close_and_resell":
        headline = "Buy to close + roll" if row.call_management.roll_to_different_expiration else "Buy to close + re-sell"
        return WeekVerdict(headline, "sell", edge_label, edge_tone)
    if row.call_management.action == "buy_to_close_wait":
        return WeekVerdict("Buy to close", "hold", edge_label, edge_tone)

    # Wheel cash phase: no call action, but active cash-secured puts need managing.
    put_mgmt = row.put_management
    if (current is None or current.action == "skip trade") and put_mgmt is not None:
        if put_mgmt.action == "buy_to_close_and_resell":
            headline = "Roll puts" if put_mgmt.roll_to_different_expiration else "Buy to close puts + re-sell"
            return WeekVerdict(headline, "sell", edge_label, edge_tone)
        if put_mgmt.action == "buy_to_close_wait":
            return WeekVerdict("Buy to close puts", "hold", edge_label, edge_tone)
        if put_mgmt.action == "hold":
            return WeekVerdict("Hold — puts working", "hold", edge_label, edge_tone)

    if current is None or current.action == "skip trade":
        if row.existing_short_calls > 0:
            return WeekVerdict("Hold — already covered", "hold", edge_label, edge_tone)
        if row.existing_short_puts > 0:
            return WeekVerdict("Puts active", "hold", edge_label, edge_tone)
        return WeekVerdict("No new calls", "skip", edge_label, edge_tone)

    count = current.contracts
    return WeekVerdict(f"Add {count} call{'' if count == 1 else 's'}", "sell", edge_label, edge_tone)


def offband_delta_note(selected: StrategyCandidateResult, posture: RollPosture, quote_price: float | None) -> str | None:
    """Caution when the selected call's delta lands well above the posture band.

    The delta band is a soft scoring tilt, not a hard gate, so the optimizer can fall back to a
    high-delta (often in-the-money) strike when every in-band weekly strike is rejected for a wide
    spread or thin liquidity. That is usually a transient data-quality artifact on a low-liquidity
    name, not a deliberate decision to cap upside — so flag it rather than presenting it as a clean pick.
    """
    if selected.action == "skip trade" or selected.option_type != "call" or selected.delta is None:
        return None
    abs_delta = abs(selected.delta)
    # Only flag a meaningful overshoot above the band; small drift is expected and not worth noting.
    if abs_delta < posture.call_delta_max + 0.10:
        return None
    itm = (
        selected.strike is not None
        and quote_price is not None
        and quote_price > 0
        and selected.strike <= quote_price
    )
    where = "in-the-money " if itm else ""
    return (
        f"Selected delta {abs_delta:.2f} is above the {posture.call_delta_min:.0%}-{posture.call_delta_max:.0%} "
        f"target band. The in-band weekly strikes were too wide or thin to model, so the optimizer fell back to "
        f"the nearest liquid {where}strike. This usually tightens as the week's quotes fill in — re-check intraday."
    )


def action_label(candidate: StrategyCandidateResult) -> str:
    if candidate.action == "skip trade":
        return "Do not add a call"
    strike = f"${candidate.strike:,.2f}" if candidate.strike is not None else "-"
    expiration = str(candidate.expiration) if candidate.expiration is not None else "-"
    delta = f"{candidate.delta:.2f}" if candidate.delta is not None else "-"
    return f"{candidate.action.title()} {candidate.contracts} @ {strike}, {expiration}, delta {delta}"


def build_roll_decision_rows(metrics, options, provider, db=None) -> tuple[list[RollDecisionRow], list[str]]:
    rows: list[RollDecisionRow] = []
    warnings: list[str] = []
    for symbol in ("IBIT", "ASST"):
        shares = metrics.shares_by_symbol.get(symbol, 0.0)
        existing_short_puts = int(metrics.option_exposure.get(symbol, {}).get("short_puts", 0))
        # A symbol with no shares but active short puts is the wheel's cash phase — keep its row.
        if shares <= 0 and existing_short_puts <= 0:
            continue
        try:
            quote = provider.get_quote(symbol)
            history = provider.get_price_history(symbol, 120, "1d")
            indicator = calculate_indicators(symbol, history)
            posture = recommend_roll_posture(indicator)
            backtest_hint = None
            if db is not None:
                backtest_hint = build_historical_roll_backtest(
                    db,
                    symbol,
                    indicator,
                    static_coverage_pct=posture.coverage_pct,
                    static_delta_min=posture.call_delta_min,
                    static_delta_max=posture.call_delta_max,
                    static_dte_min=posture.dte_min,
                    static_dte_max=posture.dte_max,
                )
                posture = apply_backtest_hint(posture, backtest_hint)
            expirations = provider.get_option_expirations(symbol)
            today = date.today()
            existing_short_calls = int(metrics.option_exposure.get(symbol, {}).get("short_calls", 0))
            existing_call_exps = [
                opt.expiration for opt in options
                if opt.underlying == symbol and opt.option_type == "call" and opt.side == "short" and opt.expiration
            ]
            current_call_expiry = max(existing_call_exps) if existing_call_exps else None

            # Price everything off the nearest liquid weekly (reliable fills, consistent across the
            # premium/account/sleeve views). The *reset timing* — the weekly after your current calls
            # expire — is shown as a label only: far-dated chains are too thin to price today, and the
            # live cockpit reprices correctly once that weekly becomes the front month.
            near_exp = choose_roll_expiration(expirations, today=today, dte_min=posture.dte_min, dte_max=posture.dte_max)
            reset_anchor = current_call_expiry if (current_call_expiry and current_call_expiry > today) else today
            reset_exp = choose_roll_expiration(expirations, today=reset_anchor, dte_min=posture.dte_min, dte_max=posture.dte_max)

            chain = provider.get_option_chain(symbol, near_exp) if near_exp else []
            iv_rank = None
            if db is not None and chain:
                try:
                    atm_iv = record_atm_iv(db, symbol, chain, quote.price)
                    iv_rank = iv_rank_for_symbol(db, symbol, atm_iv)
                except Exception:
                    iv_rank = None

            selected_settings = settings_for_posture(posture)
            # Canonical target sleeve, priced on the liquid weekly. This single recommendation drives
            # the displayed reset trade, the recurring premium for SATA, the per-account split, and the
            # sleeve comparison — so every view stays consistent.
            target_rec = generate_recommendation(
                symbol=symbol,
                shares=shares,
                available_cash=metrics.cash_value + metrics.pending_activity,
                quote=quote,
                chain=chain,
                indicators=indicator,
                settings=selected_settings,
                existing_short_call_contracts=0,
                iv_rank=iv_rank,
            )
            recurring_weekly_premium = (
                target_rec.best.expected_credit if target_rec.best.action != "skip trade" else 0.0
            )
            # This-week add-on nets your existing short calls against the target (skip when covered).
            current_rec = generate_recommendation(
                symbol=symbol,
                shares=shares,
                available_cash=metrics.cash_value + metrics.pending_activity,
                quote=quote,
                chain=chain,
                indicators=indicator,
                settings=selected_settings,
                existing_short_call_contracts=existing_short_calls,
                iv_rank=iv_rank,
            )
            # Best call across *every* weekly within the 7-day ceiling, including short 1-4 DTE ones
            # the standard 5-7 DTE target skips. Reuse the already-fetched near_exp chain to avoid a
            # duplicate fetch. This becomes the roll/re-sell target and, when it beats the standard
            # weekly, a surfaced "sharper short-dated alternative".
            alt_best, alt_exp = best_roll_recommendation(
                symbol=symbol,
                shares=shares,
                available_cash=metrics.cash_value + metrics.pending_activity,
                quote=quote,
                indicator=indicator,
                posture=posture,
                provider=provider,
                expirations=expirations,
                today=today,
                existing_short_call_contracts=0,
                iv_rank=iv_rank,
                near_exp=near_exp,
                near_chain=chain,
            )
            roll_target = alt_best if alt_best is not None else target_rec.best
            sharper_alternative = None
            sharper_alternative_dte = None
            if (
                alt_best is not None
                and alt_exp is not None
                and alt_exp != near_exp
                and near_exp is not None
                and target_rec.best.action != "skip trade"
            ):
                # Compare per calendar day so a short-dated weekly competes fairly with the standard
                # 5-7 DTE target; require a clear margin so quote noise does not trigger a roll.
                alt_pd = candidate_per_day_value(alt_best, max((alt_exp - today).days, 1))
                std_pd = candidate_per_day_value(target_rec.best, max((near_exp - today).days, 1))
                if alt_pd > std_pd + ALTERNATIVE_PER_DAY_MARGIN * max(abs(std_pd), 1.0):
                    sharper_alternative = alt_best
                    sharper_alternative_dte = (alt_exp - today).days
            elif alt_best is not None and alt_exp is not None and alt_exp != near_exp and target_rec.best.action == "skip trade":
                # The standard weekly had no qualifying call, but a shorter weekly does — surface it.
                sharper_alternative = alt_best
                sharper_alternative_dte = (alt_exp - today).days
            call_management = covered_call_management(
                symbol=symbol,
                options=options,
                replacement=roll_target,
                posture=posture,
            )
            sleeve_rows = []
            for coverage in (0.25, 0.35, 0.50):
                sleeve_settings = settings_for_posture(posture, coverage_pct=coverage)
                rec = generate_recommendation(
                    symbol=symbol,
                    shares=shares,
                    available_cash=metrics.cash_value + metrics.pending_activity,
                    quote=quote,
                    chain=chain,
                    indicators=indicator,
                    settings=sleeve_settings,
                    existing_short_call_contracts=0,
                )
                sleeve_rows.append(
                    {
                        "coverage_pct": coverage,
                        "contracts": optioned_contracts(shares, coverage),
                        "action": rec.best.action,
                        "strike": rec.best.strike,
                        "delta": rec.best.delta,
                        "credit": rec.best.expected_credit,
                        "score": rec.best.total_score,
                    }
                )
            put_collateral = sum(
                opt.contracts * 100 * opt.strike
                for opt in options
                if opt.underlying == symbol and opt.option_type == "put" and opt.side == "short" and opt.contracts > 0
            )
            put_rec = generate_recommendation(
                symbol=symbol,
                shares=max(existing_short_calls * 100, existing_short_puts * 100, 100 if shares >= 100 else 0),
                # Cash freed by call assignment plus the cash already securing the open puts.
                available_cash=assignment_cash(symbol, options) + put_collateral,
                quote=quote,
                chain=chain,
                indicators=indicator,
                settings=put_settings(posture),
                wheel_in_cash=True,
                existing_short_call_contracts=0,
            )
            # Re-sell target for the open puts: the put re-entry pick, scaled to the number of
            # puts actually held so the management block compares like for like.
            put_replacement = put_rec.best
            if (
                existing_short_puts > 0
                and put_replacement.action == "sell put"
                and put_replacement.contracts not in (0, existing_short_puts)
            ):
                factor = existing_short_puts / put_replacement.contracts
                put_replacement = replace(
                    put_replacement,
                    contracts=existing_short_puts,
                    expected_credit=put_replacement.expected_credit * factor,
                    expected_value=put_replacement.expected_value * factor,
                    collateral_required=put_replacement.collateral_required * factor,
                )
            put_management = (
                short_put_management(symbol=symbol, options=options, replacement=put_replacement, posture=posture)
                if existing_short_puts > 0
                else None
            )
            # Wheel in cash phase: the recurring premium comes from rolling the puts, so the SATA
            # routing/projection doesn't collapse to $0 while waiting for reassignment.
            if recurring_weekly_premium <= 0 and existing_short_puts > 0 and put_replacement.action != "skip trade":
                recurring_weekly_premium = put_replacement.expected_credit
            rows.append(
                RollDecisionRow(
                    symbol=symbol,
                    shares=shares,
                    quote_price=quote.price,
                    chosen_expiration=near_exp,
                    chosen_dte=(near_exp - today).days if near_exp else None,
                    trend_state=indicator.trend_state,
                    rsi_14=indicator.rsi_14,
                    price_vs_20d_high=indicator.price_vs_20d_high,
                    price_vs_20d_low=indicator.price_vs_20d_low,
                    existing_short_calls=existing_short_calls,
                    current_covered_pct=(existing_short_calls * 100 / shares) if shares else 0.0,
                    posture=posture,
                    selected=target_rec.best,
                    current_add_on=current_rec.best,
                    call_management=call_management,
                    sleeve_rows=sleeve_rows,
                    put_reentry=put_rec.best,
                    backtest_hint=backtest_hint,
                    warnings=target_rec.warnings + current_rec.warnings + indicator.warnings,
                    current_call_expiry=current_call_expiry,
                    reset_expiration=reset_exp,
                    recurring_weekly_premium=recurring_weekly_premium,
                    offband_delta_note=offband_delta_note(target_rec.best, posture, quote.price),
                    sharper_alternative=sharper_alternative,
                    sharper_alternative_dte=sharper_alternative_dte,
                    existing_short_puts=existing_short_puts,
                    put_collateral=put_collateral,
                    put_management=put_management,
                )
            )
        except Exception as exc:
            warnings.append(f"{symbol}: {exc}")
    return rows, warnings


def covered_call_management(
    *,
    symbol: str,
    options,
    replacement: StrategyCandidateResult,
    posture: RollPosture,
) -> CoveredCallManagement:
    return short_option_management(
        symbol=symbol, options=options, replacement=replacement, posture=posture, option_type="call"
    )


def short_put_management(
    *,
    symbol: str,
    options,
    replacement: StrategyCandidateResult,
    posture: RollPosture,
) -> CoveredCallManagement:
    """Same take-profit/roll engine as covered calls, applied to active cash-secured puts —
    the wheel's cash phase. The replacement is the put re-entry candidate sized to the open puts."""
    return short_option_management(
        symbol=symbol, options=options, replacement=replacement, posture=posture, option_type="put"
    )


def short_option_management(
    *,
    symbol: str,
    options,
    replacement: StrategyCandidateResult,
    posture: RollPosture,
    option_type: str,
) -> CoveredCallManagement:
    word = "call" if option_type == "call" else "put"
    short_legs = [
        option
        for option in options
        if option.underlying == symbol
        and option.option_type == option_type
        and option.side == "short"
        and option.contracts > 0
    ]
    if not short_legs:
        return CoveredCallManagement(
            "none", f"No short {word}s", f"No active short {word}s were imported."
        )

    contracts = sum(option.contracts for option in short_legs)
    avg_strike = sum(option.contracts * option.strike for option in short_legs) / contracts if contracts else None
    expiration = max(option.expiration for option in short_legs if option.expiration) if short_legs else None
    original_credit = 0.0
    buyback_cost = 0.0
    for option in short_legs:
        original = _original_credit_per_share(option)
        mark = _buyback_mark_per_share(option)
        if original is None or original <= 0 or mark is None or mark < 0:
            return CoveredCallManagement(
                "unavailable",
                "Premium capture unavailable",
                f"Imported short {word}s are missing original credit or current mark, so the app cannot score a buy-to-close trigger.",
                contracts=contracts,
                average_strike=avg_strike,
                expiration=expiration,
            )
        original_credit += original * option.contracts * 100
        buyback_cost += mark * option.contracts * 100

    if original_credit <= 0:
        return CoveredCallManagement(
            "unavailable",
            "Premium capture unavailable",
            "Original short-call credit is unavailable or zero.",
            contracts=contracts,
            average_strike=avg_strike,
            expiration=expiration,
        )

    open_profit = original_credit - buyback_cost
    captured_pct = open_profit / original_credit
    buyback_mark = buyback_cost / (contracts * 100) if contracts else None
    replacement_ok = replacement.action != "skip trade" and replacement.option_type == option_type and replacement.contracts > 0
    # Re-selling only makes sense as a net-credit roll: the new sale must bring in more per share
    # than the buy-to-close costs. Otherwise closing pays away the remaining theta and the
    # "replacement" does not even cover the exit — wait for the next weekly to price up instead.
    replacement_per_share = (
        replacement.expected_credit / (replacement.contracts * 100)
        if replacement_ok and replacement.contracts
        else None
    )
    net_credit_roll = (
        replacement_per_share is not None
        and buyback_mark is not None
        and replacement_per_share > buyback_mark
    )
    rolls_to_new_expiration = (
        replacement_ok
        and replacement.expiration is not None
        and expiration is not None
        and replacement.expiration != expiration
    )
    close_triggered = captured_pct >= TAKE_PROFIT_TRIGGER_PCT or (
        buyback_mark is not None and buyback_mark <= CHEAP_BUYBACK_PER_SHARE
    )

    common = {
        "contracts": contracts,
        "average_strike": avg_strike,
        "expiration": expiration,
        "original_credit": original_credit,
        "buyback_cost": buyback_cost,
        "open_profit": open_profit,
        "captured_pct": captured_pct,
        "buyback_mark": buyback_mark,
        "replacement": replacement if replacement_ok else None,
        "roll_to_different_expiration": rolls_to_new_expiration,
    }
    prefer_wait = option_type == "call" and _prefer_wait_after_close(posture, replacement, avg_strike)
    if close_triggered:
        if replacement_ok and net_credit_roll and not prefer_wait:
            if rolls_to_new_expiration:
                label = "Buy to close; roll to the richer weekly"
                reason = (
                    f"Short {word}s have captured at least half the original credit. Close them and roll into the "
                    f"{replacement.expiration} weekly — it scored best across the expirations within the 7-day "
                    "ceiling, so the capital re-deploys into a juicier strike/delta instead of a same-week resell."
                )
            else:
                label = "Buy to close; re-sell only if target still clears"
                reason = (
                    f"Short {word}s have captured at least half the original credit. Close them, then re-sell only "
                    "because the reset candidate still clears the optimizer."
                )
            return CoveredCallManagement("buy_to_close_and_resell", label, reason, **common)
        if replacement_ok and not net_credit_roll:
            wait_reason = (
                f"Short {word}s have captured at least half the original credit, but re-selling now would be a "
                "net-debit roll: the replacement's credit does not cover the buy-to-close cost. Close to shed "
                "the gamma/assignment risk and wait for the next weekly to price up before re-selling."
            )
        else:
            wait_reason = (
                f"Short {word}s have captured at least half the original credit, but the replacement setup is "
                "not strong enough or would roll down into an upside-first setup."
            )
        return CoveredCallManagement(
            "buy_to_close_wait",
            "Buy to close; wait before re-selling",
            wait_reason,
            **common,
        )
    return CoveredCallManagement(
        "hold",
        f"Hold current {word}s",
        f"Short {word}s have not captured at least half the original credit yet, so let the premium keep decaying.",
        **common,
    )


def _original_credit_per_share(option) -> float | None:
    value = option.average_cost_basis
    if value is None:
        return None
    return abs(float(value))


def _buyback_mark_per_share(option) -> float | None:
    if option.last_price is not None:
        return abs(float(option.last_price))
    if option.current_value is not None and option.contracts:
        return abs(float(option.current_value)) / (option.contracts * 100)
    return None


def _prefer_wait_after_close(posture: RollPosture, replacement: StrategyCandidateResult, average_strike: float | None) -> bool:
    if replacement.strike is None or average_strike is None:
        return False
    if replacement.strike >= average_strike:
        return False
    base_label = posture.label.split("+", 1)[0].strip()
    return base_label in {"Oversold", "Defensive", "Upside-first"}


def apply_backtest_hint(posture: RollPosture, hint: HistoricalRollBacktestHint | None) -> RollPosture:
    if (
        hint is None
        or not hint.actionable
        or hint.preferred_coverage_pct is None
        or hint.preferred_delta_min is None
        or hint.preferred_delta_max is None
        or hint.preferred_dte_min is None
        or hint.preferred_dte_max is None
    ):
        return posture
    # The backtest now tunes the *coverage* sleeve for this regime (how much of the stack to cap).
    # The strike/delta itself is chosen by best risk-adjusted expected value across the whole chain,
    # not by a band — so the reason talks about coverage, not a delta-band change.
    confidence = hint.status_label.lower() if hint.status_label else "a historical edge"
    if hint.preferred_coverage_pct > posture.coverage_pct:
        cov_shift = "raised"
    elif hint.preferred_coverage_pct < posture.coverage_pct:
        cov_shift = "lowered"
    else:
        cov_shift = "kept"
    reason = (
        f"Base read is {posture.label} ({posture.coverage_pct:.0%} default coverage). This regime's option "
        f"history favored {hint.preferred_coverage_pct:.0%} coverage, so the backtest {cov_shift} the sleeve "
        f"size ({confidence}). The strike/delta is then chosen by best risk-adjusted expected value across the chain."
    )
    # The backtest can tune coverage and delta, but NOT the DTE window — the 7-day ceiling makes its
    # DTE preference (often 10-21d) moot. Keep the base technical weekly window so we don't collapse
    # to a degenerate [7,7] band that rejects the actual next-Friday weekly.
    return RollPosture(
        coverage_pct=hint.preferred_coverage_pct,
        call_delta_min=hint.preferred_delta_min,
        call_delta_max=hint.preferred_delta_max,
        dte_min=min(posture.dte_min, MAX_DTE),
        dte_max=min(posture.dte_max, MAX_DTE),
        label=f"{posture.label} + Backtest",
        reason=reason,
    )


def settings_for_posture(posture: RollPosture, *, coverage_pct: float | None = None) -> OptimizerSettings:
    return OptimizerSettings(
        optioned_pct=coverage_pct if coverage_pct is not None else posture.coverage_pct,
        min_untouched_pct=0.50,
        call_delta_min=posture.call_delta_min,
        call_delta_max=posture.call_delta_max,
        put_delta_min=0.35,
        put_delta_max=0.50,
        dte_min=posture.dte_min,
        dte_max=posture.dte_max,
        min_weekly_premium=25.0,
        objective="Friday roll decision",
    )


def put_settings(posture: RollPosture) -> OptimizerSettings:
    settings = settings_for_posture(posture)
    settings.allow_calls = False
    settings.allow_puts = True
    settings.objective = "assignment re-entry"
    return settings


def assignment_cash(symbol: str, options) -> float:
    cash = 0.0
    for option in options:
        if option.underlying == symbol and option.option_type == "call" and option.side == "short":
            cash += option.contracts * 100 * option.strike
    return cash
