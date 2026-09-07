"""SQLite connection handling and schema management.

The index lives on the always-on host, never on the NAS mount (D-008): SQLite
over SMB/NFS is unreliable under concurrent access.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "1"


def utcnow() -> str:
    """ISO-8601 UTC timestamp, used for every stored time."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open the index, creating and migrating the schema as needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Scans and ingest can contend; wait rather than raising immediately.
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")

    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("tracepaper").joinpath("schema.sql").read_text()
    conn.executescript(sql)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. isolation_level=None means we drive BEGIN ourselves."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
