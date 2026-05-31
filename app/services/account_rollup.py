from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import floor
from typing import Hashable, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.journal import TradeJournalEntry
from app.models.portfolio import Holding, OptionPosition
from app.services.account_names import canonical_account, canonical_account_name, extract_account_number
from app.services.roll_decision import RollDecisionRow


T = TypeVar("T", bound=Hashable)


@dataclass(frozen=True)
class AccountKey:
    account_number: str | None
    account_name: str | None = None

    @property
    def label(self) -> str:
        account_name = canonical_account_name(self.account_number, self.account_name)
        if account_name and self.account_number:
            return f"{account_name} ({self.account_number})"
        return account_name or self.account_number or "Unassigned"


@dataclass
class AccountOptionSummary:
    account: AccountKey
    symbol: str
    short_calls: int = 0
    long_calls: int = 0
    short_puts: int = 0
    long_puts: int = 0
    source: str = "latest positions"

    @property
    def option_count(self) -> int:
        return self.short_calls + self.long_calls + self.short_puts + self.long_puts


@dataclass
class AccountRollRecommendation:
    account: AccountKey
    symbol: str
    shares: float | None
    current_short_calls: int
    current_long_calls: int
    target_call_contracts: int
    additional_call_contracts: int
    reduce_call_contracts: int
    strike: float | None
    expiration: object | None
    delta: float | None
    target_credit: float
    add_credit: float
    basis: str

    @property
    def current_covered_pct(self) -> float | None:
        if not self.shares:
            return None
        return self.current_short_calls * 100 / self.shares

    @property
    def action(self) -> str:
        if self.target_call_contracts <= 0:
            return "No call target"
        if self.additional_call_contracts > 0:
            return f"Add {self.additional_call_contracts} call"
        if self.reduce_call_contracts > 0:
            return f"No add; target {self.target_call_contracts}"
        return "At target"


def build_account_roll_recommendations(
    db: Session,
    rows: list[RollDecisionRow],
    holdings: list[Holding],
    options: list[OptionPosition],
) -> tuple[list[AccountRollRecommendation], list[str]]:
    option_summaries = build_account_option_summaries(db, options)
    shares_by_account = _holding_shares_by_account(holdings)
    output: list[AccountRollRecommendation] = []
    warnings: list[str] = []

    for row in rows:
        symbol_summaries = [summary for summary in option_summaries if summary.symbol == row.symbol]
        share_rows = {
            account: shares
            for (account, symbol), shares in shares_by_account.items()
            if symbol == row.symbol and shares > 0
        }
        account_keys = sorted(
            set(share_rows) | {summary.account for summary in symbol_summaries},
            key=lambda account: account.label,
        )
        if not account_keys:
            account_keys = [AccountKey(None)]

        summary_by_account = {summary.account: summary for summary in symbol_summaries}
        if share_rows:
            weights = {account: shares for account, shares in share_rows.items()}
            basis = "latest account-level positions"
        else:
            weights = {
                account: max(summary_by_account.get(account, AccountOptionSummary(account, row.symbol)).short_calls, 0)
                for account in account_keys
            }
            if sum(weights.values()) <= 0:
                weights = {
                    account: max(summary_by_account.get(account, AccountOptionSummary(account, row.symbol)).option_count, 1)
                    for account in account_keys
                }
            basis = "estimated from active option lots in the trade journal"

        allocations = allocate_contracts_by_weight(row.selected.contracts, weights)
        credit_per_contract = row.selected.expected_credit / row.selected.contracts if row.selected.contracts else 0.0
        for account in account_keys:
            summary = summary_by_account.get(account, AccountOptionSummary(account, row.symbol))
            target_contracts = allocations.get(account, 0)
            additional = max(target_contracts - summary.short_calls, 0)
            reduce = max(summary.short_calls - target_contracts, 0)
            output.append(
                AccountRollRecommendation(
                    account=account,
                    symbol=row.symbol,
                    shares=share_rows.get(account),
                    current_short_calls=summary.short_calls,
                    current_long_calls=summary.long_calls,
                    target_call_contracts=target_contracts,
                    additional_call_contracts=additional,
                    reduce_call_contracts=reduce,
                    strike=row.selected.strike,
                    expiration=row.selected.expiration,
                    delta=row.selected.delta,
                    target_credit=target_contracts * credit_per_contract,
                    add_credit=additional * credit_per_contract,
                    basis=basis,
                )
            )

    return output, sorted(set(warnings))


def build_account_option_summaries(db: Session, options: list[OptionPosition]) -> list[AccountOptionSummary]:
    if any(option.account_number or option.account_name for option in options):
        return _summaries_from_option_positions(options)
    return _summaries_from_journal(db, options)


def allocate_contracts_by_weight(total_contracts: int, weights: dict[T, float]) -> dict[T, int]:
    if total_contracts <= 0 or not weights:
        return {key: 0 for key in weights}
    positive_weights = {key: max(weight, 0.0) for key, weight in weights.items()}
    total_weight = sum(positive_weights.values())
    if total_weight <= 0:
        positive_weights = {key: 1.0 for key in weights}
        total_weight = float(len(positive_weights))

    raw = {key: total_contracts * weight / total_weight for key, weight in positive_weights.items()}
    allocations = {key: floor(value) for key, value in raw.items()}
    remaining = total_contracts - sum(allocations.values())
    for key, _value in sorted(raw.items(), key=lambda item: (item[1] - floor(item[1]), str(item[0])), reverse=True)[:remaining]:
        allocations[key] += 1
    return allocations


def _summaries_from_option_positions(options: list[OptionPosition]) -> list[AccountOptionSummary]:
    buckets: dict[tuple[AccountKey, str], AccountOptionSummary] = {}
    for option in options:
        account = account_key(option.account_number, option.account_name)
        summary = buckets.setdefault((account, option.underlying), AccountOptionSummary(account, option.underlying))
        _add_option_contracts(summary, option.side, option.option_type, option.contracts)
    return sorted(buckets.values(), key=lambda item: (item.symbol, item.account.label))


def _summaries_from_journal(db: Session, options: list[OptionPosition]) -> list[AccountOptionSummary]:
    active_totals: dict[tuple[str, object, float, str, str], int] = defaultdict(int)
    for option in options:
        key = (option.underlying, option.expiration, option.strike, option.option_type, option.side)
        active_totals[key] += option.contracts
    if not active_totals:
        return []

    journal_weights: dict[tuple[str, object, float, str, str], dict[AccountKey, int]] = defaultdict(lambda: defaultdict(int))
    symbols = sorted({key[0] for key in active_totals})
    entries = db.execute(select(TradeJournalEntry).where(TradeJournalEntry.ticker.in_(symbols))).scalars().all()
    for entry in entries:
        key = _journal_option_key(entry)
        if key not in active_totals:
            continue
        account = account_key(entry.account_number or extract_account_number(entry.notes), entry.account_name)
        journal_weights[key][account] += entry.contracts or 0

    summaries: dict[tuple[AccountKey, str], AccountOptionSummary] = {}
    for key, active_contracts in active_totals.items():
        symbol, _expiration, _strike, option_type, side = key
        account_weights = journal_weights.get(key)
        if account_weights:
            allocations = allocate_contracts_by_weight(active_contracts, dict(account_weights))
        else:
            allocations = {AccountKey(None): active_contracts}
        for account, contracts in allocations.items():
            summary = summaries.setdefault(
                (account, symbol),
                AccountOptionSummary(account=account, symbol=symbol, source="trade journal" if account_weights else "latest positions"),
            )
            _add_option_contracts(summary, side, option_type, contracts)
    return sorted(summaries.values(), key=lambda item: (item.symbol, item.account.label))


def _holding_shares_by_account(holdings: list[Holding]) -> dict[tuple[AccountKey, str], float]:
    if not any(holding.account_number or holding.account_name for holding in holdings):
        return {}
    shares: dict[tuple[AccountKey, str], float] = defaultdict(float)
    for holding in holdings:
        if holding.asset_class in {"cash", "pending", "option"}:
            continue
        account = account_key(holding.account_number, holding.account_name)
        shares[(account, holding.symbol.upper())] += holding.quantity or 0.0
    return dict(shares)


def _journal_option_key(entry: TradeJournalEntry) -> tuple[str, object, float, str, str] | None:
    if not entry.contracts or entry.strike is None or entry.expiration is None:
        return None
    action = (entry.action or "").lower()
    if "closing" in action or "expired" in action:
        return None
    strategy = (entry.strategy or "").lower()
    option_type = "call" if "call" in strategy else "put" if "put" in strategy else None
    if option_type is None:
        return None
    if strategy.startswith("covered") or strategy.startswith("cash-secured"):
        side = "short"
    elif strategy.startswith("long"):
        side = "long"
    elif "sold" in action or "sell" in action:
        side = "short"
    elif "bought" in action or "buy" in action:
        side = "long"
    else:
        return None
    return (entry.ticker, entry.expiration, entry.strike, option_type, side)


def account_key(account_number: str | None, account_name: str | None = None) -> AccountKey:
    number, name = canonical_account(account_number, account_name)
    return AccountKey(number, name)


def _add_option_contracts(summary: AccountOptionSummary, side: str, option_type: str, contracts: int) -> None:
    if side == "short" and option_type == "call":
        summary.short_calls += contracts
    elif side == "long" and option_type == "call":
        summary.long_calls += contracts
    elif side == "short" and option_type == "put":
        summary.short_puts += contracts
    elif side == "long" and option_type == "put":
        summary.long_puts += contracts
