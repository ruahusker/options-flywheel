from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.market_data import OptionPriceBar, PriceHistory
from app.services.historical_roll_backtest import DELTA_BANDS, build_historical_roll_backtest, classify_indicator_regime
from app.services.indicators import IndicatorResult
from app.services.options_math import black_scholes_merton


def test_backtest_hint_prefers_more_coverage_when_historical_calls_worked():
    session = make_session()
    start = date(2026, 1, 1)
    for index in range(90):
        day = start + timedelta(days=index)
        session.add(
            PriceHistory(
                provider="massive",
                symbol="IBIT",
                date_time=datetime.combine(day, datetime.min.time()),
                interval="1d",
                open=100,
                high=101,
                low=99,
                close=100,
                volume=1000,
            )
        )
    option_price = black_scholes_merton(100, 105, 7 / 365, 0.045, 0.60, "call").price
    for offset in range(55, 69):
        entry = start + timedelta(days=offset)
        expiration = entry + timedelta(days=7)
        session.add(
            OptionPriceBar(
                provider="massive",
                option_symbol=f"O:IBIT{expiration:%y%m%d}C00105000{offset}",
                underlying="IBIT",
                expiration=expiration,
                option_type="call",
                strike=105,
                date_time=datetime.combine(entry, datetime.min.time()),
                interval="1d",
                open=option_price,
                high=option_price,
                low=option_price,
                close=option_price,
                volume=100,
            )
        )
    session.commit()

    hint = build_historical_roll_backtest(
        session,
        "IBIT",
        _indicator(rsi=50, trend="neutral/chop"),
        static_coverage_pct=0.35,
        static_delta_min=0.25,
        static_delta_max=0.35,
        static_dte_min=5,
        static_dte_max=10,
    )

    assert hint.actionable is True
    assert hint.preferred_coverage_pct == 0.50
    assert hint.samples == 14
    assert hint.best is not None
    assert hint.best.avg_net_vs_hold_pct > 0


def test_indicator_regime_matches_roll_language():
    assert classify_indicator_regime(_indicator(rsi=74, trend="bullish trend")) == "overbought"
    assert classify_indicator_regime(_indicator(rsi=31, trend="bearish")) == "oversold"
    assert classify_indicator_regime(_indicator(rsi=55, trend="bullish breakout")) == "bullish"
    assert classify_indicator_regime(_indicator(rsi=55, trend="bearish")) == "bearish"


def test_backtest_candidate_bands_allow_55_delta_calls():
    assert (0.40, 0.55) in DELTA_BANDS


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _indicator(rsi: float, trend: str) -> IndicatorResult:
    return IndicatorResult(
        symbol="IBIT",
        calculated_at=datetime(2026, 6, 1),
        price=100,
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
        price_vs_20d_high=None,
        price_vs_20d_low=None,
        trend_state=trend,
        recommendation_bias="test",
        warnings=[],
    )


def test_coverage_scoring_prefers_middle_sleeve_when_upside_cost_is_material():
    # Premium 1.9% with 1.2% average foregone upside: positive net, but capping more of the
    # stack costs quadratically. The old linear score always saturated at 50% coverage on any
    # positive net; the risk-adjusted score should land on the 35% sleeve here.
    from app.services.historical_roll_backtest import HistoricalRollSample, _score_bands

    samples = [
        HistoricalRollSample(
            regime="neutral",
            entry_date=date(2026, 3, 1) + timedelta(days=i),
            expiration=date(2026, 3, 7) + timedelta(days=i),
            dte=6,
            estimated_delta=0.32,
            premium_pct=0.019,
            foregone_pct=0.012,
            net_vs_hold_pct=0.007,
            assigned=True,
            option_symbol=f"O:IBIT_TEST{i}",
        )
        for i in range(14)
    ]
    rows = _score_bands(samples)
    assert rows
    best = max(rows, key=lambda row: row.score)
    assert best.coverage_pct == 0.35
    assert best.distinct_contracts == 14


def test_unsettled_contracts_are_not_scored():
    # A contract expiring beyond the last cached close has no settled outcome; it must not be
    # marked at today's price and counted as a finished trade.
    session = make_session()
    start = date(2026, 1, 1)
    for index in range(90):
        day = start + timedelta(days=index)
        session.add(
            PriceHistory(
                provider="massive", symbol="IBIT", interval="1d",
                date_time=datetime.combine(day, datetime.min.time()),
                open=100, high=101, low=99, close=100, volume=1000,
            )
        )
    entry = start + timedelta(days=88)
    expiration = start + timedelta(days=95)  # beyond the last cached close (day 89)
    session.add(
        OptionPriceBar(
            provider="massive", option_symbol="O:IBIT_OPEN", underlying="IBIT",
            expiration=expiration, option_type="call", strike=105,
            date_time=datetime.combine(entry, datetime.min.time()), interval="1d",
            open=1.0, high=1.0, low=1.0, close=1.0, volume=100,
        )
    )
    session.commit()

    hint = build_historical_roll_backtest(
        session, "IBIT", _indicator(rsi=50, trend="neutral/chop"),
        static_coverage_pct=0.35, static_delta_min=0.25, static_delta_max=0.35,
        static_dte_min=5, static_dte_max=7,
    )

    assert hint.samples == 0
    assert hint.actionable is False
