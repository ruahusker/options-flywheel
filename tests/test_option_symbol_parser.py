from datetime import date

from app.services.option_symbol_parser import parse_fidelity_option_symbol


def test_parse_short_call_from_quantity_not_dash():
    parsed = parse_fidelity_option_symbol(" -IBIT260605C43.5", -15)
    assert parsed.normalized_symbol == "IBIT260605C43.5"
    assert parsed.underlying == "IBIT"
    assert parsed.expiration == date(2026, 6, 5)
    assert parsed.option_type == "call"
    assert parsed.strike == 43.5
    assert parsed.side == "short"
    assert parsed.contracts == 15


def test_parse_long_call_even_with_leading_dash():
    parsed = parse_fidelity_option_symbol(" -ASST260605C22.5", 9)
    assert parsed.side == "long"
    assert parsed.contracts == 9
