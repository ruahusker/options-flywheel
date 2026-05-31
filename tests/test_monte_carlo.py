from app.services.monte_carlo import run_monte_carlo


def test_monte_carlo_output_shape():
    result = run_monte_carlo(10000, 5000, 2000, 500, years=1, paths=100)
    assert result.paths == 100
    assert result.years == 1
    assert result.p5 <= result.median <= result.p95
    assert 0 <= result.win_rate_vs_buy_hold <= 1
    assert 0 <= result.time_in_cash <= 1
