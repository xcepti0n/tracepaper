"""Scanner tests (FR-9, D-009).

The cases that matter: mtime churn must not trigger re-extraction, moves must
not re-extract, and a vanished share must not empty the index.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from datamanager.scan.scanner import ScanAborted, Scanner


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def age(path: Path, seconds: float = 60.0) -> Path:
    """Backdate mtime past the scanner's trust margin.

    Freshly written files are always hashed, since a same-size edit within the
    filesystem's mtime resolution is indistinguishable from no edit. Tests about
    steady-state behaviour need files old enough to take the cheap path.
    """
    st = path.stat()
    os.utime(path, (st.st_atime - seconds, st.st_mtime - seconds))
    return path


def queued(conn, item_id: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM jobs WHERE type='extract_text' AND state='queued'"
    params: tuple = ()
    if item_id is not None:
        sql += " AND item_id = ?"
        params = (item_id,)
    return int(conn.execute(sql, params).fetchone()["n"])


def test_new_files_are_added_and_queued(conn, cfg, nas):
    write(nas / "a.txt", "alpha")
    write(nas / "sub" / "b.txt", "beta")

    result = Scanner(conn, cfg).scan(nas)

    assert result.added == 2
    assert result.seen == 2
    assert queued(conn) == 2
    rows = conn.execute("SELECT uri, content_hash FROM items").fetchall()
    assert len(rows) == 2
    assert all(r["content_hash"] for r in rows)


def test_unchanged_files_are_not_rehashed_or_requeued(conn, cfg, nas):
    age(write(nas / "a.txt", "alpha"))
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)
    conn.execute("UPDATE jobs SET state = 'done'")

    result = scanner.scan(nas)

    assert result.unchanged == 1
    assert result.candidates == 0, "unchanged metadata must not be hashed"
    assert result.changed == 0
    assert queued(conn) == 0


def test_mtime_churn_without_content_change_does_not_reextract(conn, cfg, nas):
    """The case mtime-only detection gets wrong (D-009).

    A Synology restore or rsync copy rewrites mtime while content is identical.
    The file becomes a hash candidate, but the hash decides: no re-extraction.
    """
    path = age(write(nas / "tax.txt", "gross salary 84200"))
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)
    conn.execute("UPDATE jobs SET state = 'done'")

    # A restore or rsync rewrites mtime; content is identical. Backdated so the
    # new mtime is still outside the trust margin.
    st = path.stat()
    os.utime(path, (st.st_atime - 500, st.st_mtime - 500))

    result = scanner.scan(nas)

    assert result.candidates == 1, "changed mtime should make it a candidate"
    assert result.unchanged == 1, "identical hash means no content change"
    assert result.changed == 0
    assert queued(conn) == 0, "must not re-extract on metadata churn alone"

    # The refreshed mtime is recorded, so the next scan is cheap again.
    assert Scanner(conn, cfg).scan(nas).candidates == 0


def test_mtime_moving_backwards_still_detects_change(conn, cfg, nas):
    """A restore can move mtime backwards; 'newer than indexed' would miss it."""
    path = write(nas / "a.txt", "original")
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)
    conn.execute("UPDATE jobs SET state = 'done'")

    path.write_text("restored to an older revision")
    st = path.stat()
    os.utime(path, (st.st_atime - 86400, st.st_mtime - 86400))

    result = scanner.scan(nas)

    assert result.changed == 1
    assert queued(conn) == 1


def test_content_change_opens_a_new_version(conn, cfg, nas):
    path = write(nas / "a.txt", "v1")
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)
    conn.execute("UPDATE jobs SET state = 'done'")

    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])
    conn.execute(
        "INSERT INTO item_versions (item_id, version, text, valid_from) "
        "VALUES (?, 1, 'v1', '2026-01-01T00:00:00+00:00')",
        (item_id,),
    )

    path.write_text("v2 with more content")
    result = scanner.scan(nas)

    assert result.changed == 1
    assert conn.execute(
        "SELECT extraction_status FROM items WHERE id = ?", (item_id,)
    ).fetchone()["extraction_status"] == "pending"
    # The prior version is closed, not destroyed (FR-9).
    assert conn.execute(
        "SELECT valid_to FROM item_versions WHERE item_id = ? AND version = 1",
        (item_id,),
    ).fetchone()["valid_to"] is not None


def test_move_updates_path_without_reextraction(conn, cfg, nas):
    path = write(nas / "a.txt", "same bytes either way")
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)
    conn.execute("UPDATE jobs SET state = 'done'")
    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])

    moved = nas / "archive" / "renamed.txt"
    moved.parent.mkdir()
    path.rename(moved)

    result = scanner.scan(nas)

    assert result.moved == 1
    assert result.added == 0
    assert queued(conn) == 0, "a move is a path update, never re-extraction"

    row = conn.execute("SELECT id, uri FROM items").fetchone()
    assert int(row["id"]) == item_id, "identity is preserved across a move"
    assert row["uri"] == str(moved)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM file_state"
    ).fetchone()["n"] == 1


def test_missing_file_soft_deleted_only_after_threshold(conn, cfg, nas):
    write(nas / "keep.txt", "keep")
    write(nas / "gone.txt", "gone")
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)

    (nas / "gone.txt").unlink()

    for expected in range(1, cfg.miss_threshold):
        result = scanner.scan(nas)
        assert result.removed == 0, f"deleted too early at miss {expected}"
        assert conn.execute(
            "SELECT deleted_at FROM items WHERE title = 'gone.txt'"
        ).fetchone()["deleted_at"] is None

    result = scanner.scan(nas)
    assert result.removed == 1
    assert conn.execute(
        "SELECT deleted_at FROM items WHERE title = 'gone.txt'"
    ).fetchone()["deleted_at"] is not None
    # The surviving file is untouched.
    assert conn.execute(
        "SELECT deleted_at FROM items WHERE title = 'keep.txt'"
    ).fetchone()["deleted_at"] is None


def test_vanished_share_aborts_instead_of_emptying_the_index(conn, cfg, nas):
    """An unmounted NAS must not be read as 'the user deleted everything'."""
    for i in range(10):
        write(nas / f"doc{i}.txt", f"content {i}")
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)

    for i in range(9):            # 90% vanish, as if the mount dropped
        (nas / f"doc{i}.txt").unlink()

    with pytest.raises(ScanAborted, match="not mounted"):
        scanner.scan(nas)

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NOT NULL"
    ).fetchone()["n"] == 0, "nothing may be soft-deleted when a scan aborts"


def test_synology_sidecars_are_excluded(conn, cfg, nas):
    write(nas / "real.txt", "real")
    write(nas / "@eaDir" / "thumb.jpg", "junk")
    write(nas / "#recycle" / "old.txt", "deleted")
    write(nas / "sub" / "@eaDir" / "nested.dat", "junk")

    result = Scanner(conn, cfg).scan(nas)

    assert result.seen == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1


def test_oversized_files_are_skipped(conn, cfg, nas):
    write(nas / "small.txt", "ok")
    write(nas / "big.txt", "x" * 5000)
    small_cfg = replace(cfg, max_file_bytes=1000)

    result = Scanner(conn, small_cfg).scan(nas)

    assert result.seen == 1
    assert conn.execute(
        "SELECT title FROM items"
    ).fetchone()["title"] == "small.txt"


def test_force_hash_rehashes_everything(conn, cfg, nas):
    age(write(nas / "a.txt", "alpha"))
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)

    result = scanner.scan(nas, force_hash=True)

    assert result.candidates == 1
    assert result.unchanged == 1


def test_scan_is_recorded(conn, cfg, nas):
    write(nas / "a.txt", "alpha")
    Scanner(conn, cfg).scan(nas)

    row = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "complete"
    assert row["finished_at"] is not None
    assert row["seen"] == 1
    assert row["added"] == 1


def test_same_size_edit_within_mtime_resolution_is_detected(conn, cfg, nas):
    """A same-length edit can leave (size, mtime) identical on coarse filesystems.

    Metadata comparison cannot prove such a file unchanged, so recent files are
    always hashed. Without this, correcting a digit in a fixed-width field --
    an amount, a date -- would be silently missed.
    """
    path = write(nas / "amount.txt", "total: 1200.00")
    scanner = Scanner(conn, cfg)
    scanner.scan(nas)
    conn.execute("UPDATE jobs SET state = 'done'")

    before = path.stat()
    path.write_text("total: 1300.00")          # identical length
    os.utime(path, (before.st_atime, before.st_mtime))   # identical mtime

    result = scanner.scan(nas)

    assert result.changed == 1, "a same-size, same-mtime edit must still be caught"
    assert queued(conn) == 1
