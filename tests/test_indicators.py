from datetime import datetime, timedelta

from app.schemas.market_data import Bar
from app.services.indicators import calculate_indicators


def test_rsi_is_100_when_lookback_has_no_losses():
    start = datetime(2026, 1, 1)
    bars = [
        Bar(
            symbol="TEST",
            date_time=start + timedelta(days=index),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100 + index,
            volume=1000,
        )
        for index in range(60)
    ]

    result = calculate_indicators("TEST", bars)

    assert result.rsi_14 == 100.0


def test_uptrend_with_rsi_between_65_and_70_is_still_bullish():
    from app.services.indicators import classify_trend

    trend, _ = classify_trend(
        price=100.0,
        sma20=95.0,
        sma50=90.0,
        ema8=99.0,
        ema21=97.0,
        rsi=67.0,
        macd=1.0,
        macd_signal=0.5,
        price_vs_20d_high=-0.05,
    )
    assert trend == "bullish trend"
