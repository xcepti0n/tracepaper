"""Storage validation and settings persistence (D-008, NFR-7, FR-12)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tracepaper import settings, storage


def nfs_at(point: str):
    """Pretend a path is an NFS mount."""
    return patch.object(storage, "_mount_table",
                        return_value=[(point, "nfs4", "rw,relatime"),
                                      ("/", "ext4", "rw")])


# ------------------------------------------------------- filesystem detection

def test_local_filesystem_is_not_network(tmp_path: Path):
    assert not storage.is_network_filesystem(tmp_path)


def test_network_filesystem_is_detected(tmp_path: Path):
    with nfs_at(str(tmp_path.resolve())):
        assert storage.is_network_filesystem(tmp_path)
        assert storage.filesystem_type(tmp_path) == "nfs4"


def test_longest_mount_point_wins(tmp_path: Path):
    """A nested share must not be reported as the root filesystem.

    _mount_table sorts longest-first for exactly this reason, so the parsed
    output is checked rather than a hand-built list.
    """
    nested = tmp_path / "share"
    nested.mkdir()
    mount_output = (f"/dev/sda1 on / type ext4 (rw)\n"
                    f"nas:/export on {nested.resolve()} type nfs4 (rw)\n")

    class FakeRun:
        stdout = mount_output
        returncode = 0

    with patch("subprocess.run", return_value=FakeRun()):
        assert storage.filesystem_type(nested) == "nfs4"
        assert storage.filesystem_type(Path("/")) == "ext4"


# ------------------------------------------------------------ source folders

def test_source_folder_accepts_a_readable_directory(tmp_path: Path):
    (tmp_path / "doc.txt").write_text("content")
    check = storage.check_source(tmp_path)

    assert check.ok
    assert check.file_count == 1


def test_source_folder_on_nfs_is_fine(tmp_path: Path):
    """Documents on NFS are the expected setup, not a problem."""
    (tmp_path / "doc.txt").write_text("content")
    with nfs_at(str(tmp_path.resolve())):
        check = storage.check_source(tmp_path)

    assert check.ok
    assert check.is_network


def test_missing_source_explains_an_unmounted_share(tmp_path: Path):
    check = storage.check_source(tmp_path / "not-mounted")

    assert not check.ok
    assert any("mounted" in p.lower() for p in check.problems)


def test_empty_source_warns_but_is_usable(tmp_path: Path):
    check = storage.check_source(tmp_path)

    assert check.ok, "an empty folder is not an error"
    assert any("empty" in w.lower() for w in check.warnings)


def test_writable_source_suggests_mounting_read_only(tmp_path: Path):
    """NFR-7 is the app's guarantee; a ro mount makes the OS enforce it too."""
    (tmp_path / "doc.txt").write_text("content")
    check = storage.check_source(tmp_path)

    assert check.ok
    assert any("ro" in w for w in check.warnings)


# -------------------------------------------------------------------- index

def test_index_on_local_disk_is_accepted(tmp_path: Path):
    check = storage.check_index(tmp_path / "index.db")

    assert check.ok
    assert not check.is_network


def test_index_on_nfs_is_refused(tmp_path: Path):
    """D-008: SQLite over NFS corrupts, silently and weeks later."""
    with nfs_at(str(tmp_path.resolve())):
        check = storage.check_index(tmp_path / "index.db")

    assert not check.ok
    assert any("corrupt" in p.lower() for p in check.problems)
    assert any("local disk" in p.lower() for p in check.problems), \
        "the message must say what to do instead"


def test_index_check_reports_the_filesystem_not_a_creation_error(tmp_path: Path):
    """The real reason must not be masked by a mkdir failure."""
    with nfs_at(str(tmp_path.resolve())):
        check = storage.check_index(tmp_path / "sub" / "index.db")

    assert check.filesystem == "nfs4"
    assert any("network filesystem" in p.lower() for p in check.problems)


def test_index_locking_is_actually_tested(tmp_path: Path):
    """Permission bits lie; taking a real SQLite lock does not."""
    assert storage._locking_works(tmp_path)


# ------------------------------------------------------------------ backups

def test_backup_on_nfs_is_accepted_without_complaint(tmp_path: Path):
    """The NAS is exactly where backups belong."""
    with nfs_at(str(tmp_path.resolve())):
        check = storage.check_backup(tmp_path / "backups")

    assert check.ok
    assert check.is_network
    assert not check.warnings


def test_backup_on_local_disk_warns(tmp_path: Path):
    check = storage.check_backup(tmp_path / "backups")

    assert check.ok, "local backups still work"
    assert any("nas" in w.lower() for w in check.warnings)


def test_backup_directory_is_created(tmp_path: Path):
    target = tmp_path / "new" / "backups"
    assert storage.check_backup(target).ok
    assert target.exists()


# ----------------------------------------------------------------- settings

def test_settings_round_trip(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("x")
    config = tmp_path / "tracepaper.toml"

    ok, problems = settings.save(
        config, roots=[str(docs)], db_path=str(tmp_path / "index.db"),
        backup_dir=str(tmp_path / "backups"))

    assert ok, problems
    data = settings.load_raw(config)
    assert data["scan"]["roots"] == [str(docs)]
    assert data["index"]["db_path"] == str(tmp_path / "index.db")


def test_settings_refuse_an_invalid_path(tmp_path: Path):
    """A typo must not leave the service pointing at nothing."""
    config = tmp_path / "tracepaper.toml"

    ok, problems = settings.save(
        config, roots=[str(tmp_path / "does-not-exist")],
        db_path=str(tmp_path / "index.db"))

    assert not ok
    assert problems
    assert not config.exists(), "nothing may be written when validation fails"


def test_settings_refuse_an_nfs_index(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("x")
    config = tmp_path / "tracepaper.toml"

    with nfs_at(str(tmp_path.resolve())):
        ok, problems = settings.save(config, roots=[str(docs)],
                                     db_path=str(tmp_path / "index.db"))

    assert not ok
    assert any("corrupt" in p.lower() for p in problems)


def test_settings_preserve_unmanaged_sections(tmp_path: Path):
    """Hand edits outside the UI's fields must survive a save."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("x")
    config = tmp_path / "tracepaper.toml"
    config.write_text('[enrich]\nload_threshold = 0.9\n\n'
                      '[semantic]\nmodel = "custom-model"\n')

    ok, _ = settings.save(config, roots=[str(docs)],
                          db_path=str(tmp_path / "index.db"))

    assert ok
    data = settings.load_raw(config)
    assert data["enrich"]["load_threshold"] == 0.9
    assert data["semantic"]["model"] == "custom-model"


def test_settings_write_is_atomic(tmp_path: Path):
    """A crash mid-write must not truncate a working config."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("x")
    config = tmp_path / "tracepaper.toml"
    settings.save(config, roots=[str(docs)], db_path=str(tmp_path / "index.db"))
    original = config.read_text()

    with patch("os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            settings.save(config, roots=[str(docs)],
                          db_path=str(tmp_path / "other.db"))

    assert config.read_text() == original
    assert not list(tmp_path.glob("*.tmp")), "no temp file left behind"
