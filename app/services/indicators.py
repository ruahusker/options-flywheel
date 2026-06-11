from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from app.schemas.market_data import Bar


@dataclass
class IndicatorResult:
    symbol: str
    calculated_at: datetime
    price: float | None
    sma_5: float | None
    sma_10: float | None
    sma_20: float | None
    sma_50: float | None
    ema_8: float | None
    ema_21: float | None
    rsi_14: float | None
    macd_line: float | None
    macd_signal: float | None
    macd_histogram: float | None
    bollinger_upper: float | None
    bollinger_middle: float | None
    bollinger_lower: float | None
    atr_14: float | None
    realized_vol_10: float | None
    realized_vol_20: float | None
    realized_vol_60: float | None
    price_vs_20d_high: float | None
    price_vs_20d_low: float | None
    trend_state: str
    recommendation_bias: str
    warnings: list[str]


def _last(series: pd.Series) -> float | None:
    if series.empty:
        return None
    value = series.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


def calculate_indicators(symbol: str, bars: list[Bar]) -> IndicatorResult:
    warnings: list[str] = []
    if not bars:
        return IndicatorResult(
            symbol=symbol,
            calculated_at=datetime.utcnow(),
            price=None,
            sma_5=None, sma_10=None, sma_20=None, sma_50=None,
            ema_8=None, ema_21=None,
            rsi_14=None,
            macd_line=None, macd_signal=None, macd_histogram=None,
            bollinger_upper=None, bollinger_middle=None, bollinger_lower=None,
            atr_14=None,
            realized_vol_10=None, realized_vol_20=None, realized_vol_60=None,
            price_vs_20d_high=None, price_vs_20d_low=None,
            trend_state="unknown",
            recommendation_bias="insufficient data",
            warnings=["No bars available"],
        )

    data = pd.DataFrame([bar.model_dump() if hasattr(bar, "model_dump") else bar.dict() for bar in bars])
    data = data.sort_values("date_time")
    close = data["close"].astype(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    price = float(close.iloc[-1])
    if len(data) < 50:
        warnings.append("Fewer than 50 bars; long moving averages may be incomplete")

    sma_5 = close.rolling(5).mean()
    sma_10 = close.rolling(10).mean()
    sma_20 = close.rolling(20).mean()
    sma_50 = close.rolling(50).mean()
    ema_8 = close.ewm(span=8, adjust=False).mean()
    ema_21 = close.ewm(span=21, adjust=False).mean()

    delta = close.diff()
    # Wilder's RSI: smooth average gain/loss with an exponential (alpha = 1/period), which is the
    # canonical definition the overbought/oversold thresholds (70/35) are calibrated against.
    gains = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gains / losses.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.mask((losses == 0) & (gains > 0), 100.0)
    rsi = rsi.mask((losses == 0) & (gains == 0), 50.0)

    ema_12 = close.ewm(span=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema_12 - ema_26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal

    bb_mid = sma_20
    bb_std = close.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_14 = tr.rolling(14).mean()

    returns = close.pct_change()
    realized_vol_10 = returns.rolling(10).std() * np.sqrt(252)
    realized_vol_20 = returns.rolling(20).std() * np.sqrt(252)
    realized_vol_60 = returns.rolling(60).std() * np.sqrt(252)

    high_20 = high.rolling(20).max()
    low_20 = low.rolling(20).min()
    p_vs_high = (price / high_20.iloc[-1] - 1) if len(high_20) and not pd.isna(high_20.iloc[-1]) else None
    p_vs_low = (price / low_20.iloc[-1] - 1) if len(low_20) and not pd.isna(low_20.iloc[-1]) else None

    trend_state, bias = classify_trend(
        price=price,
        sma20=_last(sma_20),
        sma50=_last(sma_50),
        ema8=_last(ema_8),
        ema21=_last(ema_21),
        rsi=_last(rsi),
        macd=_last(macd_line),
        macd_signal=_last(macd_signal),
        price_vs_20d_high=p_vs_high,
    )

    return IndicatorResult(
        symbol=symbol,
        calculated_at=datetime.utcnow(),
        price=price,
        sma_5=_last(sma_5),
        sma_10=_last(sma_10),
        sma_20=_last(sma_20),
        sma_50=_last(sma_50),
        ema_8=_last(ema_8),
        ema_21=_last(ema_21),
        rsi_14=_last(rsi),
        macd_line=_last(macd_line),
        macd_signal=_last(macd_signal),
        macd_histogram=_last(macd_hist),
        bollinger_upper=_last(bb_upper),
        bollinger_middle=_last(bb_mid),
        bollinger_lower=_last(bb_lower),
        atr_14=_last(atr_14),
        realized_vol_10=_last(realized_vol_10),
        realized_vol_20=_last(realized_vol_20),
        realized_vol_60=_last(realized_vol_60),
        price_vs_20d_high=float(p_vs_high) if p_vs_high is not None else None,
        price_vs_20d_low=float(p_vs_low) if p_vs_low is not None else None,
        trend_state=trend_state,
        recommendation_bias=bias,
        warnings=warnings,
    )


def classify_trend(
    price: float,
    sma20: float | None,
    sma50: float | None,
    ema8: float | None,
    ema21: float | None,
    rsi: float | None,
    macd: float | None,
    macd_signal: float | None,
    price_vs_20d_high: float | None,
) -> tuple[str, str]:
    if None in {sma20, sma50, ema8, ema21, rsi, macd, macd_signal}:
        return "unknown", "insufficient data"
    assert sma20 is not None and sma50 is not None and ema8 is not None and ema21 is not None
    assert rsi is not None and macd is not None and macd_signal is not None

    near_high = price_vs_20d_high is not None and price_vs_20d_high > -0.02
    above_trend = price > sma20 and price > sma50 and ema8 > ema21
    macd_positive = macd > macd_signal

    if above_trend and macd_positive and near_high and 50 <= rsi <= 68:
        return "bullish breakout", "preserve upside; penalize aggressive calls"
    # RSI band runs to (not past) 70 so a strong uptrend with RSI 66-69 stays classified as bullish
    # instead of falling through to neutral/chop and over-covering right when momentum is strongest.
    if above_trend and macd_positive and 50 <= rsi < 70:
        return "bullish trend", "prefer lower-delta calls"
    if above_trend and rsi >= 70:
        return "bullish trend", "price extended; 35-40 delta calls allowed"
    if price < sma20 and price < sma50:
        return "bearish", "avoid forced calls; puts only with re-entry plan"
    if macd < macd_signal and rsi > 60:
        return "weakening", "increase call aggressiveness modestly"
    return "neutral/chop", "favor balanced premium selling"
