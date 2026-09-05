"""Human corrections (FR-10).

A value the user fixes by hand outranks every extractor and survives re-ingest
permanently -- including a re-ingest by a better model years later. This is the
only part of the index that cannot be regenerated from the NAS, which is why it
is backed up separately (NFR-6).
"""

from __future__ import annotations

import sqlite3

from .db import transaction, utcnow
from .extract.records import normalize_key, parse_amount, parse_date

HUMAN_RECORD_TYPE = "correction"


def correct_field(conn: sqlite3.Connection, item_id: int, key: str,
                  value: str, *, unit: str | None = None) -> int:
    """Set a field by hand. Returns the record id holding the correction.

    All human corrections for an item live in one record, so they are easy to
    find, export, and back up.
    """
    row = conn.execute(
        "SELECT id FROM items WHERE id = ? AND deleted_at IS NULL", (item_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no such item: {item_id}")

    canonical = normalize_key(key)
    now = utcnow()

    with transaction(conn) as c:
        record = c.execute(
            "SELECT id FROM records WHERE item_id = ? AND source = 'human' LIMIT 1",
            (item_id,),
        ).fetchone()

        if record is None:
            version = c.execute(
                "SELECT COALESCE(MAX(version), 1) AS v FROM item_versions "
                "WHERE item_id = ?", (item_id,)
            ).fetchone()["v"]
            cur = c.execute(
                "INSERT INTO records (item_id, version, record_type, source, "
                "confidence, created_at) VALUES (?, ?, ?, 'human', 1.0, ?)",
                (item_id, int(version), HUMAN_RECORD_TYPE, now),
            )
            record_id = int(cur.lastrowid)
        else:
            record_id = int(record["id"])

        # One value per key: a correction replaces, never accumulates.
        c.execute("DELETE FROM record_fields WHERE record_id = ? AND key = ?",
                  (record_id, canonical))

        value_num, parsed_unit = parse_amount(value)
        # Only treat it as numeric when the value is essentially just a number.
        import re
        if value_num is not None and len(re.sub(r"[\d,.\s$€£₹]", "", value)) > 3:
            value_num, parsed_unit = None, None

        c.execute(
            "INSERT INTO record_fields (record_id, key, value_text, value_num, "
            "value_date, unit, confidence) VALUES (?, ?, ?, ?, ?, ?, 1.0)",
            (record_id, canonical, value, value_num, parse_date(value),
             unit or parsed_unit),
        )
        c.execute(
            "INSERT INTO key_vocabulary (key, canonical_key, occurrences) "
            "VALUES (?, ?, 1) ON CONFLICT(key) DO UPDATE SET "
            "occurrences = occurrences + 1",
            (canonical, canonical),
        )

        # Machine-extracted copies of this key would compete at query time.
        c.execute(
            "DELETE FROM record_fields WHERE key = ? AND record_id IN "
            "(SELECT id FROM records WHERE item_id = ? AND source != 'human')",
            (canonical, item_id),
        )

    return record_id


def remove_correction(conn: sqlite3.Connection, item_id: int, key: str) -> bool:
    """Drop a correction. The next reindex restores the extracted value."""
    canonical = normalize_key(key)
    with transaction(conn) as c:
        cur = c.execute(
            "DELETE FROM record_fields WHERE key = ? AND record_id IN "
            "(SELECT id FROM records WHERE item_id = ? AND source = 'human')",
            (canonical, item_id),
        )
        removed = cur.rowcount > 0
        # Drop the container record once its last field is gone.
        c.execute(
            "DELETE FROM records WHERE item_id = ? AND source = 'human' "
            "AND id NOT IN (SELECT DISTINCT record_id FROM record_fields)",
            (item_id,),
        )
    return removed


def list_corrections(conn: sqlite3.Connection,
                     item_id: int | None = None) -> list[sqlite3.Row]:
    """Every human-authored value -- the irreplaceable layer (NFR-6)."""
    sql = ("SELECT i.id AS item_id, i.title, i.uri, rf.key, rf.value_text, "
           "rf.value_num, rf.value_date, rf.unit "
           "FROM record_fields rf "
           "JOIN records r ON r.id = rf.record_id "
           "JOIN items i ON i.id = r.item_id "
           "WHERE r.source = 'human'")
    params: list[object] = []
    if item_id is not None:
        sql += " AND i.id = ?"
        params.append(item_id)
    sql += " ORDER BY i.id, rf.key"
    return conn.execute(sql, params).fetchall()
