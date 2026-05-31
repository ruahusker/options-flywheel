from __future__ import annotations

import re


ACCOUNT_NAME_BY_NUMBER = {
    "244172640": "Steve-Trad IRA",
    "239474677": "Nicole-Roth IRA",
    "241405056": "Nicole-Trad IRA",
}

_ACCOUNT_RE = re.compile(r"(?:^|;\s*)(\d{6,})(?:\s*;|$)")


def canonical_account_number(account_number: str | None) -> str | None:
    if account_number is None:
        return None
    value = str(account_number).strip()
    if not value or value.lower() in {"multiple accounts", "n/a", "--"}:
        return value or None
    return value


def canonical_account_name(account_number: str | None, account_name: str | None = None) -> str | None:
    account_number = canonical_account_number(account_number)
    if account_number in ACCOUNT_NAME_BY_NUMBER:
        return ACCOUNT_NAME_BY_NUMBER[account_number]
    if account_name is None:
        return None
    value = str(account_name).strip()
    return value or None


def canonical_account(account_number: str | None, account_name: str | None = None) -> tuple[str | None, str | None]:
    number = canonical_account_number(account_number)
    return number, canonical_account_name(number, account_name)


def extract_account_number(notes: str | None) -> str | None:
    if not notes:
        return None
    match = _ACCOUNT_RE.search(notes)
    return canonical_account_number(match.group(1)) if match else None
