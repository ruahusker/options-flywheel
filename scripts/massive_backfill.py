#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.database import SessionLocal, init_db
from app.services.massive_backfill import (
    MassiveBackfillResult,
    MassiveBackfillService,
    latest_contract_expiration,
    latest_stock_bar_date,
    query_focused_contracts_for_bar_backfill,
)
from app.services.massive_client import MassiveClient


PRIMARY_UNDERLYINGS = ("IBIT", "ASST")
FUTURE_BACKFILL_UNDERLYINGS = ("QQQ", "IWM", "MSTR", "XXI", "SPY", "BSOL", "ETHA")


def main() -> int:
    args = parse_args()
    underlyings = parse_underlyings(args.underlying or list(PRIMARY_UNDERLYINGS))
    future_underlyings = future_underlyings_for_args(args)
    if not underlyings:
        raise SystemExit("At least one --underlying is required.")

    init_db()
    result = MassiveBackfillResult()
    with MassiveClient(max_calls=args.max_calls, calls_per_minute=args.calls_per_minute) as client:
        service = MassiveBackfillService(client)
        with SessionLocal() as db:
            if args.mode in {"underlying-bars", "all"}:
                stage = service.backfill_underlying_bars(
                    db,
                    underlyings,
                    start=args.start,
                    end=args.end,
                    interval=args.interval,
                    resume_from_latest=args.resume_underlying,
                    refresh_lookback_days=args.underlying_refresh_lookback_days,
                )
                result.absorb(stage)
                if result.stopped_reason:
                    print_summary(result)
                    return 0

            if args.mode in {"contracts", "all"}:
                stage = service.backfill_contracts(
                    db,
                    underlyings,
                    as_of=args.as_of,
                    start=args.start,
                    end=args.end,
                    include_expired=args.include_expired,
                    include_active=args.include_active,
                    resume_from_latest_expiration=args.resume_contracts,
                )
                result.absorb(stage)
                if result.stopped_reason:
                    print_summary(result)
                    return 0

            if args.mode in {"option-bars", "all"}:
                stage = service.backfill_option_bars(
                    db,
                    underlyings,
                    start=args.start,
                    end=args.end,
                    interval=args.interval,
                    dte_lookback_days=args.dte_lookback_days,
                    max_contracts=args.max_contracts,
                    refresh_existing=args.refresh_existing,
                    focused=args.focused_option_bars,
                )
                result.absorb(stage)

            if args.mode == "focused-cycle":
                # Keep the primary symbols' daily closes current BEFORE anything else: the
                # historical roll backtest scores option outcomes against these closes, and a
                # stale price series silently mis-marks every recent sample. The service skips
                # the API call entirely once bars are current through the end date, so this
                # costs at most one call per symbol per trading day.
                stage = service.backfill_underlying_bars(
                    db,
                    underlyings,
                    start=args.start,
                    end=args.end,
                    interval=args.interval,
                    resume_from_latest=args.resume_underlying,
                    refresh_lookback_days=args.underlying_refresh_lookback_days,
                )
                result.absorb(stage)
                if not result.stopped_reason:
                    if args.future_queue and primary_backfill_complete(db, underlyings, args) and future_underlyings:
                        stage = run_future_backfill_queue(service, db, future_underlyings, args)
                    else:
                        stage = run_focused_cycle(service, db, underlyings, args)
                    result.absorb(stage)

    print_summary(result)
    return 0


def parse_args() -> argparse.Namespace:
    today = date.today()
    default_end = default_backfill_end(today)
    parser = argparse.ArgumentParser(description="Slowly backfill Massive historical options data into the local database.")
    parser.add_argument("--underlying", action="append", default=None, help="Underlying ticker. Repeat or comma-separate.")
    parser.add_argument("--start", type=date.fromisoformat, default=default_end - timedelta(days=730), help="Start date, YYYY-MM-DD.")
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=default_end,
        help="End date, YYYY-MM-DD. Defaults to the last completed weekday so Tradier owns today-forward data.",
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="Massive contract reference as_of date, YYYY-MM-DD.")
    parser.add_argument(
        "--mode",
        choices=["underlying-bars", "contracts", "option-bars", "focused-cycle", "all"],
        default="all",
        help="Backfill stage to run.",
    )
    parser.add_argument("--interval", default="1d", help="Local interval label. Use 1d for daily bars.")
    parser.add_argument("--calls-per-minute", type=float, default=settings.massive_calls_per_minute, help="API throttle.")
    parser.add_argument("--max-calls", type=int, default=5, help="Stop after this many Massive calls.")
    parser.add_argument("--dte-lookback-days", type=int, default=21, help="How far before expiration to fetch option bars.")
    parser.add_argument("--max-contracts", type=int, default=None, help="Limit option contracts processed this run.")
    parser.add_argument("--refresh-existing", action="store_true", help="Refresh option bars even when local bars already exist.")
    parser.add_argument("--focused-option-bars", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--resume-underlying", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--underlying-refresh-lookback-days", type=int, default=7)
    parser.add_argument("--include-expired", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-active", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--resume-contracts", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--future-underlying",
        action="append",
        default=None,
        help="Future-use ticker to backfill after the primary focused queue is complete. Repeat or comma-separate.",
    )
    parser.add_argument(
        "--future-queue",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After the primary focused queue is complete, backfill future-use symbols.",
    )
    return parser.parse_args()


def parse_underlyings(values: list[str]) -> list[str]:
    tickers: list[str] = []
    for value in values:
        tickers.extend(part.strip().upper() for part in value.split(",") if part.strip())
    return sorted(set(tickers))


def future_underlyings_for_args(args: argparse.Namespace) -> list[str]:
    if not args.future_queue:
        return []
    if args.future_underlying is not None:
        return parse_underlyings(args.future_underlying)
    if args.underlying is not None:
        return []
    return parse_underlyings(list(FUTURE_BACKFILL_UNDERLYINGS))


def run_focused_cycle(
    service: MassiveBackfillService,
    db,
    underlyings: list[str],
    args: argparse.Namespace,
) -> MassiveBackfillResult:
    result = MassiveBackfillResult()
    stage = service.backfill_option_bars(
        db,
        underlyings,
        start=args.start,
        end=args.end,
        interval=args.interval,
        dte_lookback_days=args.dte_lookback_days,
        max_contracts=args.max_contracts or args.max_calls,
        refresh_existing=args.refresh_existing,
        focused=True,
    )
    result.absorb(stage)
    if not result.stopped_reason and service.client.calls_made < args.max_calls:
        stage = service.backfill_contracts(
            db,
            underlyings,
            as_of=args.as_of,
            start=args.start,
            end=contract_backfill_end(args.end),
            include_expired=args.include_expired,
            include_active=args.include_active,
            resume_from_latest_expiration=args.resume_contracts,
        )
        result.absorb(stage)
    return result


def run_future_backfill_queue(
    service: MassiveBackfillService,
    db,
    underlyings: list[str],
    args: argparse.Namespace,
) -> MassiveBackfillResult:
    result = MassiveBackfillResult()
    missing_underlying = [
        symbol
        for symbol in underlyings
        if latest_stock_bar_date(db, symbol, interval=args.interval) is None
    ]
    if missing_underlying:
        stage = service.backfill_underlying_bars(
            db,
            missing_underlying,
            start=args.start,
            end=args.end,
            interval=args.interval,
            resume_from_latest=args.resume_underlying,
            refresh_lookback_days=args.underlying_refresh_lookback_days,
        )
        result.absorb(stage)
        if result.stopped_reason or service.client.calls_made >= args.max_calls:
            return result

    stage = run_focused_cycle(service, db, underlyings, args)
    result.absorb(stage)
    return result


def primary_backfill_complete(db, underlyings: list[str], args: argparse.Namespace) -> bool:
    if any(latest_stock_bar_date(db, symbol, interval=args.interval) is None for symbol in underlyings):
        return False
    if any(
        (latest_contract_expiration(db, symbol) or date.min) < contract_backfill_end(args.end)
        for symbol in underlyings
    ):
        return False
    remaining = query_focused_contracts_for_bar_backfill(
        db,
        underlyings,
        start=args.start,
        end=args.end,
        interval=args.interval,
        dte_lookback_days=args.dte_lookback_days,
        max_rows=1,
    )
    return not remaining


def contract_backfill_end(value: date) -> date:
    target = value
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target


def default_backfill_end(today: date | None = None) -> date:
    """Massive is historical-only; leave the current session to Tradier."""
    today = today or date.today()
    return contract_backfill_end(today - timedelta(days=1))


def print_summary(result: MassiveBackfillResult) -> None:
    print(f"Massive calls made: {result.calls_made}")
    print(f"Contracts seen/inserted/updated: {result.contracts_seen}/{result.contracts_inserted}/{result.contracts_updated}")
    print(f"Option bars seen/inserted/updated: {result.option_bars_seen}/{result.option_bars_inserted}/{result.option_bars_updated}")
    print(f"Underlying bars seen/inserted/updated: {result.stock_bars_seen}/{result.stock_bars_inserted}/{result.stock_bars_updated}")
    if result.stopped_reason:
        print(f"Stopped: {result.stopped_reason}")


if __name__ == "__main__":
    raise SystemExit(main())
