from app.services.options_math import (
    call_spread_max_give_up,
    call_spread_payoff,
    cash_secured_put_payoff,
    covered_call_payoff,
    true_strategy_value,
)


def test_payoff_math():
    assert covered_call_payoff(50, 45, 1, 200) == -300
    assert cash_secured_put_payoff(35, 40, 1, 150) == -350
    assert call_spread_payoff(50, 45, 55, 1, 120) == -380
    assert call_spread_max_give_up(45, 55, 1, 120) == 880


def test_true_strategy_value_marks_liability():
    assert true_strategy_value(1000, 100, 50, 25, 75, 20) == 1120
