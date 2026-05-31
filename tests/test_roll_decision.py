from __future__ import annotations

from datetime import date, datetime

from app.services.historical_roll_backtest import HistoricalRollBacktestHint, HistoricalRollBandResult
from app.services.indicators import IndicatorResult
from app.services.roll_decision import (
    RollPosture,
    apply_backtest_hint,
    choose_roll_expiration,
    offband_delta_note,
    recommend_roll_posture,
)
from app.services.strategy_optimizer import StrategyCandidateResult


def _call_candidate(*, action: str = "sell call", delta: float | None = 0.35, strike: float | None = 19.0) -> StrategyCandidateResult:
    return StrategyCandidateResult(
        symbol="ASST",
        action=action,
        contracts=15,
        expiration=date(2026, 6, 5),
        strike=strike,
        option_type="call" if action == "sell call" else None,
        side="short" if action == "sell call" else None,
        delta=delta,
        bid=None,
        ask=None,
        mid=None,
        expected_credit=1000.0 if action == "sell call" else 0.0,
        collateral_required=0.0,
        premium_yield_weekly=0.05,
        premium_yield_annualized=2.6,
        assignment_probability_proxy=0.4,
        upside_cap=strike,
        upside_preserved_score=50.0,
        liquidity_score=80.0,
        trend_alignment_score=70.0,
        iv_score=60.0,
        scenario_score=50.0,
        total_score=58.0,
        reason="test",
    )


_BAND = RollPosture(0.50, 0.30, 0.40, 5, 7, "Balanced", "test")


def test_offband_note_flags_itm_fallback():
    note = offband_delta_note(_call_candidate(delta=0.65, strike=17.0), _BAND, quote_price=17.67)
    assert note is not None
    assert "in-the-money" in note
    assert "0.65" in note


def test_offband_note_silent_when_delta_in_band():
    assert offband_delta_note(_call_candidate(delta=0.35, strike=19.0), _BAND, quote_price=17.67) is None


def test_offband_note_silent_for_skip_trade():
    assert offband_delta_note(_call_candidate(action="skip trade", delta=None, strike=None), _BAND, quote_price=17.67) is None


def test_offband_note_omits_itm_wording_when_strike_above_spot():
    note = offband_delta_note(_call_candidate(delta=0.55, strike=20.0), _BAND, quote_price=17.67)
    assert note is not None
    assert "in-the-money" not in note


def test_choose_roll_expiration_prefers_target_dte_window():
    # June 9 is 6 DTE (in band and within the 7-day ceiling); June 16 is 13 DTE (excluded).
    expirations = [date(2026, 6, 9), date(2026, 6, 16)]

    selected = choose_roll_expiration(expirations, today=date(2026, 6, 3), dte_min=5, dte_max=10)

    assert selected == date(2026, 6, 9)


def test_choose_roll_expiration_never_exceeds_7_day_ceiling():
    # Only expirations more than 7 days out exist -> do not sell (None), even with a wide dte_max.
    expirations = [date(2026, 6, 12), date(2026, 6, 19)]  # 9 and 16 DTE from June 3
    assert choose_roll_expiration(expirations, today=date(2026, 6, 3), dte_min=5, dte_max=21) is None

    # A near expiration below dte_min but within the ceiling is still allowed (take the furthest <=7).
    expirations = [date(2026, 6, 5), date(2026, 6, 9)]  # 2 and 6 DTE
    assert choose_roll_expiration(expirations, today=date(2026, 6, 3), dte_min=5, dte_max=7) == date(2026, 6, 9)


def test_roll_posture_increases_coverage_when_overbought():
    posture = recommend_roll_posture(_indicator(rsi=74, trend="bullish trend", price_vs_high=-0.01, price_vs_low=0.20))

    assert posture.coverage_pct == 0.50
    assert posture.call_delta_min == 0.30


def test_roll_posture_preserves_upside_when_oversold():
    posture = recommend_roll_posture(_indicator(rsi=31, trend="bearish", price_vs_high=-0.20, price_vs_low=0.01))

    assert posture.coverage_pct == 0.25
    assert posture.call_delta_max == 0.30


def test_backtest_hint_can_adjust_roll_posture():
    posture = recommend_roll_posture(_indicator(rsi=55, trend="neutral/chop", price_vs_high=-0.10, price_vs_low=0.10))
    hint = HistoricalRollBacktestHint(
        symbol="IBIT",
        requested_regime="neutral",
        matched_regime="neutral",
        preferred_coverage_pct=0.50,
        preferred_delta_min=0.30,
        preferred_delta_max=0.40,
        preferred_dte_min=11,
        preferred_dte_max=21,
        samples=18,
        confidence="medium",
        actionable=True,
        reason="test nudge",
        best=HistoricalRollBandResult(0.50, 0.30, 0.40, 11, 21, 18, 0.1, 0.02, 0.0, 0.02, 20),
        static=None,
        rows=[],
        warnings=[],
    )

    adjusted = apply_backtest_hint(posture, hint)

    assert adjusted.coverage_pct == 0.50
    assert adjusted.call_delta_min == 0.30
    # The backtest tunes coverage/delta but NOT DTE: the base weekly window (5-7) is kept, capped at
    # the 7-day ceiling. The hint's 11-21 DTE is ignored (meaningless under the ceiling).
    assert adjusted.dte_min == 5
    assert adjusted.dte_max == 7
    assert "Backtest" in adjusted.label


def _indicator(rsi: float, trend: str, price_vs_high: float, price_vs_low: float) -> IndicatorResult:
    return IndicatorResult(
        symbol="IBIT",
        calculated_at=datetime(2026, 6, 1),
        price=42.0,
        sma_5=None,
        sma_10=None,
        sma_20=None,
        sma_50=None,
        ema_8=None,
        ema_21=None,
        rsi_14=rsi,
        macd_line=None,
        macd_signal=None,
        macd_histogram=None,
        bollinger_upper=None,
        bollinger_middle=None,
        bollinger_lower=None,
        atr_14=None,
        realized_vol_10=None,
        realized_vol_20=None,
        realized_vol_60=None,
        price_vs_20d_high=price_vs_high,
        price_vs_20d_low=price_vs_low,
        trend_state=trend,
        recommendation_bias="test",
        warnings=[],
    )
