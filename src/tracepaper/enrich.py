"""Background enrichment (NFR-4).

Slow, optional passes that make things *more* findable but that nothing depends
on: object tagging for photos, VLM captions, embeddings. They run when the
machine is idle and stop the moment it is busy, so enrichment never competes
with anything the user is actually doing.

Everything here is resumable and idempotent. Killing it mid-run costs only the
item in flight; the next run picks up where it left off. That is what makes it
safe to schedule at 3am and forget.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import transaction, utcnow

log = logging.getLogger(__name__)

# Above this 1-minute load average per core, the machine is doing something
# else and enrichment yields.
BUSY_LOAD_PER_CORE = 0.7

# Re-checked between items, so a run reacts to the machine getting busy rather
# than only at startup.
IDLE_CHECK_INTERVAL = 5


@dataclass
class EnrichResult:
    photos_tagged: int = 0
    captions: int = 0
    embedded: int = 0
    skipped_busy: int = 0
    elapsed: float = 0.0

    def summary(self) -> str:
        parts = [f"photos_tagged={self.photos_tagged}",
                 f"captions={self.captions}",
                 f"embedded={self.embedded}"]
        if self.skipped_busy:
            parts.append(f"yielded={self.skipped_busy}")
        parts.append(f"in {self.elapsed:.0f}s")
        return " ".join(parts)


def system_busy(threshold: float = BUSY_LOAD_PER_CORE) -> bool:
    """Whether the machine is under load worth yielding to."""
    try:
        one_minute = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        return (one_minute / cores) > threshold
    except (OSError, AttributeError):
        return False


def wait_until_idle(*, timeout: float = 300,
                    threshold: float = BUSY_LOAD_PER_CORE) -> bool:
    """Block until the machine is idle. False if it never settles in time."""
    deadline = time.monotonic() + timeout
    while system_busy(threshold):
        if time.monotonic() > deadline:
            return False
        time.sleep(IDLE_CHECK_INTERVAL)
    return True


class Enricher:
    """Runs the optional passes, yielding whenever the machine gets busy."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config, *,
                 respect_load: bool = True,
                 load_threshold: float = BUSY_LOAD_PER_CORE):
        self.conn = conn
        self.cfg = cfg
        self.respect_load = respect_load
        self.load_threshold = load_threshold

    def _should_yield(self) -> bool:
        return self.respect_load and system_busy(self.load_threshold)

    def run(self, *, limit: int | None = None,
            captions: bool = False) -> EnrichResult:
        started = time.monotonic()
        result = EnrichResult()

        self._tag_photos(result, limit)
        if captions:
            self._caption_photos(result, limit)
        self._embed(result)

        result.elapsed = time.monotonic() - started
        return result

    # ------------------------------------------------------------- photos

    def _tag_photos(self, result: EnrichResult, limit: int | None) -> None:
        from .extract import vision

        if not vision.available():
            return

        rows = self.conn.execute(
            "SELECT i.id, i.uri FROM items i "
            "WHERE i.kind = 'photo' AND i.deleted_at IS NULL AND i.uri IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.item_id = i.id "
            "                AND t.namespace IN ('object', 'animal', 'people')) "
            "ORDER BY i.id" + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()

        for row in rows:
            if self._should_yield():
                result.skipped_busy += 1
                return

            path = Path(row["uri"])
            if not path.exists():
                continue

            tags = vision.classify(path)
            if tags.is_empty:
                # Record the attempt so an unclassifiable photo is not retried
                # on every run.
                self._mark_attempted(int(row["id"]))
                continue

            with transaction(self.conn) as conn:
                for namespace, value, source, confidence in vision.to_tags(tags):
                    conn.execute(
                        "INSERT INTO tags (item_id, namespace, value, source, "
                        "confidence) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT DO NOTHING",
                        (row["id"], namespace, value, source, confidence),
                    )
                conn.execute("UPDATE items SET enriched_at = ? WHERE id = ?",
                             (utcnow(), row["id"]))
            result.photos_tagged += 1

    def _mark_attempted(self, item_id: int) -> None:
        with transaction(self.conn) as conn:
            conn.execute(
                "INSERT INTO tags (item_id, namespace, value, source, confidence) "
                "VALUES (?, 'object', '_none', 'vision', 0.0) "
                "ON CONFLICT DO NOTHING", (item_id,))

    def _caption_photos(self, result: EnrichResult, limit: int | None) -> None:
        """Caption only photos whose labels came out thin.

        A VLM pass is orders of magnitude slower than Vision, so it is spent
        where Vision had least to say.
        """
        from .extract import vision

        if not self.cfg.llm_enabled:
            return

        rows = self.conn.execute(
            "SELECT i.id, i.uri FROM items i "
            "WHERE i.kind = 'photo' AND i.deleted_at IS NULL AND i.uri IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.item_id = i.id "
            "                AND t.namespace = 'caption') "
            "AND (SELECT COUNT(*) FROM tags t2 WHERE t2.item_id = i.id "
            "     AND t2.namespace = 'object' AND t2.value != '_none') < 3 "
            "ORDER BY i.id" + (f" LIMIT {int(limit)}" if limit else " LIMIT 200")
        ).fetchall()

        for row in rows:
            if self._should_yield():
                result.skipped_busy += 1
                return

            path = Path(row["uri"])
            if not path.exists():
                continue

            text = vision.caption(path, endpoint=self.cfg.llm_endpoint,
                                  model=self.cfg.vlm_model)
            if not text:
                continue

            with transaction(self.conn) as conn:
                conn.execute(
                    "INSERT INTO tags (item_id, namespace, value, source, "
                    "confidence) VALUES (?, 'caption', ?, 'vlm', 0.5) "
                    "ON CONFLICT DO NOTHING", (row["id"], text))
            result.captions += 1

    # --------------------------------------------------------- embeddings

    def _embed(self, result: EnrichResult) -> None:
        from . import embed

        if not embed.available() or self._should_yield():
            return
        result.embedded = embed.embed_pending(
            self.conn, model_id=self.cfg.embed_model).embedded
