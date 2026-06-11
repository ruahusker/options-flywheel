#!/usr/bin/env python3
"""Walk-forward backtest for an IBIT covered-call wheel reinvestment rule.

Default rule:
  * use the latest imported portfolio snapshot for IBIT, ASST, and SATA sizing;
  * split IBIT into a covered-call wheel sleeve and an uncovered sleeve;
  * sell ATM IBIT calls only on the wheel sleeve;
  * if called away, sell ATM cash-secured puts only on the called-away sleeve cash;
  * once put-assigned, sell covered calls again on the reassigned wheel sleeve;
  * reinvest half of call/put premiums into uncovered IBIT and half into SATA;
  * leave ASST uncovered and mark it to cached daily closes;
  * model SATA pro forma at $100 par with daily-compounded income.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.market_data import OptionPriceBar, PriceHistory
from app.models.portfolio import Holding, PortfolioSnapshot


@dataclass(frozen=True)
class HoldingTotals:
    ibit_shares: float
    asst_shares: float
    sata_value: float
    snapshot_id: int
    snapshot_created_at: datetime


@dataclass(frozen=True)
class PriceSeries:
    symbol: str
    closes: dict[date, float]

    @property
    def first_date(self) -> date:
        return min(self.closes)

    @property
    def last_date(self) -> date:
        return max(self.closes)

    def close_on_or_before(self, target: date) -> float | None:
        if target in self.closes:
            return self.closes[target]
        candidates = [day for day in self.closes if day <= target]
        if not candidates:
            return None
        return self.closes[max(candidates)]


@dataclass(frozen=True)
class SelectedOption:
    option_symbol: str
    option_type: str
    entry_date: date
    expiration: date
    strike: float
    premium: float
    spot: float

    @property
    def dte(self) -> int:
        return (self.expiration - self.entry_date).days

    @property
    def moneyness(self) -> float:
        return self.strike / self.spot if self.spot else 0.0


@dataclass(frozen=True)
class CycleResult:
    cycle: int
    trade_type: str
    entry_date: date
    expiration: date
    dte: int
    strike: float
    entry_spot: float
    expiration_spot: float
    contracts: int
    covered_shares: int
    premium: float
    intrinsic_paid: float
    net_option_pnl: float
    ibit_bought: float
    sata_contribution: float
    assigned: bool
    ibit_shares_after: float
    uncovered_ibit_shares_after: float
    wheel_ibit_shares_after: float
    cash_after: float
    secured_cash_after: float
    strategy_value_after: float
    hold_value_after: float


@dataclass(frozen=True)
class BacktestResult:
    cycles: list[CycleResult]
    holdings: HoldingTotals
    start: date
    end: date
    initial_value: float
    final_strategy_value: float
    final_hold_value: float
    strategy_return: float
    hold_return: float
    excess_return: float
    annualized_strategy_return: float
    annualized_hold_return: float
    total_premium: float
    total_intrinsic_paid: float
    total_net_option_pnl: float
    total_ibit_reinvestment: float
    total_sata_contribution: float
    final_ibit_shares: float
    final_uncovered_ibit_shares: float
    final_wheel_ibit_shares: float
    final_sata_value: float
    final_cash: float
    final_secured_cash: float
    assignment_rate: float
    call_cycles: int
    put_cycles: int
    max_drawdown_strategy: float
    max_drawdown_hold: float
    skipped_entries: int
    assumptions: list[str]


def main() -> int:
    args = parse_args()
    with SessionLocal() as db:
        holdings = load_holdings(db, args.snapshot_id, include_existing_sata=args.include_existing_sata)
        result = run_backtest(db, holdings, args)

    print_summary(result)
    if args.cycles_csv:
        write_cycles_csv(result.cycles, args.cycles_csv)
        print(f"\nCycle detail written to {args.cycles_csv}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward IBIT ATM covered-call wheel backtest.")
    parser.add_argument("--snapshot-id", type=int, default=None, help="Portfolio snapshot id. Defaults to latest snapshot.")
    parser.add_argument("--start", type=date.fromisoformat, default=None, help="Backtest start date, YYYY-MM-DD.")
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="Backtest end date, YYYY-MM-DD.")
    parser.add_argument("--coverage", type=float, default=0.50, help="Fraction of IBIT shares to cover with calls.")
    parser.add_argument("--dte-min", type=int, default=5, help="Minimum call DTE.")
    parser.add_argument("--dte-max", type=int, default=10, help="Maximum call DTE.")
    parser.add_argument("--target-dte", type=int, default=7, help="Preferred DTE inside the DTE window.")
    parser.add_argument("--sata-rate", type=float, default=settings.default_sata_annual_rate, help="Annual SATA yield.")
    parser.add_argument("--sata-price", type=float, default=settings.default_sata_price, help="Assumed SATA par/mark price.")
    parser.add_argument(
        "--include-existing-sata",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include existing SATA from the portfolio snapshot in both strategy and hold baseline.",
    )
    parser.add_argument(
        "--assignment-mode",
        choices=("wheel", "rebuy", "called-away"),
        default="wheel",
        help=(
            "wheel sells CSPs on called-away sleeve cash until reassigned; rebuy keeps IBIT share count "
            "by subtracting intrinsic; called-away leaves assignment proceeds as cash."
        ),
    )
    parser.add_argument("--cycles-csv", type=Path, default=None, help="Optional CSV path for per-cycle rows.")
    return parser.parse_args()


def load_holdings(db: Session, snapshot_id: int | None, *, include_existing_sata: bool) -> HoldingTotals:
    snapshot = latest_snapshot(db) if snapshot_id is None else db.get(PortfolioSnapshot, snapshot_id)
    if snapshot is None:
        raise SystemExit("No portfolio snapshot is available.")

    rows = (
        db.query(Holding.symbol, func.sum(Holding.quantity), func.sum(Holding.current_value))
        .filter(Holding.snapshot_id == snapshot.id, Holding.symbol.in_(("IBIT", "ASST", "SATA")))
        .group_by(Holding.symbol)
        .all()
    )
    by_symbol = {symbol.upper(): (float(quantity or 0.0), float(value or 0.0)) for symbol, quantity, value in rows}
    ibit_shares = by_symbol.get("IBIT", (0.0, 0.0))[0]
    asst_shares = by_symbol.get("ASST", (0.0, 0.0))[0]
    sata_value = by_symbol.get("SATA", (0.0, 0.0))[1] if include_existing_sata else 0.0
    if ibit_shares <= 0:
        raise SystemExit(f"Snapshot {snapshot.id} has no IBIT shares.")
    if asst_shares <= 0:
        raise SystemExit(f"Snapshot {snapshot.id} has no ASST shares.")
    return HoldingTotals(
        ibit_shares=ibit_shares,
        asst_shares=asst_shares,
        sata_value=sata_value,
        snapshot_id=snapshot.id,
        snapshot_created_at=snapshot.created_at,
    )


def latest_snapshot(db: Session) -> PortfolioSnapshot | None:
    return db.query(PortfolioSnapshot).order_by(PortfolioSnapshot.created_at.desc(), PortfolioSnapshot.id.desc()).first()


def run_backtest(db: Session, holdings: HoldingTotals, args: argparse.Namespace) -> BacktestResult:
    ibit_prices = load_price_series(db, "IBIT")
    asst_prices = load_price_series(db, "ASST")
    option_start, option_end = option_bar_bounds(db, "IBIT")
    start = max(args.start or option_start, ibit_prices.first_date, asst_prices.first_date, option_start)
    end = min(args.end or ibit_prices.last_date, ibit_prices.last_date, asst_prices.last_date, option_end)
    if end <= start:
        raise SystemExit(f"No overlapping IBIT/ASST/option data for requested window: {start} to {end}.")

    initial_ibit = ibit_prices.close_on_or_before(start)
    initial_asst = asst_prices.close_on_or_before(start)
    final_ibit = ibit_prices.close_on_or_before(end)
    final_asst = asst_prices.close_on_or_before(end)
    if initial_ibit is None or initial_asst is None or final_ibit is None or final_asst is None:
        raise SystemExit("Missing start/end underlying prices.")

    if args.assignment_mode == "wheel":
        return run_wheel_backtest(
            db,
            holdings,
            args,
            ibit_prices=ibit_prices,
            asst_prices=asst_prices,
            start=start,
            end=end,
            initial_ibit=initial_ibit,
            initial_asst=initial_asst,
            final_ibit=final_ibit,
            final_asst=final_asst,
        )

    ibit_shares = holdings.ibit_shares
    asst_shares = holdings.asst_shares
    sata_value = holdings.sata_value
    hold_sata_value = holdings.sata_value
    cash = 0.0
    initial_value = ibit_shares * initial_ibit + asst_shares * initial_asst + sata_value
    current_date = start
    entry_date = start
    cycle = 0
    skipped_entries = 0
    cycles: list[CycleResult] = []
    strategy_values = [(start, initial_value)]
    hold_values = [(start, initial_value)]

    while entry_date < end:
        selected = select_atm_call(
            db,
            entry_date,
            ibit_prices,
            dte_min=args.dte_min,
            dte_max=args.dte_max,
            target_dte=args.target_dte,
            end=end,
        )
        if selected is None:
            skipped_entries += 1
            next_entry = next_trading_day_after(ibit_prices, entry_date)
            if next_entry is None:
                break
            entry_date = next_entry
            continue

        contracts = int((ibit_shares * args.coverage) // 100)
        if contracts <= 0:
            break

        sata_value = compound_sata(sata_value, current_date, selected.entry_date, args.sata_rate)
        hold_sata_value = compound_sata(hold_sata_value, current_date, selected.entry_date, args.sata_rate)
        premium = selected.premium * contracts * 100
        ibit_reinvestment = premium * 0.50
        sata_contribution = premium - ibit_reinvestment
        ibit_bought = ibit_reinvestment / selected.spot if selected.spot else 0.0
        ibit_shares += ibit_bought
        sata_value += sata_contribution

        expiration_spot = ibit_prices.close_on_or_before(selected.expiration)
        asst_spot = asst_prices.close_on_or_before(selected.expiration)
        if expiration_spot is None or asst_spot is None:
            break

        sata_value = compound_sata(sata_value, selected.entry_date, selected.expiration, args.sata_rate)
        hold_sata_value = compound_sata(hold_sata_value, selected.entry_date, selected.expiration, args.sata_rate)
        covered_shares = contracts * 100
        intrinsic_paid = max(expiration_spot - selected.strike, 0.0) * covered_shares
        assigned = intrinsic_paid > 0
        if args.assignment_mode == "called-away" and assigned:
            ibit_shares -= covered_shares
            cash += selected.strike * covered_shares
            intrinsic_paid = 0.0
        else:
            cash -= intrinsic_paid

        cycle += 1
        strategy_value = ibit_shares * expiration_spot + asst_shares * asst_spot + sata_value + cash
        hold_value = holdings.ibit_shares * expiration_spot + asst_shares * asst_spot + hold_sata_value
        cycles.append(
            CycleResult(
                cycle=cycle,
                trade_type="call",
                entry_date=selected.entry_date,
                expiration=selected.expiration,
                dte=selected.dte,
                strike=selected.strike,
                entry_spot=selected.spot,
                expiration_spot=expiration_spot,
                contracts=contracts,
                covered_shares=covered_shares,
                premium=premium,
                intrinsic_paid=intrinsic_paid,
                net_option_pnl=premium - intrinsic_paid,
                ibit_bought=ibit_bought,
                sata_contribution=sata_contribution,
                assigned=assigned,
                ibit_shares_after=ibit_shares,
                uncovered_ibit_shares_after=ibit_shares,
                wheel_ibit_shares_after=0.0,
                cash_after=cash,
                secured_cash_after=0.0,
                strategy_value_after=strategy_value,
                hold_value_after=hold_value,
            )
        )
        strategy_values.append((selected.expiration, strategy_value))
        hold_values.append((selected.expiration, hold_value))
        current_date = selected.expiration
        next_entry = next_trading_day_after(ibit_prices, selected.expiration)
        if next_entry is None:
            break
        entry_date = next_entry

    sata_value = compound_sata(sata_value, current_date, end, args.sata_rate)
    hold_sata_value = compound_sata(hold_sata_value, current_date, end, args.sata_rate)
    final_strategy_value = ibit_shares * final_ibit + asst_shares * final_asst + sata_value + cash
    final_hold_value = holdings.ibit_shares * final_ibit + asst_shares * final_asst + hold_sata_value
    strategy_values.append((end, final_strategy_value))
    hold_values.append((end, final_hold_value))
    days = max((end - start).days, 1)

    assumptions = [
        f"Latest imported snapshot {holdings.snapshot_id} from {holdings.snapshot_created_at:%Y-%m-%d %H:%M:%S} is used for share counts.",
        f"IBIT calls are selected walk-forward from cached 1d option bars: closest to {args.target_dte} DTE inside {args.dte_min}-{args.dte_max} DTE, then closest strike to spot.",
        f"Coverage uses whole contracts: floor(current IBIT shares x {args.coverage:.0%} / 100).",
        "Premium is reinvested at the entry close: 50% IBIT and 50% SATA.",
        f"SATA is modeled pro forma at ${args.sata_price:.2f} par with {args.sata_rate:.2%} annual income compounded daily; live SATA price history is not in the local cache.",
        "ASST is left uncovered and marked to cached daily closes.",
        "Taxes, commissions, bid/ask slippage, early assignment, margin interest, and dividends other than modeled SATA income are excluded.",
        f"Assignment mode is {args.assignment_mode}.",
    ]

    return BacktestResult(
        cycles=cycles,
        holdings=holdings,
        start=start,
        end=end,
        initial_value=initial_value,
        final_strategy_value=final_strategy_value,
        final_hold_value=final_hold_value,
        strategy_return=final_strategy_value / initial_value - 1,
        hold_return=final_hold_value / initial_value - 1,
        excess_return=final_strategy_value / final_hold_value - 1,
        annualized_strategy_return=annualize(final_strategy_value / initial_value - 1, days),
        annualized_hold_return=annualize(final_hold_value / initial_value - 1, days),
        total_premium=sum(row.premium for row in cycles),
        total_intrinsic_paid=sum(row.intrinsic_paid for row in cycles),
        total_net_option_pnl=sum(row.net_option_pnl for row in cycles),
        total_ibit_reinvestment=sum(row.premium * 0.50 for row in cycles),
        total_sata_contribution=sum(row.sata_contribution for row in cycles),
        final_ibit_shares=ibit_shares,
        final_uncovered_ibit_shares=ibit_shares,
        final_wheel_ibit_shares=0.0,
        final_sata_value=sata_value,
        final_cash=cash,
        final_secured_cash=0.0,
        assignment_rate=mean(1.0 if row.assigned else 0.0 for row in cycles) if cycles else 0.0,
        call_cycles=len(cycles),
        put_cycles=0,
        max_drawdown_strategy=max_drawdown([value for _, value in strategy_values]),
        max_drawdown_hold=max_drawdown([value for _, value in hold_values]),
        skipped_entries=skipped_entries,
        assumptions=assumptions,
    )


def run_wheel_backtest(
    db: Session,
    holdings: HoldingTotals,
    args: argparse.Namespace,
    *,
    ibit_prices: PriceSeries,
    asst_prices: PriceSeries,
    start: date,
    end: date,
    initial_ibit: float,
    initial_asst: float,
    final_ibit: float,
    final_asst: float,
) -> BacktestResult:
    initial_wheel_ibit_shares = int((holdings.ibit_shares * args.coverage) // 100) * 100
    wheel_ibit_shares = initial_wheel_ibit_shares
    uncovered_ibit_shares = holdings.ibit_shares - wheel_ibit_shares
    asst_shares = holdings.asst_shares
    sata_value = holdings.sata_value
    hold_sata_value = holdings.sata_value
    secured_cash = 0.0
    initial_value = holdings.ibit_shares * initial_ibit + asst_shares * initial_asst + sata_value
    current_date = start
    entry_date = start
    cycle = 0
    skipped_entries = 0
    cycles: list[CycleResult] = []
    strategy_values = [(start, initial_value)]
    hold_values = [(start, initial_value)]

    while entry_date < end:
        trade_type = "call" if wheel_ibit_shares >= 100 else "put"
        selected = select_atm_option(
            db,
            entry_date,
            ibit_prices,
            option_type=trade_type,
            dte_min=args.dte_min,
            dte_max=args.dte_max,
            target_dte=args.target_dte,
            end=end,
        )
        if selected is None:
            skipped_entries += 1
            next_entry = next_trading_day_after(ibit_prices, entry_date)
            if next_entry is None:
                break
            entry_date = next_entry
            continue

        if trade_type == "call":
            contracts = int(wheel_ibit_shares // 100)
        else:
            contracts = int(secured_cash // (selected.strike * 100)) if selected.strike > 0 else 0
        if contracts <= 0:
            break

        sata_value = compound_sata(sata_value, current_date, selected.entry_date, args.sata_rate)
        hold_sata_value = compound_sata(hold_sata_value, current_date, selected.entry_date, args.sata_rate)
        premium = selected.premium * contracts * 100
        ibit_reinvestment = premium * 0.50
        sata_contribution = premium - ibit_reinvestment
        ibit_bought = ibit_reinvestment / selected.spot if selected.spot else 0.0
        uncovered_ibit_shares += ibit_bought
        sata_value += sata_contribution

        expiration_spot = ibit_prices.close_on_or_before(selected.expiration)
        asst_spot = asst_prices.close_on_or_before(selected.expiration)
        if expiration_spot is None or asst_spot is None:
            break

        sata_value = compound_sata(sata_value, selected.entry_date, selected.expiration, args.sata_rate)
        hold_sata_value = compound_sata(hold_sata_value, selected.entry_date, selected.expiration, args.sata_rate)
        option_shares = contracts * 100
        if trade_type == "call":
            intrinsic_paid = max(expiration_spot - selected.strike, 0.0) * option_shares
            assigned = intrinsic_paid > 0
            if assigned:
                wheel_ibit_shares -= option_shares
                secured_cash += selected.strike * option_shares
        else:
            intrinsic_paid = max(selected.strike - expiration_spot, 0.0) * option_shares
            assigned = intrinsic_paid > 0
            if assigned:
                secured_cash -= selected.strike * option_shares
                wheel_ibit_shares += option_shares

        cycle += 1
        ibit_shares = uncovered_ibit_shares + wheel_ibit_shares
        strategy_value = ibit_shares * expiration_spot + asst_shares * asst_spot + sata_value + secured_cash
        hold_value = holdings.ibit_shares * expiration_spot + asst_shares * asst_spot + hold_sata_value
        cycles.append(
            CycleResult(
                cycle=cycle,
                trade_type=trade_type,
                entry_date=selected.entry_date,
                expiration=selected.expiration,
                dte=selected.dte,
                strike=selected.strike,
                entry_spot=selected.spot,
                expiration_spot=expiration_spot,
                contracts=contracts,
                covered_shares=option_shares,
                premium=premium,
                intrinsic_paid=intrinsic_paid,
                net_option_pnl=premium - intrinsic_paid,
                ibit_bought=ibit_bought,
                sata_contribution=sata_contribution,
                assigned=assigned,
                ibit_shares_after=ibit_shares,
                uncovered_ibit_shares_after=uncovered_ibit_shares,
                wheel_ibit_shares_after=wheel_ibit_shares,
                cash_after=secured_cash,
                secured_cash_after=secured_cash,
                strategy_value_after=strategy_value,
                hold_value_after=hold_value,
            )
        )
        strategy_values.append((selected.expiration, strategy_value))
        hold_values.append((selected.expiration, hold_value))
        current_date = selected.expiration
        next_entry = next_trading_day_after(ibit_prices, selected.expiration)
        if next_entry is None:
            break
        entry_date = next_entry

    sata_value = compound_sata(sata_value, current_date, end, args.sata_rate)
    hold_sata_value = compound_sata(hold_sata_value, current_date, end, args.sata_rate)
    final_ibit_shares = uncovered_ibit_shares + wheel_ibit_shares
    final_strategy_value = final_ibit_shares * final_ibit + asst_shares * final_asst + sata_value + secured_cash
    final_hold_value = holdings.ibit_shares * final_ibit + asst_shares * final_asst + hold_sata_value
    strategy_values.append((end, final_strategy_value))
    hold_values.append((end, final_hold_value))
    days = max((end - start).days, 1)

    assumptions = [
        f"Latest imported snapshot {holdings.snapshot_id} from {holdings.snapshot_created_at:%Y-%m-%d %H:%M:%S} is used for share counts.",
        f"IBIT starts with {initial_wheel_ibit_shares:,.0f} whole-contract shares in the wheel sleeve and {holdings.ibit_shares - initial_wheel_ibit_shares:,.3f} shares uncovered.",
        f"Calls/puts are selected walk-forward from cached 1d option bars: closest to {args.target_dte} DTE inside {args.dte_min}-{args.dte_max} DTE, then closest strike to spot.",
        "Calls are sold only on the wheel sleeve. After a call assignment, the called-away proceeds become secured cash for ATM CSPs until put assignment restores the wheel sleeve.",
        "IBIT bought with premium is added to the uncovered sleeve and is not used to sell additional covered calls.",
        "Premium is reinvested at the entry close: 50% IBIT and 50% SATA.",
        f"SATA is modeled pro forma at ${args.sata_price:.2f} par with {args.sata_rate:.2%} annual income compounded daily; live SATA price history is not in the local cache.",
        "ASST is left uncovered and marked to cached daily closes.",
        "Taxes, commissions, bid/ask slippage, early assignment, margin interest, and dividends other than modeled SATA income are excluded.",
        "Assignment is approximated from expiration close: calls assign when IBIT closes above strike; puts assign when IBIT closes below strike.",
    ]

    return BacktestResult(
        cycles=cycles,
        holdings=holdings,
        start=start,
        end=end,
        initial_value=initial_value,
        final_strategy_value=final_strategy_value,
        final_hold_value=final_hold_value,
        strategy_return=final_strategy_value / initial_value - 1,
        hold_return=final_hold_value / initial_value - 1,
        excess_return=final_strategy_value / final_hold_value - 1,
        annualized_strategy_return=annualize(final_strategy_value / initial_value - 1, days),
        annualized_hold_return=annualize(final_hold_value / initial_value - 1, days),
        total_premium=sum(row.premium for row in cycles),
        total_intrinsic_paid=sum(row.intrinsic_paid for row in cycles),
        total_net_option_pnl=sum(row.net_option_pnl for row in cycles),
        total_ibit_reinvestment=sum(row.premium * 0.50 for row in cycles),
        total_sata_contribution=sum(row.sata_contribution for row in cycles),
        final_ibit_shares=final_ibit_shares,
        final_uncovered_ibit_shares=uncovered_ibit_shares,
        final_wheel_ibit_shares=wheel_ibit_shares,
        final_sata_value=sata_value,
        final_cash=secured_cash,
        final_secured_cash=secured_cash,
        assignment_rate=mean(1.0 if row.assigned else 0.0 for row in cycles) if cycles else 0.0,
        call_cycles=sum(1 for row in cycles if row.trade_type == "call"),
        put_cycles=sum(1 for row in cycles if row.trade_type == "put"),
        max_drawdown_strategy=max_drawdown([value for _, value in strategy_values]),
        max_drawdown_hold=max_drawdown([value for _, value in hold_values]),
        skipped_entries=skipped_entries,
        assumptions=assumptions,
    )


def load_price_series(db: Session, symbol: str) -> PriceSeries:
    rows = (
        db.query(PriceHistory.date_time, PriceHistory.close)
        .filter(PriceHistory.provider == "massive", PriceHistory.symbol == symbol, PriceHistory.interval == "1d")
        .order_by(PriceHistory.date_time.asc())
        .all()
    )
    closes = {row.date_time.date(): float(row.close) for row in rows}
    if not closes:
        raise SystemExit(f"No cached price history for {symbol}.")
    return PriceSeries(symbol=symbol, closes=closes)


def option_bar_bounds(db: Session, symbol: str) -> tuple[date, date]:
    row = (
        db.query(func.min(OptionPriceBar.date_time), func.max(OptionPriceBar.expiration))
        .filter(
            OptionPriceBar.provider == "massive",
            OptionPriceBar.underlying == symbol,
            OptionPriceBar.option_type.in_(("call", "put")),
            OptionPriceBar.interval == "1d",
        )
        .one()
    )
    if row[0] is None or row[1] is None:
        raise SystemExit(f"No cached option bars for {symbol}.")
    return row[0].date(), row[1]


def select_atm_call(
    db: Session,
    entry_date: date,
    prices: PriceSeries,
    *,
    dte_min: int,
    dte_max: int,
    target_dte: int,
    end: date,
) -> SelectedOption | None:
    return select_atm_option(
        db,
        entry_date,
        prices,
        option_type="call",
        dte_min=dte_min,
        dte_max=dte_max,
        target_dte=target_dte,
        end=end,
    )


def select_atm_option(
    db: Session,
    entry_date: date,
    prices: PriceSeries,
    *,
    option_type: str,
    dte_min: int,
    dte_max: int,
    target_dte: int,
    end: date,
) -> SelectedOption | None:
    spot = prices.close_on_or_before(entry_date)
    if spot is None:
        return None
    rows = (
        db.query(OptionPriceBar)
        .filter(
            OptionPriceBar.provider == "massive",
            OptionPriceBar.underlying == "IBIT",
            OptionPriceBar.option_type == option_type,
            OptionPriceBar.interval == "1d",
            func.date(OptionPriceBar.date_time) == entry_date.isoformat(),
            OptionPriceBar.expiration <= end,
            OptionPriceBar.close > 0,
        )
        .all()
    )
    candidates = []
    for row in rows:
        row_date = row.date_time.date()
        dte = (row.expiration - row_date).days
        if dte_min <= dte <= dte_max:
            candidates.append(row)
    if not candidates:
        return None
    selected = min(
        candidates,
        key=lambda row: (
            abs((row.expiration - row.date_time.date()).days - target_dte),
            abs(float(row.strike) - spot),
            row.expiration,
        ),
    )
    return SelectedOption(
        option_symbol=selected.option_symbol,
        option_type=option_type,
        entry_date=entry_date,
        expiration=selected.expiration,
        strike=float(selected.strike),
        premium=float(selected.close),
        spot=spot,
    )


def next_trading_day_after(prices: PriceSeries, after_date: date) -> date | None:
    candidates = [day for day in prices.closes if day > after_date]
    if not candidates:
        return None
    return min(candidates)


def compound_sata(value: float, start: date, end: date, annual_rate: float) -> float:
    days = max((end - start).days, 0)
    if value <= 0 or days <= 0:
        return value
    return value * ((1 + annual_rate / 365) ** days)


def annualize(total_return: float, days: int) -> float:
    if total_return <= -1:
        return -1.0
    return (1 + total_return) ** (365 / days) - 1


def max_drawdown(values: list[float]) -> float:
    peak = values[0] if values else 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst


def money(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float) -> str:
    return f"{value:.2%}"


def print_summary(result: BacktestResult) -> None:
    premiums = [row.premium for row in result.cycles]
    net_pnl = [row.net_option_pnl for row in result.cycles]
    print("IBIT ATM covered-call wheel walk-forward backtest")
    print(f"Window: {result.start} to {result.end} ({len(result.cycles)} cycles)")
    print(
        f"Position sizing: IBIT {result.holdings.ibit_shares:,.3f} sh, "
        f"ASST {result.holdings.asst_shares:,.3f} sh, starting SATA {money(result.holdings.sata_value)}"
    )
    print("")
    print("Ending values")
    print(f"  Strategy: {money(result.final_strategy_value)} ({pct(result.strategy_return)}, annualized {pct(result.annualized_strategy_return)})")
    print(f"  Buy/hold: {money(result.final_hold_value)} ({pct(result.hold_return)}, annualized {pct(result.annualized_hold_return)})")
    print(f"  Strategy vs buy/hold: {money(result.final_strategy_value - result.final_hold_value)} ({pct(result.excess_return)})")
    print("")
    print("Option and reinvestment ledger")
    print(f"  Premium collected: {money(result.total_premium)}")
    print(f"  Intrinsic paid/foregone on option sleeve: {money(result.total_intrinsic_paid)}")
    print(f"  Net option P&L before reinvestment compounding: {money(result.total_net_option_pnl)}")
    print(f"  Reinvested into IBIT: {money(result.total_ibit_reinvestment)}")
    print(f"  Contributed into SATA: {money(result.total_sata_contribution)}")
    print(f"  Final IBIT shares: {result.final_ibit_shares:,.3f}")
    print(
        f"    Uncovered IBIT: {result.final_uncovered_ibit_shares:,.3f}; "
        f"wheel-sleeve IBIT: {result.final_wheel_ibit_shares:,.3f}"
    )
    print(f"  Final SATA value: {money(result.final_sata_value)}")
    print(f"  Final cash from option settlement: {money(result.final_cash)}")
    if result.final_secured_cash:
        print(f"    Secured cash awaiting CSP assignment: {money(result.final_secured_cash)}")
    print("")
    print("Risk/path stats")
    print(f"  Call cycles: {result.call_cycles}; put cycles: {result.put_cycles}")
    print(f"  Assignment/ITM rate: {pct(result.assignment_rate)}")
    print(f"  Max drawdown, strategy: {pct(result.max_drawdown_strategy)}")
    print(f"  Max drawdown, buy/hold: {pct(result.max_drawdown_hold)}")
    print(f"  Skipped entry days without eligible call bars: {result.skipped_entries}")
    if premiums:
        print(f"  Median weekly premium: {money(median(premiums))}; average weekly premium: {money(mean(premiums))}")
    if net_pnl:
        print(f"  Median weekly net option P&L: {money(median(net_pnl))}; average weekly net option P&L: {money(mean(net_pnl))}")
    print("")
    print("Assumptions")
    for assumption in result.assumptions:
        print(f"  - {assumption}")


def write_cycles_csv(cycles: list[CycleResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(CycleResult.__dataclass_fields__)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in cycles:
            writer.writerow({field: getattr(row, field) for field in fields})


if __name__ == "__main__":
    raise SystemExit(main())
