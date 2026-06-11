from app.services.fidelity_history_parser import FidelityAccountHistoryCsvParser


HISTORY_CSV = """Run Date,Account Number,Account Name,Action,Symbol,Description,Quantity,Price ($),Amount ($)
2026-05-27,Z12345678,Retirement Rollover,You bought,IBIT,ISHARES BITCOIN TRUST ETF,10,$40.00,-$400.00
2026-05-28,Z12345678,Retirement Rollover,You sold,IBIT,ISHARES BITCOIN TRUST ETF,2,$42.00,$84.00
2026-05-27,Z12345678,Retirement Rollover,You bought,SATA,SATA INCOME PREFERRED,5,$100.01,-$500.05
2026-05-27,Z12345678,Retirement Rollover,You bought,ASST,ASST BITCOIN TREASURY CO,7.5,$18.00,-$135.00
2026-05-27,Z12345678,Retirement Rollover,You bought,BSOL,BSOL COMMON SHARES,3,$11.00,-$33.00
2026-05-29,Z12345678,Retirement Rollover,Sold short,-IBIT260605C43.5,IBIT JUN 05 2026 $43.50 CALL,1,$1.15,$115.00
"""


def test_account_history_parser_targets_dashboard_symbols():
    parsed = FidelityAccountHistoryCsvParser().parse_text(HISTORY_CSV)
    holdings = {holding.symbol: holding for holding in parsed.holdings}

    assert set(holdings) == {"IBIT", "SATA", "ASST"}
    assert holdings["IBIT"].quantity == 8
    assert holdings["IBIT"].current_value == 336
    assert holdings["SATA"].current_value == 500.05
    assert parsed.diagnostics.sata_value == 500.05
    assert parsed.option_positions[0].underlying == "IBIT"
    assert parsed.option_positions[0].side == "short"
    assert parsed.option_positions[0].contracts == 1
    assert parsed.option_positions[0].account_number == "Z12345678"


def test_account_history_parser_labels_multiple_accounts():
    text = HISTORY_CSV + "2026-05-27,Z22222222,Other Account,You bought,IBIT,ISHARES BITCOIN TRUST ETF,1,$40.00,-$40.00\n"
    parsed = FidelityAccountHistoryCsvParser().parse_text(text)

    assert parsed.account_number == "Multiple accounts"
    assert parsed.account_name == "Multiple accounts"


def test_account_history_parser_builds_trade_journal_entries():
    entries = FidelityAccountHistoryCsvParser().parse_journal_entries(HISTORY_CSV, "Accounts_History.csv")
    keyed = {(entry.ticker, entry.action): entry for entry in entries}

    assert ("BSOL", "You bought") not in keyed
    assert keyed[("SATA", "You bought")].sata_contribution == 500.05
    option = keyed[("IBIT", "Sold short")]
    assert option.account_number == "Z12345678"
    assert option.account_name == "Retirement Rollover"
    assert option.strategy == "covered call"
    assert option.contracts == 1
    assert option.strike == 43.5
    assert option.credit_debit == 115
    assert "Accounts_History.csv line 7" in option.notes


def test_account_history_parser_canonicalizes_known_account_names():
    text = HISTORY_CSV.replace("Z12345678,Retirement Rollover", "244172640,Fidelity IRA")
    parsed = FidelityAccountHistoryCsvParser().parse_text(text)
    entries = FidelityAccountHistoryCsvParser().parse_journal_entries(text, "Accounts_History.csv")

    assert parsed.account_name == "Steve-Trad IRA"
    assert parsed.holdings[0].account_name == "Steve-Trad IRA"
    assert entries[0].account_name == "Steve-Trad IRA"


def test_assignment_and_expiration_rows_close_short_positions():
    from app.services.fidelity_history_parser import _signed_quantity

    # Option leg of a call assignment: Fidelity logs positive quantity to CLOSE the short.
    # Flipping it negative used to double the short position (e.g. 29 sold -> 58 shown).
    assert _signed_quantity(29, "ASSIGNED as of Jun-08-2026 CALL (IBIT) ISHARES BITCOIN JUN 08 26 $33.5 (100 SHS)", None) == 29
    # Expiration of a short option likewise closes with positive quantity.
    assert _signed_quantity(5, "EXPIRED CALL (IBIT) ISHARES BITCOIN JUN 05 26 $43.5 (100 SHS)", None) == 5
    # Expiration of a LONG option arrives with negative quantity and must stay negative.
    assert _signed_quantity(-3, "EXPIRED CALL (IBIT) ISHARES BITCOIN JUN 05 26 $43.5 (100 SHS)", None) == -3
    # The companion share-sale row of an assignment still sells shares (negative).
    assert _signed_quantity(2900, "YOU SOLD ASSIGNED CALLS AS OF 06-08-26 ISHARES BITCOIN TRUST ETF (IBIT) (Cash)", 97150.0) == -2900
    # Plain open/close transactions are unchanged.
    assert _signed_quantity(29, "YOU SOLD OPENING TRANSACTION CALL (IBIT) ...", 785.12) == -29
    assert _signed_quantity(29, "YOU BOUGHT CLOSING TRANSACTION CALL (IBIT) ...", -238.78) == 29
