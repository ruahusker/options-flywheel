from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from statistics import mean

from sqlalchemy.orm import Session

from app.models.market_data import OptionPriceBar, PriceHistory
from app.services.indicators import IndicatorResult
from app.services.options_math import black_scholes_merton, implied_volatility


COVERAGE_CHOICES = (0.25, 0.35, 0.50)
DELTA_BANDS = ((0.20, 0.30), (0.25, 0.35), (0.30, 0.40), (0.40, 0.55))
DTE_BANDS = ((5, 10), (11, 21))
MIN_SAMPLES_TO_REPORT = 4
MIN_SAMPLES_TO_APPLY = 12
MIN_FALLBACK_SAMPLES_TO_APPLY = 20
MIN_SCORE_MARGIN_TO_APPLY = 3.0


@dataclass(frozen=True)
class HistoricalRollSample:
    regime: str
    entry_date: date
    expiration: date
    dte: int
    estimated_delta: float
    premium_pct: float
    foregone_pct: float
    net_vs_hold_pct: float
    assigned: bool


@dataclass(frozen=True)
class HistoricalRollBandResult:
    coverage_pct: float
    delta_min: float
    delta_max: float
    dte_min: int
    dte_max: int
    samples: int
    assignment_rate: float
    avg_premium_pct: float
    avg_foregone_pct: float
    avg_net_vs_hold_pct: float
    score: float


@dataclass(frozen=True)
class HistoricalRollBacktestHint:
    symbol: str
    requested_regime: str
    matched_regime: str
    preferred_coverage_pct: float | None
    preferred_delta_min: float | None
    preferred_delta_max: float | None
    preferred_dte_min: int | None
    preferred_dte_max: int | None
    samples: int
    confidence: str
    actionable: bool
    reason: str
    best: HistoricalRollBandResult | None
    static: HistoricalRollBandResult | None
    rows: list[HistoricalRollBandResult]
    warnings: list[str]

    @property
    def status_label(self) -> str:
        if self.actionable:
            return f"Applied, {self.confidence} confidence"
        if self.samples:
            return f"Observed, {self.confidence} confidence"
        return "No usable samples"


def build_historical_roll_backtest(
    db: Session,
    symbol: str,
    indicator: IndicatorResult | None,
    *,
    static_coverage_pct: float,
    static_delta_min: float,
    static_delta_max: float,
    static_dte_min: int,
    static_dte_max: int,
    provider: str = "massive",
) -> HistoricalRollBacktestHint:
    symbol = symbol.upper()
    requested_regime = classify_indicator_regime(indicator)
    samples, warnings = _build_samples(db, symbol, provider=provider)
    regime_samples = [sample for sample in samples if sample.regime == requested_regime]
    matched_regime = requested_regime
    if len(regime_samples) < MIN_SAMPLES_TO_REPORT:
        fallback_samples = samples
        if len(fallback_samples) >= MIN_SAMPLES_TO_REPORT:
            regime_samples = fallback_samples
            matched_regime = "all cached regimes"

    rows = _score_bands(regime_samples)
    rows = sorted(rows, key=lambda row: (row.score, row.samples), reverse=True)
    best = rows[0] if rows else None
    static = _find_static_row(rows, static_coverage_pct, static_delta_min, static_delta_max, static_dte_min, static_dte_max)
    confidence = _confidence(best.samples if best else 0, matched_regime)
    actionable = _is_actionable(best, static, matched_regime)
    reason = _reason(symbol, requested_regime, matched_regime, best, static, actionable, warnings)

    return HistoricalRollBacktestHint(
        symbol=symbol,
        requested_regime=requested_regime,
        matched_regime=matched_regime,
        preferred_coverage_pct=best.coverage_pct if best else None,
        preferred_delta_min=best.delta_min if best else None,
        preferred_delta_max=best.delta_max if best else None,
        preferred_dte_min=best.dte_min if best else None,
        preferred_dte_max=best.dte_max if best else None,
        samples=best.samples if best else 0,
        confidence=confidence,
        actionable=actionable,
        reason=reason,
        best=best,
        static=static,
        rows=rows,
        warnings=warnings,
    )


def classify_indicator_regime(indicator: IndicatorResult | None) -> str:
    if indicator is None:
        return "neutral"
    rsi = indicator.rsi_14
    if rsi is not None and rsi >= 70:
        return "overbought"
    if rsi is not None and rsi <= 35:
        return "oversold"
    if indicator.trend_state in {"bullish breakout", "bullish trend"}:
        return "bullish"
    if indicator.trend_state == "bearish":
        return "bearish"
    return "neutral"


def _build_samples(db: Session, symbol: str, *, provider: str) -> tuple[list[HistoricalRollSample], list[str]]:
    underlying_bars = (
        db.query(PriceHistory.date_time, PriceHistory.close)
        .filter(PriceHistory.provider == provider, PriceHistory.symbol == symbol, PriceHistory.interval == "1d")
        .order_by(PriceHistory.date_time.asc())
        .all()
    )
    if len(underlying_bars) < 60:
        return [], [f"{symbol}: fewer than 60 cached underlying bars; backtest nudge disabled."]

    price_dates = [row.date_time.date() for row in underlying_bars]
    closes = [float(row.close) for row in underlying_bars]
    close_by_date = dict(zip(price_dates, closes, strict=False))
    regimes_by_date = _regimes_by_date(price_dates, closes)
    samples: list[HistoricalRollSample] = []

    option_bars = (
        db.query(OptionPriceBar)
        .filter(
            OptionPriceBar.provider == provider,
            OptionPriceBar.underlying == symbol,
            OptionPriceBar.interval == "1d",
            OptionPriceBar.option_type == "call",
        )
        .order_by(OptionPriceBar.date_time.asc())
        .all()
    )
    for bar in option_bars:
        entry_date = bar.date_time.date()
        dte = (bar.expiration - entry_date).days
        if dte < 4 or dte > 21 or bar.close <= 0:
            continue
        spot = close_by_date.get(entry_date) or _close_on_or_before(entry_date, price_dates, closes)
        expiration_close = _close_on_or_before(bar.expiration, price_dates, closes)
        regime = regimes_by_date.get(entry_date)
        if spot is None or expiration_close is None or regime is None:
            continue
        delta = _estimated_call_delta(spot=spot, strike=bar.strike, dte=dte, option_price=float(bar.close))
        if delta is None or delta < 0.10 or delta > 0.60:
            continue
        premium_pct = float(bar.close) / spot
        foregone_pct = max(expiration_close - bar.strike, 0.0) / spot
        samples.append(
            HistoricalRollSample(
                regime=regime,
                entry_date=entry_date,
                expiration=bar.expiration,
                dte=dte,
                estimated_delta=delta,
                premium_pct=premium_pct,
                foregone_pct=foregone_pct,
                net_vs_hold_pct=premium_pct - foregone_pct,
                assigned=expiration_close > bar.strike,
            )
        )
    warnings = []
    if not samples:
        warnings.append(f"{symbol}: no cached 4-21 DTE call samples with usable option prices.")
    return samples, warnings


def _score_bands(samples: list[HistoricalRollSample]) -> list[HistoricalRollBandResult]:
    rows: list[HistoricalRollBandResult] = []
    for coverage in COVERAGE_CHOICES:
        for delta_min, delta_max in DELTA_BANDS:
            for dte_min, dte_max in DTE_BANDS:
                selected = [
                    sample
                    for sample in samples
                    if delta_min <= sample.estimated_delta <= delta_max and dte_min <= sample.dte <= dte_max
                ]
                if len(selected) < MIN_SAMPLES_TO_REPORT:
                    continue
                avg_premium = mean(sample.premium_pct for sample in selected) * coverage
                avg_foregone = mean(sample.foregone_pct for sample in selected) * coverage
                avg_net = mean(sample.net_vs_hold_pct for sample in selected) * coverage
                assignment_rate = mean(1.0 if sample.assigned else 0.0 for sample in selected)
                score = _score(avg_premium, avg_foregone, avg_net, assignment_rate, len(selected))
                rows.append(
                    HistoricalRollBandResult(
                        coverage_pct=coverage,
                        delta_min=delta_min,
                        delta_max=delta_max,
                        dte_min=dte_min,
                        dte_max=dte_max,
                        samples=len(selected),
                        assignment_rate=assignment_rate,
                        avg_premium_pct=avg_premium,
                        avg_foregone_pct=avg_foregone,
                        avg_net_vs_hold_pct=avg_net,
                        score=score,
                    )
                )
    return rows


def _score(avg_premium: float, avg_foregone: float, avg_net: float, assignment_rate: float, samples: int) -> float:
    sample_quality = min(samples / 30, 1.0) * 6
    return avg_net * 9000 + avg_premium * 3500 - avg_foregone * 2500 - assignment_rate * 8 + sample_quality


def _find_static_row(
    rows: list[HistoricalRollBandResult],
    coverage_pct: float,
    delta_min: float,
    delta_max: float,
    dte_min: int,
    dte_max: int,
) -> HistoricalRollBandResult | None:
    if not rows:
        return None
    return min(
        rows,
        key=lambda row: (
            abs(row.coverage_pct - coverage_pct) * 10
            + abs(row.delta_min - delta_min)
            + abs(row.delta_max - delta_max)
            + abs(row.dte_min - dte_min) / 20
            + abs(row.dte_max - dte_max) / 20
        ),
    )


def _is_actionable(best: HistoricalRollBandResult | None, static: HistoricalRollBandResult | None, matched_regime: str) -> bool:
    if best is None or best.samples < MIN_SAMPLES_TO_APPLY:
        return False
    if matched_regime == "all cached regimes" and best.samples < MIN_FALLBACK_SAMPLES_TO_APPLY:
        return False
    if static is None:
        return True
    return best.score - static.score >= MIN_SCORE_MARGIN_TO_APPLY


def _confidence(samples: int, matched_regime: str) -> str:
    if samples >= 30 and matched_regime != "all cached regimes":
        return "high"
    if samples >= 12:
        return "medium"
    if samples >= MIN_SAMPLES_TO_REPORT:
        return "low"
    return "none"


def _reason(
    symbol: str,
    requested_regime: str,
    matched_regime: str,
    best: HistoricalRollBandResult | None,
    static: HistoricalRollBandResult | None,
    actionable: bool,
    warnings: list[str],
) -> str:
    if best is None:
        return warnings[0] if warnings else f"{symbol}: no usable historical call samples for the {requested_regime} setup yet."
    scope = matched_regime if matched_regime == requested_regime else f"{matched_regime} fallback for {requested_regime}"
    edge = f"{best.coverage_pct:.0%} covered, {best.delta_min:.0%}-{best.delta_max:.0%} delta, {best.dte_min}-{best.dte_max} DTE"
    stats = (
        f"{best.samples} samples, {best.avg_premium_pct:.2%} average premium, "
        f"{best.avg_net_vs_hold_pct:.2%} average net-vs-hold, {best.assignment_rate:.0%} assigned/ITM."
    )
    if actionable:
        return f"Backtest nudge uses {scope}: prefer {edge}. {stats}"
    if static is not None and best.score - static.score < MIN_SCORE_MARGIN_TO_APPLY:
        return f"Backtest observed {scope}, but the edge versus the static rule is too small to override it. Best observed: {edge}. {stats}"
    return f"Backtest observed {scope}, but sample count is too low to override the static rule. Best observed: {edge}. {stats}"


def _regimes_by_date(dates: list[date], closes: list[float]) -> dict[date, str]:
    regimes: dict[date, str] = {}
    for index, close in enumerate(closes):
        if index < 50:
            continue
        rsi = _rsi(closes, index)
        sma20 = sum(closes[index - 19 : index + 1]) / 20
        sma50 = sum(closes[index - 49 : index + 1]) / 50
        high20 = max(closes[index - 19 : index + 1])
        if rsi is not None and rsi >= 70 and close >= high20 * 0.98:
            regimes[dates[index]] = "overbought"
        elif rsi is not None and rsi <= 35:
            regimes[dates[index]] = "oversold"
        elif close > sma20 and close > sma50:
            regimes[dates[index]] = "bullish"
        elif close < sma20 and close < sma50:
            regimes[dates[index]] = "bearish"
        else:
            regimes[dates[index]] = "neutral"
    return regimes


def _rsi(closes: list[float], index: int, period: int = 14) -> float | None:
    if index < period:
        return None
    gains = 0.0
    losses = 0.0
    for cursor in range(index - period + 1, index + 1):
        change = closes[cursor] - closes[cursor - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0 and avg_gain == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _close_on_or_before(target: date, dates: list[date], closes: list[float]) -> float | None:
    index = bisect_right(dates, target) - 1
    if index < 0:
        return None
    return closes[index]


def _estimated_call_delta(spot: float, strike: float, dte: int, option_price: float) -> float | None:
    time_years = max(dte / 365, 1 / 365)
    iv = implied_volatility(option_price, spot, strike, time_years, 0.045, "call")
    if iv is not None:
        try:
            return black_scholes_merton(spot, strike, time_years, 0.045, iv, "call").delta
        except ValueError:
            pass
    moneyness = strike / spot if spot else 999
    if moneyness <= 1:
        return 0.55
    if moneyness <= 1.03:
        return 0.40
    if moneyness <= 1.06:
        return 0.32
    if moneyness <= 1.10:
        return 0.24
    return 0.16
