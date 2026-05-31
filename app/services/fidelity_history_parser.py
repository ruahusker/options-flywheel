from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path

from app.services.account_names import canonical_account, canonical_account_name
from app.services.fidelity_parser import (
    FIDELITY_COLUMNS,
    ParsedHolding,
    ParsedOptionPosition,
    ParsedPortfolio,
    ParseDiagnostics,
    calculate_coverage,
    classify_asset,
    parse_money,
    parse_quantity,
)
from app.services.option_symbol_parser import is_fidelity_option_symbol, parse_fidelity_option_symbol


TARGET_SYMBOLS = {"IBIT", "SATA", "ASST"}
TARGET_OPTION_UNDERLYINGS = {"IBIT", "ASST"}


@dataclass
class _HoldingAccumulator:
    symbol: str
    description: str
    quantity: float = 0.0
    last_price: float | None = None
    current_value: float | None = None
    cost_basis_total: float = 0.0
    account_number: str | None = None
    account_name: str | None = None


@dataclass
class _OptionAccumulator:
    raw_symbol: str
    normalized_symbol: str
    underlying: str
    quantity: float = 0.0
    last_price: float | None = None
    current_value: float | None = None
    average_cost_basis: float | None = None
    description: str = ""
    account_number: str | None = None
    account_name: str | None = None


@dataclass
class ParsedJournalTransaction:
    account_number: str | None
    account_name: str | None
    ticker: str
    strategy: str
    action: str
    created_at: datetime
    contracts: int = 0
    strike: float | None = None
    expiration: object | None = None
    credit_debit: float | None = None
    sata_contribution: float | None = None
    notes: str | None = None


class FidelityAccountHistoryCsvParser:
    """Parse a Fidelity history export into a targeted portfolio snapshot.

    Fidelity history exports vary more than positions exports. This parser accepts
    common activity/history column names and imports only the symbols used by this
    app's dashboard.
    """

    def parse_path(self, path: str | Path) -> ParsedPortfolio:
        return self.parse_text(Path(path).read_text())

    def parse_journal_entries(self, text: str, filename: str | None = None) -> list[ParsedJournalTransaction]:
        entries: list[ParsedJournalTransaction] = []
        for line_number, row in _dict_rows(text):
            symbol = _symbol(row)
            if not symbol:
                continue

            description = _first(row, "Description", "Security Description", "Name", "Investment") or symbol
            action = _first(row, "Action", "Transaction Type", "Activity", "Type") or "activity"
            quantity = parse_quantity(_first(row, "Quantity", "Shares", "Units"))
            price = parse_money(_first(row, "Price ($)", "Price", "Share Price", "Execution Price", "Last Price"))
            amount = parse_money(_first(row, "Amount ($)", "Amount", "Net Amount", "Net Amount ($)"))
            transaction_date = _date(row) or datetime.utcnow()
            row_account_number = _first(row, "Account Number", "Account #", "Account")
            row_account_name = _first(row, "Account Name", "Account Type")
            row_account_number, row_account_name = canonical_account(row_account_number, row_account_name)
            account = row_account_number or row_account_name
            source_note = f"Imported from {filename or 'Fidelity history'} line {line_number}"
            notes = "; ".join(part for part in [description, account, source_note] if part)

            if is_fidelity_option_symbol(symbol):
                if quantity is None:
                    continue
                signed_quantity = _signed_quantity(quantity, action, amount)
                parsed_symbol = parse_fidelity_option_symbol(symbol, signed_quantity)
                if parsed_symbol.underlying not in TARGET_OPTION_UNDERLYINGS:
                    continue
                entries.append(
                    ParsedJournalTransaction(
                        account_number=row_account_number,
                        account_name=row_account_name,
                        ticker=parsed_symbol.underlying,
                        strategy=_option_strategy(parsed_symbol.option_type, signed_quantity),
                        action=action,
                        created_at=transaction_date,
                        contracts=parsed_symbol.contracts,
                        strike=parsed_symbol.strike,
                        expiration=parsed_symbol.expiration,
                        credit_debit=amount if amount is not None else _option_value(signed_quantity, price),
                        notes=notes,
                    )
                )
                continue

            symbol_upper = symbol.upper()
            if symbol_upper not in TARGET_SYMBOLS:
                continue
            signed_quantity = _signed_quantity(quantity or 0.0, action, amount)
            sata_contribution = None
            if symbol_upper == "SATA" and signed_quantity > 0:
                sata_contribution = abs(amount) if amount is not None else _holding_value(signed_quantity, price)
            entries.append(
                ParsedJournalTransaction(
                    account_number=row_account_number,
                    account_name=row_account_name,
                    ticker=symbol_upper,
                    strategy="SATA contribution" if symbol_upper == "SATA" and signed_quantity > 0 else "stock",
                    action=action,
                    created_at=transaction_date,
                    contracts=0,
                    credit_debit=amount,
                    sata_contribution=sata_contribution,
                    notes=notes,
                )
            )
        return entries

    def parse_text(self, text: str) -> ParsedPortfolio:
        rows = _dict_rows(text)
        diagnostics = ParseDiagnostics()
        account_number = None
        account_name = None
        account_numbers: set[str] = set()
        account_names: set[str] = set()
        holdings_by_symbol: dict[tuple[str | None, str], _HoldingAccumulator] = {}
        options_by_symbol: dict[tuple[str | None, str], _OptionAccumulator] = {}

        for line_number, row in rows:
            symbol = _symbol(row)
            description = _first(row, "Description", "Security Description", "Name", "Investment") or symbol
            if not symbol:
                diagnostics.rows_skipped += 1
                continue

            quantity = parse_quantity(_first(row, "Quantity", "Shares", "Units"))
            price = parse_money(_first(row, "Price ($)", "Price", "Share Price", "Execution Price", "Last Price"))
            value = parse_money(_first(row, "Current Value", "Market Value", "Value"))
            amount = parse_money(_first(row, "Amount ($)", "Amount", "Net Amount", "Net Amount ($)"))
            action = _first(row, "Action", "Transaction Type", "Activity", "Type") or ""
            row_account_number = _first(row, "Account Number", "Account", "Account #")
            row_account_name = _first(row, "Account Name", "Account Type", "Account")
            row_account_number, row_account_name = canonical_account(row_account_number, row_account_name)
            account_number = account_number or row_account_number
            account_name = account_name or row_account_name
            row_account_key = row_account_number or row_account_name
            if row_account_number:
                account_numbers.add(row_account_number)
            if row_account_name:
                account_names.add(row_account_name)

            if is_fidelity_option_symbol(symbol):
                if quantity is None:
                    diagnostics.rows_skipped += 1
                    diagnostics.warnings.append(f"History row {line_number} has an option symbol but no quantity.")
                    continue
                parsed_symbol = parse_fidelity_option_symbol(symbol, _signed_quantity(quantity, action, amount))
                if parsed_symbol.underlying not in TARGET_OPTION_UNDERLYINGS:
                    diagnostics.rows_skipped += 1
                    continue
                option_key = (row_account_key, parsed_symbol.normalized_symbol)
                option = options_by_symbol.get(option_key)
                if option is None:
                    option = _OptionAccumulator(
                        raw_symbol=symbol,
                        normalized_symbol=parsed_symbol.normalized_symbol,
                        underlying=parsed_symbol.underlying,
                        description=description,
                        account_number=row_account_number,
                        account_name=row_account_name,
                    )
                    options_by_symbol[option_key] = option
                signed_quantity = _signed_quantity(quantity, action, amount)
                option.quantity += signed_quantity
                option.last_price = price if price is not None else option.last_price
                option.average_cost_basis = price if price is not None else option.average_cost_basis
                option.current_value = _option_value(option.quantity, option.last_price)
                diagnostics.rows_imported += 1
                continue

            symbol_upper = symbol.upper()
            if symbol_upper not in TARGET_SYMBOLS:
                diagnostics.rows_skipped += 1
                continue
            if quantity is None:
                diagnostics.rows_skipped += 1
                diagnostics.warnings.append(f"History row {line_number} for {symbol_upper} has no quantity.")
                continue

            holding_key = (row_account_key, symbol_upper)
            holding = holdings_by_symbol.get(holding_key)
            if holding is None:
                holding = _HoldingAccumulator(
                    symbol=symbol_upper,
                    description=description,
                    account_number=row_account_number,
                    account_name=row_account_name,
                )
                holdings_by_symbol[holding_key] = holding

            signed_quantity = _signed_quantity(quantity, action, amount)
            holding.quantity += signed_quantity
            holding.last_price = price if price is not None else holding.last_price
            if signed_quantity > 0 and amount is not None:
                holding.cost_basis_total += abs(amount)
            elif signed_quantity < 0 and amount is not None:
                holding.cost_basis_total -= min(abs(amount), holding.cost_basis_total)
            holding.current_value = value if value is not None else _holding_value(holding.quantity, holding.last_price)
            diagnostics.rows_imported += 1

        holdings = [
            ParsedHolding(
                account_number=item.account_number,
                account_name=item.account_name,
                symbol=item.symbol,
                description=item.description,
                quantity=item.quantity,
                last_price=item.last_price,
                current_value=item.current_value,
                percent_of_account=None,
                cost_basis_total=item.cost_basis_total or None,
                average_cost_basis=(item.cost_basis_total / item.quantity if item.quantity else None),
                position_type=None,
                asset_class=classify_asset(item.symbol, item.description),
            )
            for item in holdings_by_symbol.values()
            if abs(item.quantity) > 0.000001
        ]

        options: list[ParsedOptionPosition] = []
        for item in options_by_symbol.values():
            if abs(item.quantity) <= 0.000001:
                continue
            parsed_symbol = parse_fidelity_option_symbol(item.normalized_symbol, item.quantity)
            options.append(
                ParsedOptionPosition(
                    account_number=item.account_number,
                    account_name=item.account_name,
                    raw_symbol=item.raw_symbol,
                    normalized_symbol=item.normalized_symbol,
                    underlying=parsed_symbol.underlying,
                    expiration=parsed_symbol.expiration,
                    option_type=parsed_symbol.option_type,
                    strike=parsed_symbol.strike,
                    side=parsed_symbol.side,
                    contracts=parsed_symbol.contracts,
                    quantity=item.quantity,
                    last_price=item.last_price,
                    current_value=item.current_value,
                    average_cost_basis=item.average_cost_basis,
                    description=item.description,
                )
            )

        diagnostics.detected_symbols = [holding.symbol for holding in holdings]
        diagnostics.detected_options = [option.normalized_symbol for option in options]
        diagnostics.equity_etf_value = sum(holding.current_value or 0.0 for holding in holdings)
        diagnostics.sata_value = sum(holding.current_value or 0.0 for holding in holdings if holding.symbol == "SATA")
        diagnostics.option_market_value = sum(option.current_value or 0.0 for option in options)
        diagnostics.total_account_value = diagnostics.equity_etf_value + diagnostics.option_market_value
        diagnostics.coverage = calculate_coverage(holdings, options, 0.0)
        if holdings or options:
            diagnostics.warnings.append(
                "Imported from account history. Quantities are aggregated from matching rows; values use current-value columns when present, otherwise the latest transaction price."
            )
        if len(account_numbers) > 1:
            account_number = "Multiple accounts"
        if len(account_names) > 1:
            account_name = "Multiple accounts"
        account_name = canonical_account_name(account_number, account_name)
        return ParsedPortfolio(holdings, options, [], diagnostics, account_number, account_name)


def _dict_rows(text: str) -> list[tuple[int, dict[str, str]]]:
    raw_rows = list(csv.reader(StringIO(text)))
    if not raw_rows:
        raise ValueError("CSV is empty")

    header_index = 0
    for index, row in enumerate(raw_rows):
        normalized = {_normalize_header(cell) for cell in row}
        if "symbol" in normalized and ("quantity" in normalized or "shares" in normalized or "units" in normalized):
            header_index = index
            break

    header = raw_rows[header_index]
    if header == FIDELITY_COLUMNS:
        reader = csv.DictReader(StringIO(text))
        return [(line, row) for line, row in enumerate(reader, start=2)]

    rows: list[tuple[int, dict[str, str]]] = []
    for line_number, raw in enumerate(raw_rows[header_index + 1 :], start=header_index + 2):
        if not raw or all(not cell.strip() for cell in raw):
            continue
        values = raw[: len(header)] + [""] * max(len(header) - len(raw), 0)
        rows.append((line_number, dict(zip(header, values))))
    return rows


def _first(row: dict[str, str], *names: str) -> str | None:
    normalized = {_normalize_header(key): value for key, value in row.items()}
    for name in names:
        value = normalized.get(_normalize_header(name))
        if value is not None and str(value).strip() not in {"", "--", "N/A"}:
            return str(value).strip()
    return None


def _symbol(row: dict[str, str]) -> str:
    value = _first(row, "Symbol", "Security Symbol", "Ticker", "Ticker Symbol")
    return (value or "").strip().upper()


def _normalize_header(value: str) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def _date(row: dict[str, str]) -> datetime | None:
    value = _first(row, "Date", "Run Date", "Transaction Date", "Trade Date", "Settlement Date", "Activity Date")
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _signed_quantity(quantity: float, action: str, amount: float | None) -> float:
    if quantity < 0:
        return quantity
    action_text = action.lower()
    negative_terms = (
        "sell",
        "sold",
        "redemption",
        "exchange out",
        "transfer out",
        "journal out",
        "assigned",
        "expired",
    )
    positive_terms = (
        "buy",
        "bought",
        "purchase",
        "reinvest",
        "exchange in",
        "transfer in",
        "journal in",
    )
    if any(term in action_text for term in negative_terms):
        return -abs(quantity)
    if any(term in action_text for term in positive_terms):
        return abs(quantity)
    if amount is not None and amount > 0 and "dividend" not in action_text:
        return -abs(quantity)
    return abs(quantity)


def _holding_value(quantity: float, price: float | None) -> float | None:
    if price is None:
        return None
    return quantity * price


def _option_value(quantity: float, price: float | None) -> float | None:
    if price is None:
        return None
    return quantity * price * 100


def _option_strategy(option_type: str, quantity: float) -> str:
    if option_type == "call":
        return "covered call" if quantity < 0 else "long call"
    return "cash-secured put" if quantity < 0 else "long put"
