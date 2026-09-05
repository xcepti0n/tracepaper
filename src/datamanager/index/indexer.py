"""Indexing: extract text, split passages, populate FTS.

Runs the queued extract_text jobs written by the scanner. Deliberately
model-free -- this is M1, the floor that works with nothing installed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .. import events as event_derive
from .. import vocabulary
from ..config import Config
from ..db import transaction, utcnow
from ..extract import passages as passage_split
from ..extract import photos as photo_extract
from ..extract import records as record_extract
from ..extract import text as text_extract

log = logging.getLogger(__name__)


@dataclass
class IndexResult:
    processed: int = 0
    indexed: int = 0
    partial: int = 0
    failed: int = 0
    passages: int = 0
    records: int = 0
    fields: int = 0
    events: int = 0
    tags: int = 0

    def summary(self) -> str:
        return (f"processed={self.processed} indexed={self.indexed} "
                f"partial={self.partial} failed={self.failed} "
                f"passages={self.passages} records={self.records} "
                f"fields={self.fields} events={self.events} tags={self.tags}")


class Indexer:
    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg

    def _llm_config(self):
        """The LLM gap-filler config, or None when disabled."""
        if not self.cfg.llm_enabled:
            return None
        from ..extract.llm import LlmConfig
        return LlmConfig(endpoint=self.cfg.llm_endpoint, model=self.cfg.llm_model,
                         timeout=self.cfg.llm_timeout, enabled=True)

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
        # Records are extracted from the WHOLE document, before any splitting:
        # binding related facts is the entire point (FR-3).
        found = record_extract.extract_records(
            extracted.text, title, llm_config=self._llm_config(),
            min_fields=self.cfg.llm_min_fields)

        with transaction(self.conn) as conn:
            version = self._open_version(conn, item_id, extracted.text)
            self._replace_passages(conn, item_id, version, parts, title)
            counts = self._replace_records(conn, item_id, version, found,
                                           extracted.pages)
            derived = event_derive.derive_for_item(conn, item_id)

            # Photos also carry derived tags: EXIF is exact and free (FR-11).
            tagged = 0
            if photo_extract.is_photo(path):
                conn.execute("UPDATE items SET kind = 'photo' WHERE id = ?",
                             (item_id,))
                tagged = photo_extract.store_tags(
                    conn, item_id, photo_extract.extract_tags(path))

            conn.execute(
                "UPDATE items SET mime = ?, indexed_at = ?, enriched_at = ?, "
                "extraction_status = ? WHERE id = ?",
                (mime, utcnow(), utcnow() if found else None,
                 extracted.status, item_id),
            )

        result.passages += len(parts)
        result.records += counts[0]
        result.fields += counts[1]
        result.events += derived
        result.tags += tagged
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
        title = item["title"] if item else ""
        parts = passage_split.split(
            row["text"] or "",
            target_chars=self.cfg.passage_target_chars,
            overlap_chars=self.cfg.passage_overlap_chars,
        )
        found = record_extract.extract_records(
            row["text"] or "", title, llm_config=self._llm_config(),
            min_fields=self.cfg.llm_min_fields)

        with transaction(self.conn) as conn:
            self._replace_passages(conn, item_id, int(row["version"]), parts, title)
            counts = self._replace_records(conn, item_id, int(row["version"]),
                                           found, None)
            derived = event_derive.derive_for_item(conn, item_id)
            conn.execute(
                "UPDATE items SET indexed_at = ?, enriched_at = ?, "
                "extraction_status = 'complete' WHERE id = ?",
                (utcnow(), utcnow() if found else None, item_id),
            )
        result.passages += len(parts)
        result.records += counts[0]
        result.fields += counts[1]
        result.events += derived
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
    def _replace_records(conn: sqlite3.Connection, item_id: int, version: int,
                         found: list, pages: list[str] | None) -> tuple[int, int]:
        """Replace machine-extracted records, preserving human corrections.

        Human rows are never deleted (FR-10, success criterion 7): a value the
        user fixed by hand must survive a full reindex, including one run by a
        better model later.
        """
        conn.execute(
            "DELETE FROM records WHERE item_id = ? AND source != 'human'",
            (item_id,),
        )

        # Keys a human has already settled. Re-extracting them would let a
        # machine value outrank the correction at query time.
        human_keys = {
            row["key"] for row in conn.execute(
                "SELECT rf.key FROM record_fields rf JOIN records r ON r.id = rf.record_id "
                "WHERE r.item_id = ? AND r.source = 'human'", (item_id,)
            ).fetchall()
        }

        now = utcnow()
        record_count = 0
        field_count = 0

        for record in found:
            fields = [f for f in record.fields if f.key not in human_keys]
            if not fields:
                continue

            page = record.page
            if page is None and pages and record.char_start is not None:
                page = _page_for_offset(pages, record.char_start)

            cur = conn.execute(
                "INSERT INTO records (item_id, version, record_type, source, "
                "confidence, page, char_start, char_end, model_id, "
                "extractor_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, version, record.record_type, record.source,
                 record.confidence, page, record.char_start, record.char_end,
                 record.model_id, record_extract.EXTRACTOR_VERSION, now),
            )
            record_id = int(cur.lastrowid)
            record_count += 1

            for f in fields:
                # Vocabulary is discovered, not declared (FR-4). Fields are
                # stored under the canonical key so a query for one name finds
                # documents that used a synonym.
                canonical = vocabulary.register(conn, f.key)
                conn.execute(
                    "INSERT INTO record_fields (record_id, key, value_text, "
                    "value_num, value_date, unit, confidence, char_start, char_end) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (record_id, canonical, f.value_text, f.value_num, f.value_date,
                     f.unit, f.confidence, f.char_start, f.char_end),
                )
                field_count += 1

        return record_count, field_count

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


def _page_for_offset(pages: list[str], offset: int) -> int | None:
    """Map a character offset in the joined text back to its page number."""
    cursor = 0
    for number, page_text in enumerate(pages, start=1):
        end = cursor + len(page_text)
        if offset <= end:
            return number
        cursor = end + 2      # the "\n\n" joiner used when pages were merged
    return len(pages) or None
