from app.services.scenario_analyzer import analyze_scenario


def test_heavy_bull_warning_when_strategy_underperforms():
    result = analyze_scenario("Heavy bullish", 1.5, 1, 100000, 0, 0.5, 5000, 7000, 0.6)
    assert result.buy_hold_value > result.strategy_value
    assert result.warning


def test_bearish_can_outperform_buy_hold_while_still_down():
    result = analyze_scenario("Bearish", -0.2, 1, 100000, 0, 0.35, 8000, 9000, 0.65)
    assert result.net_vs_buy_hold > 0
