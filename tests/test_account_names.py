from app.services.account_names import canonical_account, canonical_account_name, extract_account_number
from app.services.account_rollup import account_key


def test_known_account_numbers_use_canonical_names():
    assert canonical_account_name("244172640", "Old Name") == "Steve-Trad IRA"
    assert canonical_account_name("239474677", None) == "Nicole-Roth IRA"
    assert canonical_account("241405056", "Nicole - Trad IRA") == ("241405056", "Nicole-Trad IRA")


def test_account_label_uses_canonical_name_from_number():
    assert account_key("244172640").label == "Steve-Trad IRA (244172640)"


def test_extract_account_number_from_import_note():
    assert extract_account_number("CALL ASST; 239474677; Imported from history line 4") == "239474677"
