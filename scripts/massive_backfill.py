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
from app.services.massive_backfill import MassiveBackfillResult, MassiveBackfillService
from app.services.massive_client import MassiveClient


def main() -> int:
    args = parse_args()
    underlyings = parse_underlyings(args.underlying or ["IBIT", "ASST"])
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
                if not result.stopped_reason and client.calls_made < args.max_calls:
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

    print_summary(result)
    return 0


def parse_args() -> argparse.Namespace:
    today = date.today()
    parser = argparse.ArgumentParser(description="Slowly backfill Massive historical options data into the local database.")
    parser.add_argument("--underlying", action="append", default=None, help="Underlying ticker. Repeat or comma-separate.")
    parser.add_argument("--start", type=date.fromisoformat, default=today - timedelta(days=730), help="Start date, YYYY-MM-DD.")
    parser.add_argument("--end", type=date.fromisoformat, default=today, help="End date, YYYY-MM-DD.")
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
    return parser.parse_args()


def parse_underlyings(values: list[str]) -> list[str]:
    tickers: list[str] = []
    for value in values:
        tickers.extend(part.strip().upper() for part in value.split(",") if part.strip())
    return sorted(set(tickers))


def print_summary(result: MassiveBackfillResult) -> None:
    print(f"Massive calls made: {result.calls_made}")
    print(f"Contracts seen/inserted/updated: {result.contracts_seen}/{result.contracts_inserted}/{result.contracts_updated}")
    print(f"Option bars seen/inserted/updated: {result.option_bars_seen}/{result.option_bars_inserted}/{result.option_bars_updated}")
    print(f"Underlying bars seen/inserted/updated: {result.stock_bars_seen}/{result.stock_bars_inserted}/{result.stock_bars_updated}")
    if result.stopped_reason:
        print(f"Stopped: {result.stopped_reason}")


if __name__ == "__main__":
    raise SystemExit(main())
