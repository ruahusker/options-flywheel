from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.schemas.market_data import OptionContractSchema, Quote
from app.services.historical_roll_backtest import HistoricalRollBacktestHint, HistoricalRollBandResult
from app.services.indicators import IndicatorResult
from app.services.roll_decision import (
    RollPosture,
    apply_backtest_hint,
    best_roll_recommendation,
    choose_roll_expiration,
    covered_call_management,
    offband_delta_note,
    recommend_roll_posture,
)
from app.services.strategy_optimizer import StrategyCandidateResult


def _call_candidate(*, action: str = "sell call", delta: float | None = 0.35, strike: float | None = 19.0, expiration: date = date(2026, 6, 5)) -> StrategyCandidateResult:
    return StrategyCandidateResult(
        symbol="ASST",
        action=action,
        contracts=15,
        expiration=expiration,
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


def test_covered_call_management_recommends_close_and_resell_at_60_pct_capture():
    management = covered_call_management(
        symbol="IBIT",
        options=[_short_call(avg_credit=1.00, mark=0.35, strike=45.0)],
        replacement=_call_candidate(strike=47.0),
        posture=RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "test"),
    )

    assert management.action == "buy_to_close_and_resell"
    assert round(management.captured_pct or 0, 2) == 0.65
    assert management.open_profit == 65.0


def test_covered_call_management_triggers_close_at_50_pct_capture():
    # At exactly 50% captured the take-profit trigger now fires (previously it waited for 60%).
    management = covered_call_management(
        symbol="IBIT",
        options=[_short_call(avg_credit=1.00, mark=0.50, strike=45.0)],
        replacement=_call_candidate(strike=47.0),
        posture=RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "test"),
    )

    assert management.action == "buy_to_close_and_resell"
    assert round(management.captured_pct or 0, 2) == 0.50


def test_covered_call_management_holds_below_50_pct_capture():
    management = covered_call_management(
        symbol="IBIT",
        options=[_short_call(avg_credit=1.00, mark=0.55, strike=45.0)],
        replacement=_call_candidate(strike=47.0),
        posture=RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "test"),
    )

    assert management.action == "hold"
    assert round(management.captured_pct or 0, 2) == 0.45


def test_covered_call_management_frames_calendar_roll_to_new_expiration():
    # Held calls expire 2026-06-05; the best replacement sits on a shorter 2026-06-03 weekly -> roll.
    management = covered_call_management(
        symbol="IBIT",
        options=[_short_call(avg_credit=1.00, mark=0.40, strike=45.0)],
        replacement=_call_candidate(strike=47.0, expiration=date(2026, 6, 3)),
        posture=RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "test"),
    )

    assert management.action == "buy_to_close_and_resell"
    assert management.roll_to_different_expiration is True
    assert "roll" in management.label.lower()


def test_covered_call_management_closes_but_waits_when_replacement_rolls_down_in_oversold_setup():
    management = covered_call_management(
        symbol="IBIT",
        options=[_short_call(avg_credit=1.00, mark=0.30, strike=45.0)],
        replacement=_call_candidate(strike=43.0),
        posture=RollPosture(0.25, 0.20, 0.30, 5, 7, "Oversold", "test"),
    )

    assert management.action == "buy_to_close_wait"
    assert "replacement setup" in management.reason


def test_covered_call_management_unavailable_without_original_credit():
    management = covered_call_management(
        symbol="IBIT",
        options=[_short_call(avg_credit=None, mark=0.30, strike=45.0)],
        replacement=_call_candidate(strike=47.0),
        posture=RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "test"),
    )

    assert management.action == "unavailable"


def _chain_call(*, expiration: date, dte: int, mid: float, strike: float = 43.0) -> OptionContractSchema:
    return OptionContractSchema(
        underlying="IBIT",
        expiration=expiration,
        option_type="call",
        strike=strike,
        bid=mid - 0.02,
        ask=mid + 0.02,
        mid=mid,
        last=mid,
        volume=500,
        open_interest=2000,
        delta=0.30,
        implied_volatility=0.50,
        dte=dte,
    )


class _FakeProvider:
    def __init__(self, chains: dict):
        self._chains = chains

    def get_option_chain(self, symbol: str, expiration: date):
        return self._chains.get(expiration, [])


def test_best_roll_recommendation_picks_juicy_short_dated_weekly():
    today = date(2026, 6, 1)
    short_exp = date(2026, 6, 4)   # 3 DTE
    standard_exp = date(2026, 6, 8)  # 7 DTE
    provider = _FakeProvider(
        {
            short_exp: [_chain_call(expiration=short_exp, dte=3, mid=0.95)],   # richer premium
            standard_exp: [_chain_call(expiration=standard_exp, dte=7, mid=0.30)],
        }
    )
    quote = Quote(symbol="IBIT", price=42.0, timestamp=datetime.utcnow(), provider="mock")
    indicator = _indicator(rsi=55, trend="neutral/chop", price_vs_high=-0.10, price_vs_low=0.10)

    best, best_exp = best_roll_recommendation(
        symbol="IBIT",
        shares=1500,
        available_cash=0.0,
        quote=quote,
        indicator=indicator,
        posture=_BAND,
        provider=provider,
        expirations=[short_exp, standard_exp],
        today=today,
        existing_short_call_contracts=0,
        iv_rank=None,
    )

    assert best is not None
    assert best_exp == short_exp
    assert best.action == "sell call"


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


def _short_call(avg_credit: float | None, mark: float | None, strike: float):
    return SimpleNamespace(
        underlying="IBIT",
        option_type="call",
        side="short",
        contracts=1,
        expiration=date(2026, 6, 5),
        strike=strike,
        average_cost_basis=avg_credit,
        last_price=mark,
        current_value=-(mark or 0) * 100 if mark is not None else None,
    )


def test_covered_call_management_waits_on_net_debit_roll():
    # Capture trigger fires (55%), but the replacement only brings in ~$0.67/share while closing
    # costs $0.90/share — a net-debit roll. Close to shed risk, but do NOT re-sell yet.
    management = covered_call_management(
        symbol="IBIT",
        options=[_short_call(avg_credit=2.00, mark=0.90, strike=45.0)],
        replacement=_call_candidate(strike=47.0),
        posture=RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "test"),
    )

    assert management.action == "buy_to_close_wait"
    assert "net-debit" in management.reason


def _put_candidate(*, strike: float = 40.0, expiration: date = date(2026, 6, 5)) -> StrategyCandidateResult:
    candidate = _call_candidate(strike=strike, expiration=expiration)
    candidate.action = "sell put"
    candidate.option_type = "put"
    candidate.side = "short"
    return candidate


def _short_put(avg_credit: float | None, mark: float | None, strike: float, contracts: int = 1):
    leg = _short_call(avg_credit=avg_credit, mark=mark, strike=strike)
    leg.option_type = "put"
    leg.contracts = contracts
    return leg


def test_short_put_management_triggers_close_and_resell_at_capture():
    from app.services.roll_decision import short_put_management

    management = short_put_management(
        symbol="IBIT",
        options=[_short_put(avg_credit=1.00, mark=0.40, strike=40.0)],
        replacement=_put_candidate(strike=40.0),
        posture=RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "test"),
    )

    assert management.action == "buy_to_close_and_resell"
    assert "put" in management.reason
    assert round(management.captured_pct or 0, 2) == 0.60


def test_short_put_management_holds_below_capture():
    from app.services.roll_decision import short_put_management

    management = short_put_management(
        symbol="IBIT",
        options=[_short_put(avg_credit=1.00, mark=0.80, strike=40.0)],
        replacement=_put_candidate(strike=40.0),
        posture=RollPosture(0.35, 0.25, 0.35, 5, 7, "Balanced", "test"),
    )

    assert management.action == "hold"
    assert "put" in management.label.lower()


class _WheelProvider:
    """Provider stub for a symbol fully in the wheel's cash phase (no shares, short puts)."""

    def __init__(self, chain):
        self._chain = chain

    def get_quote(self, symbol):
        return Quote(symbol=symbol, price=42.0, timestamp=datetime.utcnow(), provider="mock")

    def get_price_history(self, symbol, periods, interval):
        return []

    def get_option_expirations(self, symbol):
        return [date.today() + timedelta(days=5)]

    def get_option_chain(self, symbol, expiration):
        return self._chain


def test_roll_rows_include_symbol_with_only_short_puts():
    from app.services.roll_decision import build_roll_decision_rows, week_verdict

    exp = date.today() + timedelta(days=5)
    chain = [
        OptionContractSchema(
            underlying="IBIT", expiration=exp, option_type="put", strike=40.0,
            bid=0.48, ask=0.52, mid=0.50, volume=400, open_interest=1500,
            delta=-0.40, implied_volatility=0.55, dte=5,
        ),
    ]
    metrics = SimpleNamespace(
        shares_by_symbol={"IBIT": 0.0},
        option_exposure={"IBIT": {"short_calls": 0, "short_puts": 2}},
        cash_value=10000.0,
        pending_activity=0.0,
    )
    puts = [
        SimpleNamespace(
            underlying="IBIT", option_type="put", side="short", contracts=2,
            expiration=exp, strike=40.0, average_cost_basis=1.00,
            last_price=0.80, current_value=-160.0,
        )
    ]

    rows, warnings = build_roll_decision_rows(metrics, puts, _WheelProvider(chain), db=None)

    assert warnings == []
    assert len(rows) == 1
    row = rows[0]
    assert row.existing_short_puts == 2
    assert row.put_collateral == 8000.0
    assert row.put_management is not None
    assert row.put_management.action == "hold"  # only 20% of the credit captured
    assert row.recurring_weekly_premium > 0  # put rolls keep funding the SATA routing
    verdict = week_verdict(row)
    assert verdict.headline == "Hold — puts working"
