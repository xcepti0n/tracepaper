"""Indexing: extract text, split passages, populate FTS.

Runs the queued extract_text jobs written by the scanner. Deliberately
model-free -- this is M1, the floor that works with nothing installed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..db import transaction, utcnow
from ..extract import passages as passage_split
from ..extract import text as text_extract

log = logging.getLogger(__name__)


@dataclass
class IndexResult:
    processed: int = 0
    indexed: int = 0
    partial: int = 0
    failed: int = 0
    passages: int = 0

    def summary(self) -> str:
        return (f"processed={self.processed} indexed={self.indexed} "
                f"partial={self.partial} failed={self.failed} "
                f"passages={self.passages}")


class Indexer:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg

    def run_pending(self, limit: int | None = None) -> IndexResult:
        """Process queued extract_text jobs."""
        result = IndexResult()
        while True:
            job = self._claim_job(limit_check=limit, done=result.processed)
            if job is None:
                break
            self._process(job, result)
        return result

    def reindex_item(self, item_id: int) -> IndexResult:
        result = IndexResult()
        row = self.conn.execute(
            "SELECT id, uri FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no such item: {item_id}")
        self._index_one(int(row["id"]), row["uri"], result)
        return result

    # ------------------------------------------------------------ internals

    def _claim_job(self, limit_check: int | None, done: int) -> sqlite3.Row | None:
        if limit_check is not None and done >= limit_check:
            return None
        with transaction(self.conn) as conn:
            row = conn.execute(
                "SELECT j.id, j.item_id, i.uri FROM jobs j "
                "JOIN items i ON i.id = j.item_id "
                "WHERE j.type = 'extract_text' AND j.state = 'queued' "
                "AND i.deleted_at IS NULL "
                "ORDER BY j.priority, j.id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE jobs SET state = 'claimed', claimed_at = ?, "
                "attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (utcnow(), utcnow(), row["id"]),
            )
            return row

    def _process(self, job: sqlite3.Row, result: IndexResult) -> None:
        result.processed += 1
        job_id = int(job["id"])
        try:
            status = self._index_one(int(job["item_id"]), job["uri"], result)
        except Exception as exc:
            log.exception("indexing failed for item %s", job["item_id"])
            with transaction(self.conn) as conn:
                conn.execute(
                    "UPDATE jobs SET state = 'failed', last_error = ?, updated_at = ? "
                    "WHERE id = ?",
                    (f"{type(exc).__name__}: {exc}", utcnow(), job_id),
                )
                conn.execute(
                    "UPDATE items SET extraction_status = 'failed' WHERE id = ?",
                    (job["item_id"],),
                )
            result.failed += 1
            return

        with transaction(self.conn) as conn:
            conn.execute(
                "UPDATE jobs SET state = 'done', updated_at = ? WHERE id = ?",
                (utcnow(), job_id),
            )
        if status == "complete":
            result.indexed += 1
        else:
            result.partial += 1

    def _index_one(self, item_id: int, uri: str | None, result: IndexResult) -> str:
        if uri is None:
            return self._index_note(item_id, result)

        path = Path(uri)
        if not path.exists():
            with transaction(self.conn) as conn:
                conn.execute(
                    "UPDATE items SET extraction_status = 'failed' WHERE id = ?",
                    (item_id,),
                )
            return "failed"

        extracted = text_extract.extract(path)
        mime = text_extract.guess_mime(path)

        parts = passage_split.split(
            extracted.text,
            pages=extracted.pages,
            target_chars=self.cfg.passage_target_chars,
            overlap_chars=self.cfg.passage_overlap_chars,
        )

        title = path.name
        with transaction(self.conn) as conn:
            version = self._open_version(conn, item_id, extracted.text)
            self._replace_passages(conn, item_id, version, parts, title)
            conn.execute(
                "UPDATE items SET mime = ?, indexed_at = ?, extraction_status = ? "
                "WHERE id = ?",
                (mime, utcnow(), extracted.status, item_id),
            )

        result.passages += len(parts)
        if extracted.note:
            log.info("item %s (%s): %s", item_id, path.name, extracted.note)
        return extracted.status

    def _index_note(self, item_id: int, result: IndexResult) -> str:
        """Native notes already hold their text in the current version."""
        row = self.conn.execute(
            "SELECT version, text FROM item_versions "
            "WHERE item_id = ? AND valid_to IS NULL "
            "ORDER BY version DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        if row is None:
            return "failed"

        item = self.conn.execute(
            "SELECT title FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        parts = passage_split.split(
            row["text"] or "",
            target_chars=self.cfg.passage_target_chars,
            overlap_chars=self.cfg.passage_overlap_chars,
        )
        with transaction(self.conn) as conn:
            self._replace_passages(conn, item_id, int(row["version"]), parts,
                                   item["title"] if item else "")
            conn.execute(
                "UPDATE items SET indexed_at = ?, extraction_status = 'complete' "
                "WHERE id = ?",
                (utcnow(), item_id),
            )
        result.passages += len(parts)
        return "complete"

    @staticmethod
    def _open_version(conn: sqlite3.Connection, item_id: int, text: str) -> int:
        """Open a new version if the text changed; otherwise reuse the current one."""
        current = conn.execute(
            "SELECT version, text FROM item_versions "
            "WHERE item_id = ? AND valid_to IS NULL "
            "ORDER BY version DESC LIMIT 1",
            (item_id,),
        ).fetchone()

        if current is not None and (current["text"] or "") == text:
            return int(current["version"])

        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM item_versions WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        next_version = int(row["v"]) + 1

        conn.execute(
            "UPDATE item_versions SET valid_to = ? WHERE item_id = ? AND valid_to IS NULL",
            (utcnow(), item_id),
        )
        item = conn.execute(
            "SELECT content_hash FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO item_versions (item_id, version, content_hash, text, valid_from) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_id, next_version, item["content_hash"] if item else None,
             text, utcnow()),
        )
        return next_version

    @staticmethod
    def _replace_passages(conn: sqlite3.Connection, item_id: int, version: int,
                          parts: list[passage_split.Passage], title: str) -> None:
        """Replace this item's passages and their FTS rows.

        FTS5 external-content tables need their delete rows issued before the
        source rows disappear, so the old ids are read first.
        """
        old = conn.execute(
            "SELECT id FROM passages WHERE item_id = ?", (item_id,)
        ).fetchall()
        for row in old:
            src = conn.execute(
                "SELECT id, text, title FROM passages_fts_src WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if src:
                conn.execute(
                    "INSERT INTO passages_fts (passages_fts, rowid, text, title) "
                    "VALUES ('delete', ?, ?, ?)",
                    (src["id"], src["text"], src["title"]),
                )
                conn.execute("DELETE FROM passages_fts_src WHERE id = ?", (row["id"],))
        conn.execute("DELETE FROM passages WHERE item_id = ?", (item_id,))

        for part in parts:
            cur = conn.execute(
                "INSERT INTO passages (item_id, version, ordinal, text, page, "
                "char_start, char_end) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item_id, version, part.ordinal, part.text, part.page,
                 part.char_start, part.char_end),
            )
            passage_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO passages_fts_src (id, text, title) VALUES (?, ?, ?)",
                (passage_id, part.text, title),
            )
            conn.execute(
                "INSERT INTO passages_fts (rowid, text, title) VALUES (?, ?, ?)",
                (passage_id, part.text, title),
            )

        # An item with no extractable text (a scanned PDF, an image) still gets
        # one title-only FTS row, so it remains findable by filename (FR-2).
        if not parts and title:
            cur = conn.execute(
                "INSERT INTO passages (item_id, version, ordinal, text, page, "
                "char_start, char_end) VALUES (?, ?, 0, '', NULL, 0, 0)",
                (item_id, version),
            )
            passage_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO passages_fts_src (id, text, title) VALUES (?, '', ?)",
                (passage_id, title),
            )
            conn.execute(
                "INSERT INTO passages_fts (rowid, text, title) VALUES (?, '', ?)",
                (passage_id, title),
            )
