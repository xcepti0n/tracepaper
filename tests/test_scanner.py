"""Scanner tests (FR-9, D-009).

The cases that matter: mtime churn must not trigger re-extraction, moves must
not re-extract, and a vanished share must not empty the index.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from tracepaper.scan.scanner import ScanAborted, Scanner


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


def test_rescan_does_not_rehash_settled_files(conn, cfg, nas):
    """What makes the hourly timer cheap: a file whose (size, mtime) is
    unchanged, and old enough to trust, is never opened again."""
    for i in range(3):
        age(write(nas / f"d{i}.txt", f"document {i}"))

    first = Scanner(conn, cfg).scan(nas)
    assert first.added == 3
    assert first.candidates == 3, "a first sighting must be hashed"

    second = Scanner(conn, cfg).scan(nas)
    assert second.unchanged == 3
    assert second.added == 0
    assert second.candidates == 0, (
        "a settled, unchanged file must not be read again -- this is what "
        "keeps an hourly scan of a large corpus nearly free")


def test_touching_mtime_does_not_reextract(conn, cfg, nas):
    """A Synology restore or an rsync rewrites mtime without changing content.
    The hash decides, so this costs one hash and no re-extraction."""
    path = age(write(nas / "a.txt", "unchanged content"))
    Scanner(conn, cfg).scan(nas)
    before = queued(conn)

    os.utime(path, None)  # touch: new mtime, same bytes

    result = Scanner(conn, cfg).scan(nas)
    assert result.candidates == 1, "the changed mtime makes it a candidate"
    assert result.changed == 0, "but the hash proves the content is identical"
    assert result.unchanged == 1
    assert queued(conn) == before, "no new extraction work was queued"


def test_uri_stays_unique_across_repeated_scans(conn, cfg, nas):
    """Re-running a scan must never duplicate an item."""
    age(write(nas / "a.txt", "one"))
    for _ in range(4):
        Scanner(conn, cfg).scan(nas)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NULL"
    ).fetchone()["n"]
    assert n == 1


def test_prune_reports_before_it_deletes(conn, cfg, capsys):
    """Adding an exclude stops the next scan walking a directory, but rows
    already indexed stay and keep matching searches. prune removes them --
    and defaults to reporting, because it is still a delete."""
    from tracepaper.cli import _cmd_prune

    conn.execute("INSERT INTO items (kind, uri, extraction_status) VALUES "
                 "('document', '/nas/Notes/.obsidian/plugins/x/main.js', 'complete')")
    conn.execute("INSERT INTO items (kind, uri, extraction_status) VALUES "
                 "('document', '/nas/Notes/real-note.md', 'complete')")
    conn.commit()

    class Args:
        apply = False
        formats = False

    assert _cmd_prune(Args(), cfg, conn) == 0
    assert "nothing deleted" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2, (
        "a report must not delete anything")


def test_prune_removes_excluded_items_and_their_file_state(conn, cfg):
    """Passages and embeddings go with the item via ON DELETE CASCADE, but
    file_state is keyed by uri with no foreign key -- left behind, the next
    scan treats the file as already known and skips it, making the exclude
    look like it did nothing."""
    from tracepaper.cli import _cmd_prune

    bad = "/nas/Notes/.obsidian/plugins/mind-map/main.js"
    good = "/nas/Notes/real-note.md"
    for uri in (bad, good):
        conn.execute("INSERT INTO items (kind, uri, extraction_status) "
                     "VALUES ('document', ?, 'complete')", (uri,))
        conn.execute("INSERT INTO file_state (uri, size_bytes, mtime, "
                     "last_seen_scan) VALUES (?, 1, 1.0, 1)", (uri,))
    item_id = conn.execute("SELECT id FROM items WHERE uri = ?", (bad,)).fetchone()[0]
    conn.execute("INSERT INTO passages (item_id, version, ordinal, text) "
                 "VALUES (?, 1, 0, 'function x()')", (item_id,))
    conn.commit()

    class Args:
        apply = True
        formats = False

    assert _cmd_prune(Args(), cfg, conn) == 0

    remaining = [r[0] for r in conn.execute("SELECT uri FROM items")]
    assert remaining == [good]
    assert conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0] == 0, (
        "passages must cascade away with their item")
    states = [r[0] for r in conn.execute("SELECT uri FROM file_state")]
    assert states == [good], "file_state must not keep the excluded path"


def test_configured_excludes_merge_with_the_defaults(tmp_path):
    """A config written months ago cannot know about a name added since.
    Replacing the list silently dropped every default -- which is exactly how
    .obsidian plugin JavaScript got indexed on a host whose tracepaper.toml
    predated that exclude."""
    from tracepaper.config import DEFAULT_EXCLUDES, Config

    config_file = tmp_path / "tracepaper.toml"
    config_file.write_text(
        '[scan]\nexcludes = ["@eaDir", "MyOwnFolder"]\n')

    cfg = Config.load(config_file)
    assert "MyOwnFolder" in cfg.excludes, "a configured name must be kept"
    assert ".obsidian" in cfg.excludes, (
        "defaults must survive a config that predates them")
    for name in DEFAULT_EXCLUDES:
        assert name in cfg.excludes
    assert len(set(cfg.excludes)) == len(cfg.excludes), "no duplicates"


def test_a_default_exclude_can_be_turned_off_explicitly(tmp_path):
    """Merging must not remove the escape hatch -- just make it deliberate."""
    from tracepaper.config import Config

    config_file = tmp_path / "tracepaper.toml"
    config_file.write_text('[scan]\nexcludes = ["!node_modules"]\n')

    cfg = Config.load(config_file)
    assert "node_modules" not in cfg.excludes
    assert ".obsidian" in cfg.excludes, "only the named default is dropped"


def test_prune_reports_progress_while_deleting(conn, cfg, capsys):
    """Each item cascades into passages, embeddings, records and tags, so
    deleting hundreds of thousands takes tens of minutes. Silence there is
    indistinguishable from a hang -- and it runs in one transaction that a
    panicked Ctrl+C would roll back."""
    from tracepaper.cli import _cmd_prune

    for i in range(3):
        conn.execute("INSERT INTO items (kind, uri, extraction_status) VALUES "
                     "('document', ?, 'complete')",
                     (f"/nas/x/.venv/lib/f{i}.py",))
    conn.commit()

    class Args:
        apply = True
        formats = False

    _cmd_prune(Args(), cfg, conn)
    out = capsys.readouterr().out
    assert "deleting 3 item(s)" in out, "it must say what it is about to do"
    assert "3/3" in out, "it must report progress, not only a final line"


def test_every_cascading_foreign_key_to_items_is_indexed(conn):
    """SQLite does not index a foreign key for you, and ON DELETE CASCADE must
    find the children of every deleted row. Unindexed, each delete full-scans
    the child table: 159x slower on a small database, and it turned `prune`
    deleting 230k items into hours of scanning rather than seconds.

    This asserts the property rather than a list, so a table added later with a
    cascading item_id cannot quietly reintroduce it.
    """
    conn.row_factory = sqlite3.Row
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'")]

    missing = []
    for table in tables:
        cascading = {
            r["from"] for r in conn.execute(f"PRAGMA foreign_key_list({table})")
            if r["table"] == "items" and r["on_delete"] == "CASCADE"
        }
        if not cascading:
            continue

        # Only the FIRST column of an index can satisfy a cascade lookup, and
        # a PARTIAL index cannot: the cascade must find children in any state,
        # while a partial index only covers rows matching its WHERE clause.
        # jobs had exactly this trap -- idx_jobs_pending leads with item_id but
        # is limited to queued/claimed, so every cascade still full-scanned it.
        leading = set()
        for index in conn.execute(f"PRAGMA index_list({table})"):
            if index["partial"]:
                continue
            columns = [r["name"] for r in
                       conn.execute(f"PRAGMA index_info({index['name']})")]
            if columns:
                leading.add(columns[0])

        missing.extend(f"{table}.{column}"
                       for column in sorted(cascading) if column not in leading)

    assert not missing, (
        f"cascading foreign keys to items with no leading index: {missing}")


def test_prune_formats_keeps_the_file_but_drops_its_contents(conn, cfg, capsys):
    """Deleting the item would lose the filename, which is the part worth
    keeping -- "which gcode did I slice for the stand?" is a real search. So
    only the passages go, and the item becomes partial, exactly as a binary is."""
    from tracepaper.cli import _cmd_prune

    conn.execute("INSERT INTO items (kind, uri, extraction_status) VALUES "
                 "('document', '/nas/3d/stand.gcode', 'complete')")
    gcode_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO items (kind, uri, extraction_status) VALUES "
                 "('document', '/nas/docs/passport.pdf', 'complete')")
    pdf_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for item_id, text in ((gcode_id, "G1 X92.7 Y104.5"), (pdf_id, "passport")):
        conn.execute("INSERT INTO passages (item_id, version, ordinal, text) "
                     "VALUES (?, 1, 0, ?)", (item_id, text))
    conn.commit()

    class Args:
        apply = True
        formats = True

    _cmd_prune(Args(), cfg, conn)

    assert conn.execute("SELECT COUNT(*) FROM passages WHERE item_id = ?",
                        (gcode_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM passages WHERE item_id = ?",
                        (pdf_id,)).fetchone()[0] == 1, "documents untouched"
    assert conn.execute("SELECT COUNT(*) FROM items WHERE id = ?",
                        (gcode_id,)).fetchone()[0] == 1, (
        "the file must stay findable by name")
    assert conn.execute("SELECT extraction_status FROM items WHERE id = ?",
                        (gcode_id,)).fetchone()[0] == "partial"


def test_prune_formats_reports_before_changing_anything(conn, cfg, capsys):
    from tracepaper.cli import _cmd_prune

    conn.execute("INSERT INTO items (kind, uri, extraction_status) VALUES "
                 "('document', '/nas/3d/a.gcode', 'complete')")
    item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO passages (item_id, version, ordinal, text) "
                 "VALUES (?, 1, 0, 'G1 X1')", (item_id,))
    conn.commit()

    class Args:
        apply = False
        formats = True

    _cmd_prune(Args(), cfg, conn)
    assert "re-run with --apply --formats" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0] == 1


def test_generated_package_directories_are_not_scanned(conn, cfg, nas):
    """PyInstaller output filled a page of results with LICENSE and METADATA.

    These directories carry a version in the name, so a fixed exclude list
    cannot hold them -- they are matched as patterns.
    """
    build = nas / "Projects" / "DexterAI" / "dist" / "DexterAI" / "_internal"
    (build / "typing_extensions-4.14.0.dist-info" / "licenses").mkdir(parents=True)
    (build / "typing_extensions-4.14.0.dist-info" / "licenses" / "LICENSE"
     ).write_text("MIT License. Permission is hereby granted.")
    (build / "typing_extensions-4.14.0.dist-info" / "METADATA").write_text(
        "Name: typing_extensions\nVersion: 4.14.0\n")

    keep = nas / "Personal"
    keep.mkdir(parents=True)
    (keep / "return_2023.pdf.txt").write_text("Tax return for 2023.")

    Scanner(conn, cfg).scan(nas)

    found = {r["uri"] for r in conn.execute("SELECT uri FROM items")}
    assert any("return_2023" in uri for uri in found), "real documents stay"
    assert not [uri for uri in found if "dist-info" in uri], \
        f"packaging output was indexed: {found}"
    assert not [uri for uri in found if "_internal" in uri]


def test_a_folder_merely_named_like_a_build_is_kept(conn, cfg, nas):
    """"dist" and "build" are ordinary words; only proven output is skipped."""
    (nas / "Distribution").mkdir(parents=True)
    (nas / "Distribution" / "supplier list.txt").write_text("Acme Ltd, Bolt Co.")
    (nas / "Notes").mkdir(parents=True)
    (nas / "Notes" / "build a shed.txt").write_text("Shed plans and timber list.")

    Scanner(conn, cfg).scan(nas)

    found = {r["uri"] for r in conn.execute("SELECT uri FROM items")}
    assert len(found) == 2, f"ordinary folders must be scanned: {found}"


def test_a_failed_scan_records_why(conn, cfg, nas):
    """"failed" with no reason meant opening the journal to find out.

    Uses the vanish guard, which is the failure that actually happens: an
    unmounted share, or (as it turned out) a prune that removed more than the
    guard's threshold.
    """
    from tracepaper.scan.scanner import ScanAborted, Scanner

    for n in range(10):
        (nas / f"doc{n}.txt").write_text(f"Document {n}.")
    Scanner(conn, cfg).scan(nas)

    # Everything disappears, as it would if the NAS were not mounted.
    for n in range(10):
        (nas / f"doc{n}.txt").unlink()

    with pytest.raises(ScanAborted):
        Scanner(conn, cfg).scan(nas)

    row = conn.execute(
        "SELECT status, message FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed"
    assert row["message"], "the reason must be stored, not just the failure"
    assert "disappeared" in row["message"]


def test_marking_a_folder_as_code_does_not_look_like_a_vanished_share(
        conn, cfg, nas):
    """The guard could not tell "deliberately skipped" from "gone".

    Marking one folder as code hid 7,212 files from the walk, the guard read
    that as 76% of the share disappearing, and every scan aborted. The index
    stopped updating because of a setting chosen on purpose.
    """
    from dataclasses import replace

    from tracepaper import rules
    from tracepaper.scan.scanner import Scanner

    code_dir = nas / "Projects"
    code_dir.mkdir(parents=True, exist_ok=True)
    for index in range(12):
        (code_dir / f"module{index}.py").write_text(f"x = {index}\n")
    (nas / "letter.txt").write_text("a real document\n")

    configured = replace(cfg, roots=[nas])
    first = Scanner(conn, configured).scan(nas)
    assert first.seen >= 13

    # Now hide the folder, exactly as the Settings page does.
    rules.add_rule(conn, str(code_dir), "code")

    # The scan must succeed, not abort, and must not soft-delete the files it
    # was told to skip: the rule is reversible.
    Scanner(conn, replace(configured)).scan(nas)

    still_there = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NULL "
        "AND uri LIKE ?", (f"{code_dir}%",)).fetchone()["n"]
    assert still_there > 0, (
        "a folder marked as code is hidden from search, not deleted")


def test_a_genuinely_unmounted_share_still_aborts(conn, cfg, nas):
    """The guard must keep doing its job: the fix above must not disarm it."""
    import shutil
    from dataclasses import replace

    from tracepaper.scan.scanner import Scanner

    for index in range(20):
        (nas / f"doc{index}.txt").write_text(f"content {index}\n")

    configured = replace(cfg, roots=[nas])
    Scanner(conn, configured).scan(nas)

    # Everything gone, with no rule to explain it.
    for item in nas.iterdir():
        if item.is_file():
            item.unlink()
        else:
            shutil.rmtree(item)

    from tracepaper.scan.scanner import ScanAborted

    with pytest.raises(ScanAborted, match="not mounted"):
        Scanner(conn, configured).scan(nas)
