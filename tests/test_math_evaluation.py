from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.iv_history import IVHistory
from app.schemas.market_data import OptionContractSchema, Quote
from app.services.indicators import IndicatorResult
from app.services.iv_history import iv_rank_for_symbol, record_atm_iv
from app.services.monte_carlo import run_monte_carlo
from app.services.options_math import (
    black_scholes_merton,
    cash_secured_put_breakeven,
    covered_call_breakeven,
    expected_terminal_intrinsic,
    norm_cdf,
    norm_ppf,
    option_sale_edge_per_share,
    risk_neutral_prob_itm,
)
from app.services.sata_projection import project_sata_value
from app.services.scenario_analyzer import analyze_scenario
from app.services.strategy_optimizer import OptimizerSettings, score_candidate


def _make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _indicator(realized_vol_20: float | None, trend: str = "neutral/chop") -> IndicatorResult:
    return IndicatorResult(
        symbol="IBIT", calculated_at=datetime(2026, 6, 1), price=100, sma_5=None, sma_10=None,
        sma_20=None, sma_50=None, ema_8=None, ema_21=None, rsi_14=50, macd_line=None,
        macd_signal=None, macd_histogram=None, bollinger_upper=None, bollinger_middle=None,
        bollinger_lower=None, atr_14=None, realized_vol_10=None, realized_vol_20=realized_vol_20,
        realized_vol_60=None, price_vs_20d_high=None, price_vs_20d_low=None, trend_state=trend,
        recommendation_bias="test", warnings=[],
    )


# ---- S4: risk-neutral P(ITM) via N(d2) is lower than delta (N(d1)) for a call ----
def test_norm_ppf_inverts_norm_cdf():
    for p in (0.05, 0.3, 0.5, 0.7, 0.95):
        assert abs(norm_cdf(norm_ppf(p)) - p) < 1e-6


def test_risk_neutral_prob_itm_below_delta_for_call():
    greeks = black_scholes_merton(100, 105, 30 / 365, 0.04, 0.6, "call")
    prob = risk_neutral_prob_itm(100, 105, 30 / 365, 0.04, 0.6, "call")
    assert prob is not None
    # N(d2) < N(d1) == delta, so using delta as P(ITM) overstates assignment.
    assert 0 < prob < greeks.delta


# ---- S1/S3: edge (premium minus expected payout) is positive only when IV > forecast vol ----
def test_option_sale_edge_positive_when_premium_exceeds_expected_payout():
    # OTM call, low forecast vol -> tiny expected payout -> keeping the premium is +EV.
    edge_rich = option_sale_edge_per_share(100, 110, 7 / 365, 0.30, "call", premium_per_share=1.5, drift=0.04)
    assert edge_rich > 0
    # Same premium, very high forecast vol -> large expected payout -> negative edge.
    edge_cheap = option_sale_edge_per_share(100, 110, 7 / 365, 2.5, "call", premium_per_share=1.5, drift=0.04)
    assert edge_cheap < edge_rich
    assert expected_terminal_intrinsic(100, 110, 7 / 365, 2.5, "call", 0.04) > 0


def test_breakevens():
    assert covered_call_breakeven(100, 2.0) == 98.0
    assert cash_secured_put_breakeven(95, 1.5) == 93.5


# ---- S1/S2: scoring rewards rich IV and flags negative-edge (IV < RV) trades ----
def _call_option(iv: float) -> OptionContractSchema:
    return OptionContractSchema(
        underlying="IBIT", expiration=date(2026, 6, 12), option_type="call", strike=110,
        bid=1.45, ask=1.55, mid=1.50, last=1.50, volume=500, open_interest=1000,
        implied_volatility=iv, delta=0.30, dte=7,
    )


def _score(iv: float, realized_vol_20: float):
    quote = Quote(symbol="IBIT", price=100.0, timestamp=datetime(2026, 6, 1), provider="mock")
    option = _call_option(iv)
    settings = OptimizerSettings()
    fill = 1.50
    return score_candidate(
        symbol="IBIT", action="sell call", contracts=1, option=option, quote=quote,
        fill_price=fill, expected_credit=fill * 100, collateral_required=0.0,
        trend_state="neutral/chop", settings=settings, indicators=_indicator(realized_vol_20),
        iv_rank=None, warnings=[], rejected=False,
    )


def test_rich_iv_scores_higher_than_cheap_iv():
    rich = _score(iv=0.90, realized_vol_20=0.45)   # IV >> RV: positive edge
    cheap = _score(iv=0.30, realized_vol_20=0.90)  # IV << RV: negative edge
    assert rich.expected_value > 0
    assert cheap.expected_value < 0
    assert rich.total_score > cheap.total_score
    assert any("negative edge" in w for w in cheap.warnings)
    assert rich.vrp_ratio > 1 and cheap.vrp_ratio < 1


# ---- S7: scenario sleeve uses full capped participation, no sleeve-delta double-discount ----
def test_scenario_value_independent_of_sleeve_effective_delta():
    a = analyze_scenario("Heavy bullish", 1.5, 1, 100000, 0, 0.5, 5000, 7000, sleeve_effective_delta=0.6)
    b = analyze_scenario("Heavy bullish", 1.5, 1, 100000, 0, 0.5, 5000, 7000, sleeve_effective_delta=0.9)
    assert a.strategy_value == b.strategy_value
    # Optioned sleeve (50k) is capped at +35% in a 1-year heavy bull, untouched (50k) gets full +150%.
    assert abs(a.strategy_value - (50000 * 1.35 + 50000 * 2.5 + 7000)) < 1e-6


# ---- S8: SATA preferred compounds daily at par; tax reduces the result ----
def test_sata_daily_compounding_and_tax():
    base = project_sata_value(10000, 0, 1, annual_rate=0.13)
    # SATA pays daily: (1 + 0.13/365)^365 on 10,000 with no contributions.
    assert abs(base.ending_value - 10000 * (1 + 0.13 / 365) ** 365) < 1e-6
    taxed = project_sata_value(10000, 0, 1, annual_rate=0.13, tax_rate=0.25)
    assert taxed.ending_value < base.ending_value


# ---- S9: Monte Carlo assignment frequency tracks the chosen call delta ----
def test_monte_carlo_assignment_frequency_tracks_delta():
    low = run_monte_carlo(10000, 5000, 2000, 500, years=2, paths=200, call_delta=0.15, seed=1)
    high = run_monte_carlo(10000, 5000, 2000, 500, years=2, paths=200, call_delta=0.45, seed=1)
    # A lower-delta call sits further OTM -> assigned less often.
    assert low.assignment_frequency < high.assignment_frequency


# ---- IV history store + rank ----
def test_iv_history_records_and_ranks():
    session = _make_session()
    chain = [_call_option(iv) for iv in (0.5,)]
    # Seed a trailing range of observations across distinct days.
    for day, iv in enumerate([0.4, 0.5, 0.6, 0.7, 0.8] * 5, start=1):
        session.add(IVHistory(symbol="IBIT", observed_on=date(2026, 1, 1).replace(day=min(day, 28)), atm_iv=iv))
    session.commit()
    # current IV at the top of the [0.4, 0.8] range -> rank near 1.0
    rank = iv_rank_for_symbol(session, "IBIT", current_iv=0.8)
    assert rank is not None and rank > 0.9
    # recording uses the ATM (nearest-strike) call IV
    recorded = record_atm_iv(session, "IBIT", chain, underlying_price=110.0)
    assert recorded == 0.5
