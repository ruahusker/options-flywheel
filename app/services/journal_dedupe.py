"""Detect and collapse spurious duplicate journal rows from Fidelity history imports.

Two distinct duplication mechanisms exist, and they must be treated differently from genuine
repeated fills (a 34-contract order really can print as 1+1+1+31 rows with identical amounts):

* Dual-section exports: some "Accounts_History" downloads list every transaction twice — a
  combined all-accounts section followed by per-account sections. Detected per file: when most
  of a file's rows have an in-file twin, the file is dual-section and each duplicate cluster is
  halved. In a normal file, identical in-file rows are separate fills and are kept.
* Overlapping re-downloads: a later export covering the same dates re-lists earlier trades,
  often with amounts differing by a cent (Fidelity rounding), which defeats equality dedupe.
  Cross-file clusters keep only the earliest file's copies, matching amounts within AMOUNT_TOL.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Callable, Sequence

# Fidelity rounds per-section/per-download differently by up to a cent; two copies of one
# transaction can differ by $0.01-0.02 while two same-size real fills minutes apart differ more.
AMOUNT_TOL = 0.02

# Fraction of a file's rows that must have an in-file twin before the file is treated as a
# dual-section export (where it is ~1.0) rather than a file with a few repeated fills (~0.05).
DUAL_SECTION_COVERAGE = 0.5

_SOURCE_RE = re.compile(r"Imported from (.+?) line (\d+)$")


def parse_source(notes: str | None) -> tuple[str, int] | None:
    """Extract (filename, line) from an imported entry's notes; None for manual entries."""
    match = _SOURCE_RE.search(notes or "")
    return (match.group(1), int(match.group(2))) if match else None


def _transaction_key(entry: Any) -> tuple:
    return (
        entry.created_at,
        entry.account_number,
        entry.ticker,
        entry.action,
        entry.contracts,
        entry.strike,
        entry.expiration,
    )


def _amount(entry: Any) -> float:
    return float(entry.credit_debit or 0.0)


def cluster_duplicates(entries: Sequence[Any], sort_key: Callable[[Any], Any]) -> list[list[Any]]:
    """Group entries that look like copies of one transaction: identical on every field except
    the amount, with amounts chained within AMOUNT_TOL. Returns clusters ordered by sort_key."""
    by_key: dict[tuple, list[Any]] = defaultdict(list)
    for entry in entries:
        by_key[_transaction_key(entry)].append(entry)

    clusters: list[list[Any]] = []
    for group in by_key.values():
        group.sort(key=_amount)
        cluster = [group[0]]
        for entry in group[1:]:
            if _amount(entry) - _amount(cluster[-1]) <= AMOUNT_TOL:
                cluster.append(entry)
            else:
                clusters.append(cluster)
                cluster = [entry]
        clusters.append(cluster)
    for cluster in clusters:
        cluster.sort(key=sort_key)
    return clusters


def is_dual_section(entries: Sequence[Any], sort_key: Callable[[Any], Any]) -> bool:
    """True when most rows of a single file have an in-file twin (the file lists every
    transaction in both a combined and a per-account section)."""
    if len(entries) < 4:
        return False
    clusters = cluster_duplicates(entries, sort_key)
    duplicated = sum(len(c) for c in clusters if len(c) > 1)
    return duplicated / len(entries) >= DUAL_SECTION_COVERAGE


def collapse_dual_section(entries: Sequence[Any], sort_key: Callable[[Any], Any]) -> list[Any]:
    """For a dual-section file, keep half of each duplicate cluster (a transaction with n real
    fills appears 2n times, so ceil(n/2) survives). Non-dual-section input is returned as is."""
    if not is_dual_section(entries, sort_key):
        return list(entries)
    kept: list[Any] = []
    for cluster in cluster_duplicates(entries, sort_key):
        kept.extend(cluster[: (len(cluster) + 1) // 2])
    kept.sort(key=sort_key)
    return kept
