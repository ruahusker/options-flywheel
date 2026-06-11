from datetime import date, datetime

from app.schemas.market_data import OptionContractSchema, Quote
from app.services.strategy_optimizer import OptimizerSettings, optioned_contracts, rank_candidates


def test_optioned_contracts_rounds_down_to_100_share_lots():
    assert optioned_contracts(1530.849, 0.35) == 5


def test_optimizer_does_not_force_bad_trade():
    quote = Quote(symbol="IBIT", price=41.63, timestamp=datetime.utcnow(), provider="mock")
    chain = [
        OptionContractSchema(
            underlying="IBIT",
            expiration=date(2026, 6, 5),
            option_type="call",
            strike=43.5,
            bid=0.01,
            ask=1.00,
            mid=0.505,
            open_interest=1,
            volume=0,
            delta=0.35,
            dte=7,
        )
    ]
    candidates = rank_candidates("IBIT", 1530.849, 0, quote, chain, None, OptimizerSettings(min_weekly_premium=1000))
    assert candidates[0].action == "skip trade"


def test_optimizer_does_not_add_calls_when_existing_short_calls_exceed_target():
    quote = Quote(symbol="IBIT", price=41.63, timestamp=datetime.utcnow(), provider="yahoo")
    chain = [
        OptionContractSchema(
            underlying="IBIT",
            expiration=date(2026, 6, 5),
            option_type="call",
            strike=43.5,
            bid=0.65,
            ask=0.75,
            mid=0.70,
            open_interest=1000,
            volume=100,
            delta=0.32,
            dte=6,
        )
    ]
    candidates = rank_candidates(
        "IBIT",
        1530.849,
        0,
        quote,
        chain,
        None,
        OptimizerSettings(optioned_pct=0.35),
        existing_short_call_contracts=15,
    )
    assert candidates[0].action == "skip trade"
    assert "Existing short calls" in candidates[0].reason


def test_ev_score_stays_neutral_without_any_vol_forecast():
    # No indicators and no IV on the contract: the edge is unknowable, so the EV score must be
    # the neutral 50 — not 100 (which previously treated the whole premium as pure edge).
    quote = Quote(symbol="IBIT", price=41.63, timestamp=datetime.utcnow(), provider="mock")
    chain = [
        OptionContractSchema(
            underlying="IBIT",
            expiration=date(2026, 6, 5),
            option_type="call",
            strike=43.5,
            bid=0.65,
            ask=0.75,
            mid=0.70,
            open_interest=1000,
            volume=100,
            delta=0.32,
            implied_volatility=None,
            dte=6,
        )
    ]
    candidates = rank_candidates("IBIT", 1530.849, 0, quote, chain, None, OptimizerSettings(optioned_pct=0.35))
    best = candidates[0]
    assert best.action == "sell call"
    assert best.ev_score == 50.0
    assert best.expected_value == 0.0
    assert best.expected_value_annualized_yield == 0.0
