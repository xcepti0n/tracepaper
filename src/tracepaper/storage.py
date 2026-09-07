"""Storage configuration and validation (NFR-3, NFR-7, D-008).

Three paths, with different requirements:

- **Source roots** -- the documents, on NFS/SMB, mounted read-only. Never
  written to (NFR-7).
- **Index** -- must be on local disk. SQLite over NFS corrupts: NFS advisory
  locking is unreliable across clients, and SQLite's WAL mode depends on it.
  This fails silently and weeks later, so it is checked and refused up front.
- **Backup directory** -- on the NAS, writable. Holds the human-authored layer
  and index snapshots, which is what makes losing the local index survivable.

Mounting is deliberately not this application's job. Mounting needs root, and
an app that mounts filesystems turns every stale handle and credential problem
into its own bug. `/etc/fstab` or a systemd mount unit does it properly and
survives a reboot; this module verifies what it finds and explains what to fix.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Filesystems where SQLite's locking cannot be trusted.
NETWORK_FILESYSTEMS = {
    "nfs", "nfs4", "cifs", "smbfs", "smb2", "afpfs", "webdav", "fuse.sshfs",
    "ftp", "davfs", "9p", "gvfsd-fuse",
}


@dataclass
class PathCheck:
    path: str
    ok: bool
    exists: bool = False
    is_dir: bool = False
    readable: bool = False
    writable: bool = False
    filesystem: str = ""
    is_network: bool = False
    free_bytes: int = 0
    file_count: int | None = None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": self.path, "ok": self.ok, "exists": self.exists,
            "is_dir": self.is_dir, "readable": self.readable,
            "writable": self.writable, "filesystem": self.filesystem,
            "is_network": self.is_network, "free_bytes": self.free_bytes,
            "free_human": human_bytes(self.free_bytes),
            "file_count": self.file_count,
            "problems": self.problems, "warnings": self.warnings,
        }


def human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024
    return f"{value:.1f} TB"


def _mount_table() -> list[tuple[str, str, str]]:
    """(mount point, filesystem type, options) for every mount.

    Parsed from `mount`, which reports the real filesystem type on both macOS
    and Linux. `stat -f %T` does not: on macOS it returns the volume label.
    """
    entries: list[tuple[str, str, str]] = []
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return entries

    for line in out.stdout.splitlines():
        if " on " not in line:
            continue
        _, _, rest = line.partition(" on ")
        # Linux is checked first: its lines contain BOTH " type " and "(", so
        # testing for the parenthesis first would mangle every Linux mount
        # point -- which is where this actually has to work.
        if " type " in rest:
            # Linux: "server:/export on /mnt type nfs4 (rw,relatime)"
            point, _, tail = rest.partition(" type ")
            fs_type = tail.split()[0].strip().lower()
            options = tail.lower()
        elif "(" in rest:
            # macOS: "/dev/disk1 on / (apfs, local, journaled)"
            point, _, tail = rest.partition(" (")
            options = tail.rstrip(")")
            fs_type = options.split(",")[0].strip().lower()
        else:
            continue
        entries.append((point.strip(), fs_type, options.lower()))

    # Longest mount point first, so /mnt/nas wins over / for a nested path.
    entries.sort(key=lambda e: len(e[0]), reverse=True)
    return entries


def filesystem_type(path: Path) -> str:
    """The filesystem backing a path, as the mount table reports it."""
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        resolved = str(target.resolve())
    except OSError:
        resolved = str(target)

    for point, fs_type, _ in _mount_table():
        if resolved == point or resolved.startswith(point.rstrip("/") + "/"):
            return fs_type
    return "unknown"


def is_network_filesystem(path: Path) -> bool:
    fs = filesystem_type(path)
    return any(fs.startswith(name) for name in NETWORK_FILESYSTEMS)


def check_source(path: str | Path) -> PathCheck:
    """A documents folder: must exist and be readable. Network is expected."""
    target = Path(path).expanduser()
    check = PathCheck(path=str(target), ok=False)

    if not target.exists():
        check.problems.append(
            "Does not exist. If this is an NFS share, it is probably not "
            "mounted — check `mount | grep nfs` and your /etc/fstab entry.")
        return check

    check.exists = True
    check.is_dir = target.is_dir()
    if not check.is_dir:
        check.problems.append("Not a directory.")
        return check

    check.readable = os.access(target, os.R_OK | os.X_OK)
    if not check.readable:
        check.problems.append(
            "Not readable by this user. Check the export's permissions and "
            "the uid the service runs as.")
        return check

    check.filesystem = filesystem_type(target)
    check.is_network = is_network_filesystem(target)
    check.writable = os.access(target, os.W_OK)

    if check.writable:
        # Not an error -- the application never writes here -- but a read-only
        # mount makes that guarantee enforceable by the OS too (NFR-7).
        check.warnings.append(
            "Mounted read-write. Tracepaper never writes here, but mounting "
            "with `ro` makes that guarantee enforced by the OS as well.")

    try:
        entries = list(os.scandir(target))
        check.file_count = sum(1 for e in entries if e.is_file())
        if not entries:
            check.warnings.append("Empty. Is the right share mounted?")
    except OSError as exc:
        check.problems.append(f"Cannot list: {exc}")
        return check

    check.free_bytes = _free_bytes(target)
    check.ok = True
    return check


def check_index(path: str | Path) -> PathCheck:
    """The index location: must be local and writable.

    SQLite over NFS/SMB corrupts, and does so silently and late. It is refused
    rather than warned about (D-008).
    """
    target = Path(path).expanduser()
    parent = target.parent
    check = PathCheck(path=str(target), ok=False)

    # The filesystem is checked BEFORE trying to create anything: on a real
    # NFS mount the directory usually already exists, so a creation failure
    # would mask the reason that actually matters.
    check.filesystem = filesystem_type(parent)
    check.is_network = is_network_filesystem(parent)

    if check.is_network:
        check.problems.append(
            f"This is a network filesystem ({check.filesystem}). SQLite "
            "corrupts over NFS/SMB because their file locking is unreliable "
            "across clients — and it fails silently, weeks later. Put the "
            "index on local disk and back it up to the NAS instead; it "
            "rebuilds from your documents anyway.")
        return check

    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            check.problems.append(f"Cannot create {parent}: {exc}")
            return check

    check.exists = target.exists()
    check.is_dir = False
    check.readable = os.access(parent, os.R_OK)
    check.writable = os.access(parent, os.W_OK)
    check.free_bytes = _free_bytes(parent)

    if not check.writable:
        check.problems.append(f"{parent} is not writable by this user.")
        return check

    if not _locking_works(parent):
        check.problems.append(
            "File locking does not work here, so SQLite cannot run safely. "
            "This usually means a network or fuse filesystem.")
        return check

    if check.free_bytes and check.free_bytes < 1024 ** 3:
        check.warnings.append(
            f"Only {human_bytes(check.free_bytes)} free. The index grows with "
            "the corpus; allow a few GB.")

    check.ok = True
    return check


def check_backup(path: str | Path) -> PathCheck:
    """The backup directory: must be writable. The NAS is the right home."""
    target = Path(path).expanduser()
    check = PathCheck(path=str(target), ok=False)

    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            check.problems.append(
                f"Cannot create: {exc}. If this is an NFS share, check it is "
                "mounted read-write and that the service's uid can write to it.")
            return check

    check.exists = True
    check.is_dir = target.is_dir()
    if not check.is_dir:
        check.problems.append("Not a directory.")
        return check

    check.readable = os.access(target, os.R_OK)
    check.writable = os.access(target, os.W_OK)
    check.filesystem = filesystem_type(target)
    check.is_network = is_network_filesystem(target)
    check.free_bytes = _free_bytes(target)

    if not check.writable:
        check.problems.append(
            "Not writable by this user. On a Synology NFS export, map the "
            "service's uid or set squash appropriately.")
        return check

    # Proving the write works matters more than the permission bits, which lie
    # on a squashed NFS export.
    try:
        with tempfile.NamedTemporaryFile(dir=target, prefix=".tp-write-test-",
                                         delete=True) as fh:
            fh.write(b"ok")
            fh.flush()
    except OSError as exc:
        check.problems.append(f"Write test failed: {exc}")
        return check

    if not check.is_network:
        check.warnings.append(
            "This is local disk. Backups are safer on the NAS — that is the "
            "copy that survives losing this machine.")

    check.ok = True
    return check


def _free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _locking_works(directory: Path) -> bool:
    """Whether SQLite can actually take the locks it needs here."""
    probe = directory / f".tp-lock-probe-{os.getpid()}.db"
    try:
        conn = sqlite3.connect(str(probe))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO probe (id) VALUES (1)")
        conn.execute("COMMIT")
        conn.close()
        return True
    except sqlite3.Error:
        return False
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(probe) + suffix).unlink(missing_ok=True)


def check_all(roots: list[str], index_path: str,
              backup_dir: str | None) -> dict:
    """Validate every configured path. Used by the settings page."""
    return {
        "sources": [check_source(root).as_dict() for root in roots],
        "index": check_index(index_path).as_dict(),
        "backup": check_backup(backup_dir).as_dict() if backup_dir else None,
    }


def mounts() -> list[dict]:
    """Currently mounted network shares, to offer as choices in the UI."""
    found: list[dict] = []
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return found

    table = {point: (fs, opts) for point, fs, opts in _mount_table()}
    for line in out.stdout.splitlines():
        if " on " not in line:
            continue
        source, _, rest = line.partition(" on ")
        point = rest.split(" (")[0].split(" type ")[0].strip()
        fs_type, options = table.get(point, ("", ""))
        if not any(fs_type.startswith(name) for name in NETWORK_FILESYSTEMS):
            continue
        found.append({
            "source": source.strip(),
            "path": point,
            "type": fs_type,
            "read_only": "read-only" in options or "ro," in options
                         or options.rstrip().endswith("ro"),
        })
    return found
