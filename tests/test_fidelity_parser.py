from pathlib import Path

from app.services.fidelity_parser import FidelityPositionsCsvParser, parse_money, parse_percent


def test_parse_money_variants():
    assert parse_money("$63,729.24") == 63729.24
    assert parse_money("-$315.00") == -315.0
    assert parse_money("+$65.91") == 65.91
    assert parse_money("") is None


def test_parse_percent_as_percentage_points():
    assert parse_percent("73.38%") == 73.38


def test_fidelity_sample_parser_handles_options_cash_and_pending():
    text = Path("sample_data/portfolio_sample_fidelity.csv").read_text()
    parsed = FidelityPositionsCsvParser().parse_text(text)
    assert parsed.diagnostics.rows_imported == 9
    assert parsed.diagnostics.rows_skipped == 0
    assert parsed.diagnostics.sata_value == 580.85
    assert parsed.diagnostics.cash_value == 2500.25
    assert parsed.diagnostics.pending_activity == 30.63
    ibit_option = next(option for option in parsed.option_positions if option.underlying == "IBIT")
    assert ibit_option.side == "short"
    assert ibit_option.contracts == 15
    asst_long = next(option for option in parsed.option_positions if option.normalized_symbol == "ASST260605C22.5")
    assert asst_long.side == "long"
    coverage = {row.underlying: row for row in parsed.diagnostics.coverage}
    assert coverage["IBIT"].optioned_shares == 1500
    assert coverage["ASST"].short_call_contracts == 9
