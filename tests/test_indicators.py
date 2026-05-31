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
