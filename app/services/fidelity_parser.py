from __future__ import annotations

import csv
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Iterable

from app.services.account_names import canonical_account
from app.services.option_symbol_parser import (
    is_fidelity_option_symbol,
    parse_fidelity_option_symbol,
)


FIDELITY_COLUMNS = [
    "Account Number",
    "Account Name",
    "Symbol",
    "Description",
    "Quantity",
    "Last Price",
    "Last Price Change",
    "Current Value",
    "Today's Gain/Loss Dollar",
    "Today's Gain/Loss Percent",
    "Total Gain/Loss Dollar",
    "Total Gain/Loss Percent",
    "Percent Of Account",
    "Cost Basis Total",
    "Average Cost Basis",
    "Type",
]


@dataclass
class ParsedHolding:
    account_number: str | None
    account_name: str | None
    symbol: str
    description: str
    quantity: float | None
    last_price: float | None
    current_value: float | None
    percent_of_account: float | None
    cost_basis_total: float | None
    average_cost_basis: float | None
    position_type: str | None
    asset_class: str


@dataclass
class ParsedOptionPosition:
    account_number: str | None
    account_name: str | None
    raw_symbol: str
    normalized_symbol: str
    underlying: str
    expiration: object
    option_type: str
    strike: float
    side: str
    contracts: int
    quantity: float
    last_price: float | None
    current_value: float | None
    average_cost_basis: float | None
    description: str


@dataclass
class ParsedCashPosition:
    account_number: str | None
    account_name: str | None
    symbol: str
    description: str
    current_value: float | None


@dataclass
class CoverageDiagnostic:
    underlying: str
    shares: float
    short_call_contracts: int
    long_call_contracts: int
    short_put_contracts: int
    optioned_shares: int
    uncovered_shares: float
    optioned_percentage: float
    covered_ratio: float
    cash_secured_put_required_cash: float = 0.0


@dataclass
class ParseDiagnostics:
    rows_imported: int = 0
    rows_skipped: int = 0
    total_account_value: float = 0.0
    equity_etf_value: float = 0.0
    sata_value: float = 0.0
    cash_value: float = 0.0
    pending_activity: float = 0.0
    option_market_value: float = 0.0
    warnings: list[str] = field(default_factory=list)
    skipped_rows: list[dict] = field(default_factory=list)
    detected_symbols: list[str] = field(default_factory=list)
    detected_options: list[str] = field(default_factory=list)
    coverage: list[CoverageDiagnostic] = field(default_factory=list)


@dataclass
class ParsedPortfolio:
    holdings: list[ParsedHolding]
    option_positions: list[ParsedOptionPosition]
    cash_positions: list[ParsedCashPosition]
    diagnostics: ParseDiagnostics
    account_number: str | None = None
    account_name: str | None = None


def parse_money(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "--", "N/A"}:
        return None
    is_negative = False
    if text.startswith("(") and text.endswith(")"):
        is_negative = True
        text = text[1:-1]
    if text.startswith("-"):
        is_negative = True
        text = text[1:]
    if text.startswith("+"):
        text = text[1:]
    text = text.replace("$", "").replace(",", "").strip()
    if text == "":
        return None
    result = float(text)
    return -result if is_negative else result


def parse_percent(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "--", "N/A"}:
        return None
    text = text.replace("%", "").replace(",", "").strip()
    if text.startswith("+"):
        text = text[1:]
    return float(text)


def parse_quantity(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "--", "N/A"}:
        return None
    return float(text.replace(",", ""))


def _should_stop(row: list[str]) -> bool:
    if not row or all(not cell.strip() for cell in row):
        return True
    first = row[0].strip()
    return (
        first.startswith("The data and information")
        or first.startswith("Brokerage services")
        or first.startswith("Date downloaded")
    )


def _trim_row(row: list[str]) -> list[str]:
    if len(row) == len(FIDELITY_COLUMNS) + 1 and row[-1].strip() == "":
        return row[:-1]
    return row


def classify_asset(symbol: str, description: str) -> str:
    symbol_clean = (symbol or "").strip()
    symbol_upper = symbol_clean.upper()
    desc_upper = (description or "").upper()
    if symbol_clean == "Pending activity":
        return "pending"
    if symbol_upper == "SPAXX**" or "HELD IN MONEY MARKET" in desc_upper:
        return "cash"
    if is_fidelity_option_symbol(symbol_clean):
        return "option"
    if symbol_upper == "SATA":
        return "preferred"
    if symbol_upper == "IBIT":
        return "etf"
    if symbol_upper == "ASST":
        return "equity"
    if "PFD" in desc_upper or "PREFERRED" in desc_upper:
        return "preferred"
    return "equity_or_etf"


class FidelityPositionsCsvParser:
    columns = FIDELITY_COLUMNS

    def parse_path(self, path: str | Path) -> ParsedPortfolio:
        return self.parse_text(Path(path).read_text())

    def parse_text(self, text: str) -> ParsedPortfolio:
        reader = csv.reader(StringIO(text))
        rows = list(reader)
        if not rows:
            raise ValueError("CSV is empty")

        header = _trim_row(rows[0])
        if header != FIDELITY_COLUMNS:
            raise ValueError("CSV header does not match expected Fidelity positions export")

        holdings: list[ParsedHolding] = []
        options: list[ParsedOptionPosition] = []
        cash_positions: list[ParsedCashPosition] = []
        diagnostics = ParseDiagnostics()
        account_number = None
        account_name = None

        for line_number, raw_row in enumerate(rows[1:], start=2):
            if _should_stop(raw_row):
                break
            row = _trim_row(raw_row)
            if len(row) != len(FIDELITY_COLUMNS):
                diagnostics.rows_skipped += 1
                diagnostics.skipped_rows.append({"line": line_number, "row": raw_row, "reason": "wrong column count"})
                continue

            data = dict(zip(FIDELITY_COLUMNS, row))
            symbol = data["Symbol"].strip()
            description = data["Description"].strip()
            quantity = parse_quantity(data["Quantity"])
            last_price = parse_money(data["Last Price"])
            current_value = parse_money(data["Current Value"])
            percent_of_account = parse_percent(data["Percent Of Account"])
            cost_basis_total = parse_money(data["Cost Basis Total"])
            average_cost_basis = parse_money(data["Average Cost Basis"])
            asset_class = classify_asset(symbol, description)
            row_account_number, row_account_name = canonical_account(
                data["Account Number"].strip() or None,
                data["Account Name"].strip() or None,
            )
            account_number = account_number or row_account_number
            account_name = account_name or row_account_name

            diagnostics.rows_imported += 1
            if symbol and symbol not in diagnostics.detected_symbols:
                diagnostics.detected_symbols.append(symbol)

            if asset_class == "option":
                if quantity is None:
                    diagnostics.warnings.append(f"Option row {line_number} has no quantity and was skipped")
                    diagnostics.rows_imported -= 1
                    diagnostics.rows_skipped += 1
                    continue
                parsed = parse_fidelity_option_symbol(symbol, quantity)
                option = ParsedOptionPosition(
                    account_number=row_account_number,
                    account_name=row_account_name,
                    raw_symbol=symbol,
                    normalized_symbol=parsed.normalized_symbol,
                    underlying=parsed.underlying,
                    expiration=parsed.expiration,
                    option_type=parsed.option_type,
                    strike=parsed.strike,
                    side=parsed.side,
                    contracts=parsed.contracts,
                    quantity=quantity,
                    last_price=last_price,
                    current_value=current_value,
                    average_cost_basis=average_cost_basis,
                    description=description,
                )
                options.append(option)
                diagnostics.option_market_value += current_value or 0.0
                diagnostics.detected_options.append(parsed.normalized_symbol)
                continue

            holding = ParsedHolding(
                account_number=row_account_number,
                account_name=row_account_name,
                symbol=symbol,
                description=description,
                quantity=quantity,
                last_price=last_price,
                current_value=current_value,
                percent_of_account=percent_of_account,
                cost_basis_total=cost_basis_total,
                average_cost_basis=average_cost_basis,
                position_type=data["Type"].strip() or None,
                asset_class=asset_class,
            )
            holdings.append(holding)

            value = current_value or 0.0
            if asset_class in {"equity", "etf", "equity_or_etf", "preferred"}:
                diagnostics.equity_etf_value += value
            if symbol.upper() == "SATA":
                diagnostics.sata_value += value
            if asset_class == "cash":
                diagnostics.cash_value += value
                cash_positions.append(
                    ParsedCashPosition(holding.account_number, holding.account_name, symbol, description, current_value)
                )
            if asset_class == "pending":
                diagnostics.pending_activity += value
                cash_positions.append(
                    ParsedCashPosition(holding.account_number, holding.account_name, symbol, description, current_value)
                )

        diagnostics.total_account_value = (
            sum(h.current_value or 0.0 for h in holdings)
            + sum(o.current_value or 0.0 for o in options)
        )
        diagnostics.coverage = calculate_coverage(holdings, options, diagnostics.cash_value + diagnostics.pending_activity)
        return ParsedPortfolio(holdings, options, cash_positions, diagnostics, account_number, account_name)


def calculate_coverage(
    holdings: Iterable[ParsedHolding],
    options: Iterable[ParsedOptionPosition],
    available_cash: float = 0.0,
) -> list[CoverageDiagnostic]:
    shares_by_symbol: dict[str, float] = {}
    for holding in holdings:
        symbol = holding.symbol.upper()
        if holding.asset_class not in {"cash", "pending", "option"}:
            shares_by_symbol[symbol] = shares_by_symbol.get(symbol, 0.0) + (holding.quantity or 0.0)

    option_list = list(options)
    underlyings = sorted(set(shares_by_symbol) | {option.underlying for option in option_list})
    coverage: list[CoverageDiagnostic] = []
    for underlying in underlyings:
        shares = shares_by_symbol.get(underlying, 0.0)
        short_calls = sum(
            option.contracts
            for option in option_list
            if option.underlying == underlying and option.option_type == "call" and option.side == "short"
        )
        long_calls = sum(
            option.contracts
            for option in option_list
            if option.underlying == underlying and option.option_type == "call" and option.side == "long"
        )
        short_puts = sum(
            option.contracts
            for option in option_list
            if option.underlying == underlying and option.option_type == "put" and option.side == "short"
        )
        required_shares = short_calls * 100
        optioned_shares = required_shares
        uncovered_shares = shares - optioned_shares
        optioned_pct = optioned_shares / shares if shares > 0 else 0.0
        covered_ratio = min(shares / required_shares, 1.0) if required_shares > 0 else 1.0
        required_cash = sum(
            option.contracts * 100 * option.strike
            for option in option_list
            if option.underlying == underlying and option.option_type == "put" and option.side == "short"
        )
        cash_ratio = available_cash / required_cash if required_cash > 0 else 1.0
        _ = cash_ratio
        coverage.append(
            CoverageDiagnostic(
                underlying=underlying,
                shares=shares,
                short_call_contracts=short_calls,
                long_call_contracts=long_calls,
                short_put_contracts=short_puts,
                optioned_shares=optioned_shares,
                uncovered_shares=uncovered_shares,
                optioned_percentage=optioned_pct,
                covered_ratio=covered_ratio,
                cash_secured_put_required_cash=required_cash,
            )
        )
    return coverage
