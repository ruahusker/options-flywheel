#!/usr/bin/env python
"""Scheduled market-data refresh: fetch live data from the provider into the cache and rebuild the
precomputed pages. Run on a 15-minute timer during market hours (the Tradier clock gates fetching;
use --force to bypass the gate for a manual run).

    python scripts/refresh_market_data.py
    python scripts/refresh_market_data.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.services.market_data import get_refresh_provider
from app.services.market_refresh import run_refresh


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh cached market data and precomputed pages.")
    parser.add_argument("--force", action="store_true", help="Fetch even when the market clock is closed/premarket.")
    args = parser.parse_args()

    init_db()
    provider = get_refresh_provider()
    db = SessionLocal()
    try:
        result = run_refresh(db, provider, force=args.force)
    finally:
        db.close()

    if result.get("skipped"):
        print(f"Skipped: market is '{result.get('market_status')}'. Use --force to fetch anyway.")
        return 0

    market = result["market"]
    print(f"Market status: {market['market_status']}")
    for symbol, counts in market["symbols"].items():
        print(f"  {symbol}: {counts['bars']} bars, {counts['expirations']} expirations, {counts['chains_cached']} chains cached")
    errors = result.get("precompute_errors") or []
    print(f"Precomputed pages rebuilt ({'no errors' if not errors else f'{len(errors)} errors'}).")
    for err in errors:
        print(f"  precompute error: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
