from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.services.massive_client import MassiveClient, MassiveRateLimiter


def test_massive_rate_limiter_enforces_minimum_spacing():
    now = [100.0]
    sleeps: list[float] = []

    def sleep_func(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = MassiveRateLimiter(calls_per_minute=5, clock=lambda: now[0], sleep_func=sleep_func)

    limiter.wait()
    limiter.wait()

    assert sleeps == pytest.approx([12.0])


def test_massive_client_parses_contract_pages_and_bars():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        if request.url.path == "/v3/reference/options/contracts":
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        {
                            "ticker": "O:IBIT260605C00045000",
                            "underlying_ticker": "IBIT",
                            "expiration_date": "2026-06-05",
                            "contract_type": "call",
                            "strike_price": 45,
                            "shares_per_contract": 100,
                            "exercise_style": "american",
                        },
                        {
                            "ticker": "O:A260605C00045000",
                            "underlying_ticker": "A",
                            "expiration_date": "2026-06-05",
                            "contract_type": "call",
                            "strike_price": 45,
                            "shares_per_contract": 100,
                            "exercise_style": "american",
                        }
                    ],
                },
            )
        if request.url.path == "/v2/aggs/ticker/O:IBIT260605C00045000/range/1/day/2026-06-01/2026-06-05":
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "results": [
                        {
                            "t": 1780272000000,
                            "o": 1.2,
                            "h": 1.4,
                            "l": 1.0,
                            "c": 1.3,
                            "v": 12,
                            "vw": 1.25,
                            "n": 4,
                        }
                    ],
                },
            )
        return httpx.Response(404)

    client = MassiveClient(
        "test-key",
        base_url="https://api.massive.test",
        rate_limiter=MassiveRateLimiter(calls_per_minute=999999),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    pages = list(client.iter_option_contract_pages("IBIT", expiration_gte=date(2026, 6, 1), expiration_lte=date(2026, 6, 30)))
    bars = client.get_option_bars("O:IBIT260605C00045000", date(2026, 6, 1), date(2026, 6, 5))

    assert len(pages) == 1
    assert len(pages[0]) == 1
    assert pages[0][0].provider_symbol == "O:IBIT260605C00045000"
    assert pages[0][0].expiration == date(2026, 6, 5)
    assert bars[0].close == 1.3
    assert bars[0].transactions == 4
