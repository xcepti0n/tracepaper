"""Which indexed items no longer belong, and why.

Exclusion rules change over time -- a new default, a folder you marked as
code, a pattern for build output. None of that touches what is already in the
index: the scanner only decides what to add next time. This module answers
"what is in there that would not be added today", so the UI and the CLI can
agree on the answer and show it before anything is deleted.

Reporting and deleting are deliberately separate. Deleting index rows is cheap
to undo -- a rescan rebuilds anything removed by mistake, because your files
are never touched -- but it is still a delete.
"""

from __future__ import annotations

import sqlite3
from fnmatch import fnmatch
from pathlib import Path

from . import rules
from .config import Config


def prune_reasons(conn: sqlite3.Connection, cfg: Config, uri: str) -> set[str]:
    """Every rule that says this path should not be indexed.

    Returned as names rather than a boolean so the UI can group by cause --
    "4,812 under site-packages" is actionable in a way that "4,812 items" is
    not.
    """
    reasons: set[str] = set()
    parts = set(Path(uri).parts)

    reasons |= parts & set(cfg.excludes)
    for pattern in getattr(cfg, "exclude_patterns", ()):
        if any(fnmatch(part, pattern) for part in parts):
            reasons.add(pattern)
    # Only rules the SCANNER also honours may be reasons to delete. Anything
    # else and prune removes a file that the next scan re-adds, forever: the
    # first version of this treated "looks like code" as a reason, so a prune
    # deleted 12,261 files and the following scan put 2,266 straight back and
    # then tripped the vanish guard, leaving the index unable to update at
    # all. Being code is a reason to hide something from search, not a reason
    # to stop indexing it.
    if rules.is_ruled(conn, uri, "code"):
        reasons.add("folder marked as code")
    if rules.is_ruled(conn, uri, "hide"):
        reasons.add("folder marked as hidden")
    return reasons


def prunable(conn: sqlite3.Connection, cfg: Config,
             rows: list[sqlite3.Row] | None = None) -> list[sqlite3.Row]:
    """Indexed items that today's rules would not have indexed."""
    if rows is None:
        rows = conn.execute(
            "SELECT id, uri FROM items "
            "WHERE uri IS NOT NULL AND deleted_at IS NULL").fetchall()
    return [row for row in rows if prune_reasons(conn, cfg, row["uri"])]


def preview(conn: sqlite3.Connection, cfg: Config) -> dict:
    """What a prune would remove, grouped by cause. Reads only."""
    doomed = prunable(conn, cfg)

    by_reason: dict[str, int] = {}
    for row in doomed:
        for reason in prune_reasons(conn, cfg, row["uri"]):
            by_reason[reason] = by_reason.get(reason, 0) + 1

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NULL"
    ).fetchone()["n"]

    # Stale scan bookkeeping is counted separately. It holds no searchable
    # content, so it is not an "item", but it is what the vanish guard
    # measures against, and leaving it is what jams a scan.
    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM file_state f "
        "LEFT JOIN items i ON i.uri = f.uri AND i.deleted_at IS NULL "
        "WHERE i.id IS NULL").fetchone()["n"]
    if orphans:
        by_reason["stale scan records (no file indexed)"] = int(orphans)

    return {
        "items": len(doomed),
        "orphans": int(orphans),
        "total": int(total),
        "reasons": sorted(({"reason": reason, "items": count}
                           for reason, count in by_reason.items()),
                          key=lambda entry: -entry["items"]),
        "examples": [row["uri"] for row in doomed[:8]],
    }


def apply(conn: sqlite3.Connection, cfg: Config) -> int:
    """Delete those items. ON DELETE CASCADE carries their passages,
    records, tags and embeddings with them.

    Your files are not touched. This removes index rows only.
    """
    doomed = prunable(conn, cfg)
    # No early return when `doomed` is empty: the orphaned bookkeeping below
    # is the half that unsticks a scan, and it is exactly the case where
    # there are no items left to delete.
    ids = [int(row["id"]) for row in doomed]
    with conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            # file_state has no FK to items, so a pruned path would otherwise
            # be remembered as seen and never re-indexed if it came back.
            conn.execute(
                f"DELETE FROM file_state WHERE uri IN "
                f"(SELECT uri FROM items WHERE id IN ({placeholders}))", chunk)
            conn.execute(f"DELETE FROM items WHERE id IN ({placeholders})", chunk)

        # file_state rows can outlive their item, and some never had one: an
        # excluded file is remembered here so it is not re-examined, but no
        # item row is created for it. The vanish guard counts THESE rather
        # than items, so 82,266 orphans made every scan look like an
        # unmounted share and abort.
        #
        # Every orphan goes, not only the excluded ones: with no item row
        # there is nothing to search and nothing to show, so the bookkeeping
        # entry is dead weight either way. A file still on disk and still
        # wanted gets both rows back on the next scan.
        orphans = [row["uri"] for row in conn.execute(
            "SELECT f.uri FROM file_state f "
            "LEFT JOIN items i ON i.uri = f.uri AND i.deleted_at IS NULL "
            "WHERE i.id IS NULL")]
        for start in range(0, len(orphans), 500):
            chunk = orphans[start:start + 500]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM file_state WHERE uri IN ({placeholders})", chunk)
    return len(ids)
