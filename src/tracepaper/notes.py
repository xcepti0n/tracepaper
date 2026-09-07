"""Native notes (FR-1): text typed into Tracepaper, no file on disk.

Notes share the item model with documents, so they are searchable by the same
engine. Edits append a version rather than overwriting (FR-10).
"""

from __future__ import annotations

import sqlite3

from .db import transaction, utcnow


def create_note(conn: sqlite3.Connection, title: str, text: str) -> int:
    now = utcnow()
    with transaction(conn) as c:
        cur = c.execute(
            "INSERT INTO items (kind, uri, title, mime, created_at, modified_at, "
            "extraction_status) VALUES ('note', NULL, ?, 'text/plain', ?, ?, 'pending')",
            (title, now, now),
        )
        item_id = int(cur.lastrowid)
        c.execute(
            "INSERT INTO item_versions (item_id, version, text, valid_from) "
            "VALUES (?, 1, ?, ?)",
            (item_id, text, now),
        )
        c.execute(
            "INSERT INTO jobs (item_id, type, state, created_at, updated_at) "
            "SELECT ?, 'extract_text', 'queued', ?, ? WHERE NOT EXISTS ("
            "  SELECT 1 FROM jobs WHERE item_id = ? AND type = 'extract_text' "
            "  AND state IN ('queued', 'claimed'))",
            (item_id, now, now, item_id),
        )
    return item_id


def update_note(conn: sqlite3.Connection, item_id: int, text: str,
                title: str | None = None) -> int:
    """Append a new version. Returns the new version number."""
    now = utcnow()
    row = conn.execute(
        "SELECT kind FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no such item: {item_id}")
    if row["kind"] != "note":
        raise ValueError(f"item {item_id} is not a note; edit the file on the NAS instead")

    with transaction(conn) as c:
        prev = c.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM item_versions WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        version = int(prev["v"]) + 1

        c.execute(
            "UPDATE item_versions SET valid_to = ? WHERE item_id = ? AND valid_to IS NULL",
            (now, item_id),
        )
        c.execute(
            "INSERT INTO item_versions (item_id, version, text, valid_from) "
            "VALUES (?, ?, ?, ?)",
            (item_id, version, text, now),
        )
        if title is not None:
            c.execute("UPDATE items SET title = ? WHERE id = ?", (title, item_id))
        c.execute(
            "UPDATE items SET modified_at = ?, extraction_status = 'pending' WHERE id = ?",
            (now, item_id),
        )
        c.execute(
            "INSERT INTO jobs (item_id, type, state, created_at, updated_at) "
            "SELECT ?, 'extract_text', 'queued', ?, ? WHERE NOT EXISTS ("
            "  SELECT 1 FROM jobs WHERE item_id = ? AND type = 'extract_text' "
            "  AND state IN ('queued', 'claimed'))",
            (item_id, now, now, item_id),
        )
    return version
