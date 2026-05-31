from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


OPTION_SYMBOL_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])([0-9]+(?:\.[0-9]+)?)$")


@dataclass(frozen=True)
class ParsedOptionSymbol:
    raw_symbol: str
    normalized_symbol: str
    underlying: str
    expiration: date
    option_type: str
    strike: float
    side: str
    contracts: int
    quantity: float


def normalize_for_option_parsing(raw_symbol: str) -> str:
    stripped = (raw_symbol or "").strip()
    if stripped.startswith("-"):
        stripped = stripped[1:]
    return stripped.strip().upper()


def is_fidelity_option_symbol(raw_symbol: str) -> bool:
    return bool(OPTION_SYMBOL_RE.match(normalize_for_option_parsing(raw_symbol)))


def parse_fidelity_option_symbol(raw_symbol: str, quantity: float) -> ParsedOptionSymbol:
    normalized = normalize_for_option_parsing(raw_symbol)
    match = OPTION_SYMBOL_RE.match(normalized)
    if not match:
        raise ValueError(f"Invalid Fidelity option symbol: {raw_symbol!r}")

    underlying, expiration_raw, option_type_raw, strike_raw = match.groups()
    year = 2000 + int(expiration_raw[0:2])
    month = int(expiration_raw[2:4])
    day = int(expiration_raw[4:6])
    option_type = "call" if option_type_raw == "C" else "put"
    side = "short" if quantity < 0 else "long"
    contracts = abs(int(quantity))

    return ParsedOptionSymbol(
        raw_symbol=raw_symbol,
        normalized_symbol=normalized,
        underlying=underlying,
        expiration=date(year, month, day),
        option_type=option_type,
        strike=float(strike_raw),
        side=side,
        contracts=contracts,
        quantity=quantity,
    )
