from datetime import date

from app.services.market_data.yahoo_provider import YahooProvider


def test_yahoo_provider_normalizes_option_and_calculates_greeks():
    provider = YahooProvider()
    contract = provider._normalize_contract(
        "IBIT",
        date(2026, 6, 5),
        "call",
        {
            "contractSymbol": "IBIT260605C00043500",
            "strike": 43.5,
            "lastPrice": 0.7,
            "bid": 0.65,
            "ask": 0.75,
            "volume": 120,
            "openInterest": 450,
            "impliedVolatility": 0.75,
            "lastTradeDate": 1780080000,
        },
        41.63,
        "regular",
    )

    assert contract is not None
    assert contract.provider == "yahoo"
    assert contract.underlying == "IBIT"
    assert contract.option_type == "call"
    assert contract.mid == 0.7
    assert contract.delta is not None
    assert 0 < contract.delta < 1
    assert "Black-Scholes-Merton" in " ".join(contract.warnings)
