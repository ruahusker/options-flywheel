#!/usr/bin/env python
"""One-time cleanup of spurious duplicate trade-journal rows from Fidelity history imports.

Removes two kinds of duplicates while keeping genuine repeated fills (see
app/services/journal_dedupe.py for the distinction):
  * dual-section exports that list every transaction twice within one file;
  * overlapping re-downloads that re-list earlier trades with penny-different amounts.

Dry run by default; pass --apply to delete the rows and clear the precompute cache so pages
rebuild from the cleaned journal.

    python scripts/cleanup_journal_duplicates.py
    python scripts/cleanup_journal_duplicates.py --apply
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.models.journal import TradeJournalEntry
from app.models.market_data import PrecomputeCache
from app.services.journal_dedupe import cluster_duplicates, is_dual_section, parse_source


def find_spurious(entries) -> list[tuple[TradeJournalEntry, str]]:
    """Return (entry, reason) pairs to delete. Manual entries (no import source in notes) are
    never touched and never cause an imported row to be treated as a duplicate."""
    sourced = [(entry, parse_source(entry.notes)) for entry in entries]
    by_file: dict[str, list[TradeJournalEntry]] = defaultdict(list)
    lines = {}
    for entry, source in sourced:
        if source is None:
            continue
        by_file[source[0]].append(entry)
        lines[entry.id] = source[1]

    to_delete: list[tuple[TradeJournalEntry, str]] = []
    deleted_ids: set[int] = set()

    # Pass 1: dual-section files — keep the first half of each in-file duplicate cluster.
    for filename, file_entries in by_file.items():
        line_key = lambda e: lines[e.id]
        if not is_dual_section(file_entries, sort_key=line_key):
            continue
        for cluster in cluster_duplicates(file_entries, sort_key=line_key):
            for entry in cluster[(len(cluster) + 1) // 2 :]:
                to_delete.append((entry, f"dual-section copy in {filename}"))
                deleted_ids.add(entry.id)

    # Pass 2: cross-file re-downloads — keep the earliest-imported file's copies.
    survivors = [entry for entry, source in sourced if source is not None and entry.id not in deleted_ids]
    for cluster in cluster_duplicates(survivors, sort_key=lambda e: e.id):
        files = {parse_source(entry.notes)[0] for entry in cluster}
        if len(files) < 2:
            continue
        keep_file = parse_source(min(cluster, key=lambda e: e.id).notes)[0]
        for entry in cluster:
            filename = parse_source(entry.notes)[0]
            if filename != keep_file:
                to_delete.append((entry, f"re-listed in {filename} (kept copy from {keep_file})"))
    return to_delete


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove spurious duplicate journal rows.")
    parser.add_argument("--apply", action="store_true", help="Delete the rows (default is a dry run).")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        entries = db.query(TradeJournalEntry).order_by(TradeJournalEntry.id).all()
        spurious = find_spurious(entries)

        option_total = sum(float(e.credit_debit or 0.0) for e in entries if (e.contracts or 0) > 0)
        option_excess = sum(float(e.credit_debit or 0.0) for e, _ in spurious if (e.contracts or 0) > 0)
        print(f"journal rows: {len(entries)}, spurious: {len(spurious)}")
        for entry, reason in spurious:
            print(
                f"  delete id={entry.id} {entry.created_at:%Y-%m-%d} {entry.account_name} "
                f"{entry.contracts}x {entry.strike} {float(entry.credit_debit or 0.0):>9.2f}  [{reason}]"
            )
        print(f"net option premium: {option_total:,.2f} -> {option_total - option_excess:,.2f} "
              f"(removing {option_excess:,.2f})")

        if not args.apply:
            print("dry run — pass --apply to delete")
            return 0
        for entry, _ in spurious:
            db.delete(entry)
        cleared = db.query(PrecomputeCache).delete()
        db.commit()
        print(f"deleted {len(spurious)} rows, cleared {cleared} precompute cache entries")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    main()
