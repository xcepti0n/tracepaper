"""Reconciliation scanner (FR-9, D-009).

Filesystem events do not cross SMB/NFS from a Synology NAS, so discovery is a
scheduled scan rather than a watcher. Each pass:

  1. Walk the tree collecting (uri, size, mtime)  -- metadata only, no reads.
  2. Select candidates whose metadata differs from the last recorded state.
  3. Hash only those candidates.
  4. Decide by hash: unchanged / changed / moved / new / missing.

mtime is a filter, never the decision. Synology restores, rsync copies and sync
clients rewrite mtime without touching content, and mtime can move backwards
after a restore -- so "newer than last indexed" both over- and under-reports.
Hash is the authority.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from ..config import Config
from ..db import transaction, utcnow

log = logging.getLogger(__name__)

HASH_CHUNK = 1024 * 1024

# Filesystems report mtime at limited resolution (1s on many SMB/NFS mounts).
# A file written twice inside that window can keep an identical (size, mtime)
# pair despite different content, so metadata comparison cannot prove it
# unchanged. Files whose mtime is within this margin of the previous scan are
# always hashed. Cheap: it only ever affects files touched during a scan.
MTIME_TRUST_MARGIN_SECONDS = 2.0


class ScanAborted(RuntimeError):
    """Raised when a scan looks unsafe to apply (e.g. the share is not mounted)."""


@dataclass
class ScanResult:
    scan_id: int
    seen: int = 0
    candidates: int = 0
    added: int = 0
    changed: int = 0
    moved: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    # Old paths consumed by a move this pass. They are absent from the walk but
    # are not missing -- their content was found elsewhere.
    moved_from: set[str] = field(default_factory=set)

    def summary(self) -> str:
        return (
            f"seen={self.seen} candidates={self.candidates} added={self.added} "
            f"changed={self.changed} moved={self.moved} unchanged={self.unchanged} "
            f"removed={self.removed} errors={len(self.errors)}"
        )


def hash_file(path: Path, chunk: int = HASH_CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _is_excluded(path: Path, root: Path, excludes: Iterable[str]) -> bool:
    """True if any path component under the root matches an exclude pattern."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    names = set(rel.parts)
    return any(pattern in names for pattern in excludes)


def walk(root: Path, cfg: Config) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield (path, stat) for every eligible file below root. Metadata only."""
    excludes = set(cfg.excludes)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (PermissionError, OSError) as exc:
            log.warning("cannot list %s: %s", current, exc)
            continue

        for entry in entries:
            if entry.name in excludes:
                continue
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=cfg.follow_symlinks):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=cfg.follow_symlinks):
                    st = entry.stat(follow_symlinks=cfg.follow_symlinks)
                    if st.st_size > cfg.max_file_bytes:
                        log.info("skipping oversized file %s (%d bytes)", path, st.st_size)
                        continue
                    if _is_excluded(path, root, excludes):
                        continue
                    yield path, st
            except (PermissionError, OSError) as exc:
                log.warning("cannot stat %s: %s", path, exc)


class Scanner:
    """Reconciles the index against what is currently on disk."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg

    def scan(self, root: Path, *, force_hash: bool = False) -> ScanResult:
        root = Path(root).resolve()
        if not root.exists():
            raise ScanAborted(f"scan root does not exist: {root}")

        # Anything modified this close to now cannot be trusted as unchanged on
        # metadata alone -- see MTIME_TRUST_MARGIN_SECONDS.
        scan_started = time.time()
        scan_id = self._begin_scan(root)
        result = ScanResult(scan_id=scan_id)

        prior = self._load_prior_state(root)
        prior_count = len(prior)
        seen_uris: set[str] = set()

        try:
            for path, st in walk(root, self.cfg):
                uri = str(path)
                seen_uris.add(uri)
                result.seen += 1
                try:
                    self._reconcile_one(uri, st, prior.get(uri), scan_id,
                                        result, force_hash=force_hash,
                                        scan_started=scan_started)
                except (PermissionError, OSError) as exc:
                    log.warning("cannot read %s: %s", uri, exc)
                    result.errors.append((uri, str(exc)))

            self._handle_missing(root, prior, seen_uris, prior_count, result)
        except Exception:
            self._finish_scan(scan_id, result, status="failed")
            raise

        self._finish_scan(scan_id, result, status="complete")
        return result

    # ------------------------------------------------------------ internals

    def _begin_scan(self, root: Path) -> int:
        with transaction(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO scans (started_at, root, status) VALUES (?, ?, 'running')",
                (utcnow(), str(root)),
            )
            return int(cur.lastrowid)

    def _finish_scan(self, scan_id: int, result: ScanResult, status: str) -> None:
        with transaction(self.conn) as conn:
            conn.execute(
                "UPDATE scans SET finished_at = ?, seen = ?, candidates = ?, "
                "changed = ?, added = ?, moved = ?, removed = ?, status = ? "
                "WHERE id = ?",
                (utcnow(), result.seen, result.candidates, result.changed,
                 result.added, result.moved, result.removed, status, scan_id),
            )

    def _load_prior_state(self, root: Path) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT uri, size_bytes, mtime, content_hash, miss_count "
            "FROM file_state WHERE uri LIKE ?",
            (f"{root}{os.sep}%",),
        ).fetchall()
        return {row["uri"]: row for row in rows}

    def _reconcile_one(self, uri: str, st: os.stat_result,
                       prior: sqlite3.Row | None, scan_id: int,
                       result: ScanResult, *, force_hash: bool,
                       scan_started: float) -> None:
        # Step 2: cheap metadata comparison picks candidates.
        metadata_same = (
            prior is not None
            and prior["size_bytes"] == st.st_size
            and abs(prior["mtime"] - st.st_mtime) < 0.001
        )

        # A same-size edit within the filesystem's mtime resolution leaves
        # (size, mtime) identical. Hash anything recent enough to be in that
        # window rather than trusting metadata.
        mtime_trustworthy = (scan_started - st.st_mtime) > MTIME_TRUST_MARGIN_SECONDS

        if metadata_same and mtime_trustworthy and not force_hash:
            # Not a candidate. Touch last_seen so it is not treated as missing.
            with transaction(self.conn) as conn:
                conn.execute(
                    "UPDATE file_state SET last_seen_scan = ?, miss_count = 0 "
                    "WHERE uri = ?",
                    (scan_id, uri),
                )
            result.unchanged += 1
            return

        # Step 3: hash only candidates.
        result.candidates += 1
        digest = hash_file(Path(uri))

        # Step 4: the hash decides.
        if prior is not None and prior["content_hash"] == digest:
            # Metadata churn only -- a restore or rsync rewrote mtime. No
            # re-extraction; this is the case mtime-only detection gets wrong.
            with transaction(self.conn) as conn:
                conn.execute(
                    "UPDATE file_state SET size_bytes = ?, mtime = ?, "
                    "last_seen_scan = ?, miss_count = 0 WHERE uri = ?",
                    (st.st_size, st.st_mtime, scan_id, uri),
                )
            result.unchanged += 1
            return

        if prior is not None:
            self._record_changed(uri, st, digest, scan_id)
            result.changed += 1
            return

        moved_from = self._find_move_source(digest, uri)
        if moved_from is not None:
            self._record_move(moved_from, uri, st, digest, scan_id)
            result.moved += 1
            result.moved_from.add(moved_from)
            return

        self._record_new(uri, st, digest, scan_id)
        result.added += 1

    def _find_move_source(self, digest: str, new_uri: str) -> str | None:
        """A known hash at an unseen path, whose old path is gone, is a move."""
        rows = self.conn.execute(
            "SELECT uri FROM file_state WHERE content_hash = ? AND uri != ?",
            (digest, new_uri),
        ).fetchall()
        for row in rows:
            if not Path(row["uri"]).exists():
                return str(row["uri"])
        return None

    def _record_new(self, uri: str, st: os.stat_result, digest: str, scan_id: int) -> None:
        now = utcnow()
        with transaction(self.conn) as conn:
            cur = conn.execute(
                "INSERT INTO items (kind, uri, content_hash, title, size_bytes, "
                "modified_at, extraction_status) "
                "VALUES ('document', ?, ?, ?, ?, ?, 'pending') "
                "ON CONFLICT(uri) DO UPDATE SET content_hash = excluded.content_hash, "
                "size_bytes = excluded.size_bytes, modified_at = excluded.modified_at, "
                "deleted_at = NULL",
                (uri, digest, Path(uri).name, st.st_size,
                 _iso_from_mtime(st.st_mtime)),
            )
            item_id = cur.lastrowid or self._item_id_for(conn, uri)
            conn.execute(
                "INSERT INTO file_state (uri, size_bytes, mtime, content_hash, "
                "last_seen_scan, miss_count) VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(uri) DO UPDATE SET size_bytes = excluded.size_bytes, "
                "mtime = excluded.mtime, content_hash = excluded.content_hash, "
                "last_seen_scan = excluded.last_seen_scan, miss_count = 0",
                (uri, st.st_size, st.st_mtime, digest, scan_id),
            )
            _enqueue(conn, item_id, "extract_text", now)

    def _record_changed(self, uri: str, st: os.stat_result, digest: str,
                        scan_id: int) -> None:
        now = utcnow()
        with transaction(self.conn) as conn:
            row = conn.execute("SELECT id FROM items WHERE uri = ?", (uri,)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO items (kind, uri, content_hash, title, size_bytes, "
                    "modified_at, extraction_status) "
                    "VALUES ('document', ?, ?, ?, ?, ?, 'pending')",
                    (uri, digest, Path(uri).name, st.st_size,
                     _iso_from_mtime(st.st_mtime)),
                )
                item_id = int(cur.lastrowid)
            else:
                item_id = int(row["id"])
                # Close the current version; extraction opens the next one.
                conn.execute(
                    "UPDATE item_versions SET valid_to = ? "
                    "WHERE item_id = ? AND valid_to IS NULL",
                    (now, item_id),
                )
                conn.execute(
                    "UPDATE items SET content_hash = ?, size_bytes = ?, "
                    "modified_at = ?, extraction_status = 'pending', deleted_at = NULL "
                    "WHERE id = ?",
                    (digest, st.st_size, _iso_from_mtime(st.st_mtime), item_id),
                )

            conn.execute(
                "UPDATE file_state SET size_bytes = ?, mtime = ?, content_hash = ?, "
                "last_seen_scan = ?, miss_count = 0 WHERE uri = ?",
                (st.st_size, st.st_mtime, digest, scan_id, uri),
            )
            _enqueue(conn, item_id, "extract_text", now)

    def _record_move(self, old_uri: str, new_uri: str, st: os.stat_result,
                     digest: str, scan_id: int) -> None:
        """Same content at a new path: update the path only, never re-extract."""
        with transaction(self.conn) as conn:
            conn.execute(
                "UPDATE items SET uri = ?, title = ?, deleted_at = NULL WHERE uri = ?",
                (new_uri, Path(new_uri).name, old_uri),
            )
            conn.execute("DELETE FROM file_state WHERE uri = ?", (old_uri,))
            conn.execute(
                "INSERT INTO file_state (uri, size_bytes, mtime, content_hash, "
                "last_seen_scan, miss_count) VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(uri) DO UPDATE SET size_bytes = excluded.size_bytes, "
                "mtime = excluded.mtime, content_hash = excluded.content_hash, "
                "last_seen_scan = excluded.last_seen_scan, miss_count = 0",
                (new_uri, st.st_size, st.st_mtime, digest, scan_id),
            )
        log.info("moved: %s -> %s", old_uri, new_uri)

    def _handle_missing(self, root: Path, prior: dict[str, sqlite3.Row],
                        seen: set[str], prior_count: int,
                        result: ScanResult) -> None:
        """Soft-delete paths that have gone missing for several consecutive scans.

        Guarded: if most of a previously-populated tree vanished at once, the
        share is probably not mounted. Abort rather than delete the index.
        """
        # A path consumed by a move is not missing -- its content was located
        # under a new name and its file_state row is already gone.
        missing = [uri for uri in prior
                   if uri not in seen and uri not in result.moved_from]
        if not missing:
            return

        # Survival is measured against paths that could still be found: a
        # reorganised folder shows up as moves, not as a vanished share.
        accounted = result.seen + len(result.moved_from)
        if prior_count > 0 and accounted < prior_count:
            survival = accounted / prior_count
            if survival < self.cfg.vanish_guard:
                raise ScanAborted(
                    f"{len(missing)} of {prior_count} known paths under {root} "
                    f"disappeared in one scan ({survival:.0%} remain). Refusing to "
                    "soft-delete -- the share is probably not mounted. Re-run once "
                    "it is available, or use --force-delete to override."
                )

        now = utcnow()
        with transaction(self.conn) as conn:
            for uri in missing:
                conn.execute(
                    "UPDATE file_state SET miss_count = miss_count + 1 WHERE uri = ?",
                    (uri,),
                )
                row = conn.execute(
                    "SELECT miss_count FROM file_state WHERE uri = ?", (uri,)
                ).fetchone()
                if row and row["miss_count"] >= self.cfg.miss_threshold:
                    conn.execute(
                        "UPDATE items SET deleted_at = ? WHERE uri = ? AND deleted_at IS NULL",
                        (now, uri),
                    )
                    conn.execute("DELETE FROM file_state WHERE uri = ?", (uri,))
                    result.removed += 1

    @staticmethod
    def _item_id_for(conn: sqlite3.Connection, uri: str) -> int:
        row = conn.execute("SELECT id FROM items WHERE uri = ?", (uri,)).fetchone()
        if row is None:
            raise RuntimeError(f"item vanished during insert: {uri}")
        return int(row["id"])


def _iso_from_mtime(mtime: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")


def _enqueue(conn: sqlite3.Connection, item_id: int, job_type: str, now: str) -> None:
    """Queue a job, ignoring duplicates already waiting for the same work."""
    conn.execute(
        "INSERT INTO jobs (item_id, type, state, created_at, updated_at) "
        "SELECT ?, ?, 'queued', ?, ? WHERE NOT EXISTS ("
        "  SELECT 1 FROM jobs WHERE item_id = ? AND type = ? "
        "  AND state IN ('queued', 'claimed'))",
        (item_id, job_type, now, now, item_id, job_type),
    )
