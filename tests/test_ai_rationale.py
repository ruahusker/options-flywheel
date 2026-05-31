from datetime import date
from types import SimpleNamespace

from app.services.ai_rationale import generate_rationale
from app.services.risk_engine import DashboardMetrics
from app.services.strategy_optimizer import StrategyCandidateResult


def test_ai_rationale_fallback_without_openai_key(monkeypatch):
    monkeypatch.setattr(
        "app.services.minimax_client.settings",
        SimpleNamespace(minimax_api_key=None),
    )
    monkeypatch.setattr(
        "app.services.kimi_client.settings",
        SimpleNamespace(kimi_api_key=None),
    )
    monkeypatch.setattr(
        "app.services.ai_rationale.settings",
        SimpleNamespace(
            kimi_api_key=None,
            kimi_model="kimi-test",
            minimax_api_key=None,
            minimax_model="minimax-test",
            openai_api_key=None,
            ai_rationale_model="test-model",
            ai_rationale_timeout_seconds=1,
        ),
    )
    candidate = StrategyCandidateResult(
        symbol="IBIT",
        action="sell call",
        contracts=5,
        expiration=date(2026, 6, 5),
        strike=44.5,
        option_type="call",
        side="short",
        delta=0.28,
        bid=0.58,
        ask=0.65,
        mid=0.615,
        expected_credit=307.50,
        collateral_required=0.0,
        premium_yield_weekly=0.01,
        premium_yield_annualized=0.52,
        assignment_probability_proxy=0.28,
        upside_cap=44.5,
        upside_preserved_score=80,
        liquidity_score=84,
        trend_alignment_score=80,
        iv_score=70,
        scenario_score=72,
        total_score=71,
        reason="Balanced candidate.",
        warnings=[],
    )
    metrics = DashboardMetrics(
        total_account_value=100000,
        long_position_value=90000,
        sata_value=500,
        cash_value=1000,
        pending_activity=0,
        short_option_liability=100,
        long_option_value=0,
        true_strategy_value=91400,
        net_sidecar_value=1400,
        cash_collateral=0,
        estimated_weekly_premium=0,
        estimated_annual_premium=0,
        shares_by_symbol={"IBIT": 1530.849},
        values_by_symbol={"IBIT": 63729.24},
        option_exposure={"IBIT": {"optioned_percentage": 0.35}},
        warnings=[],
    )
    result = generate_rationale("IBIT", candidate, [], None, metrics, 0.35, "balanced")
    assert not result.used_ai
    assert "Recommendation: IBIT" in result.text
    assert "Buy-and-hold risk" in result.text
