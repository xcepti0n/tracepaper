"""Reading and writing the config file (FR-12).

Storage paths change: a share gets remounted, a folder is added, the index
moves to a bigger disk. Editing TOML over SSH for that is the same friction as
dropping to the CLI to fix a value, so the settings page writes the file.

Only the fields the UI exposes are written, and the file is replaced
atomically. A change is never saved unless its paths validate, so a typo
cannot leave the service pointing at nothing.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path

from .config import Config
from .storage import check_backup, check_index, check_source

DEFAULT_CONFIG_NAMES = ("tracepaper.toml", "config/tracepaper.toml")


def find_config() -> Path | None:
    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path(name)
        if candidate.exists():
            return candidate
    return None


def load_raw(path: Path | str) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def validate(roots: list[str], db_path: str,
             backup_dir: str | None) -> tuple[bool, list[str]]:
    """Check a proposed configuration. Returns (ok, problems)."""
    problems: list[str] = []

    if not roots:
        problems.append("At least one documents folder is required.")
    for root in roots:
        check = check_source(root)
        if not check.ok:
            problems.extend(f"{root}: {p}" for p in check.problems)

    if not db_path:
        problems.append("An index location is required.")
    else:
        check = check_index(db_path)
        if not check.ok:
            problems.extend(f"Index: {p}" for p in check.problems)

    if backup_dir:
        check = check_backup(backup_dir)
        if not check.ok:
            problems.extend(f"Backups: {p}" for p in check.problems)

    return not problems, problems


def save(path: Path | str, *, roots: list[str], db_path: str,
         backup_dir: str | None = None,
         llm_enabled: bool | None = None,
         llm_endpoint: str | None = None,
         llm_model: str | None = None,
         validate_paths: bool = True) -> tuple[bool, list[str]]:
    """Write the settings the UI owns, preserving everything else.

    Refuses to save a configuration whose paths do not validate: a typo here
    would leave the service pointing at nothing, and the failure would only
    surface on the next scan.
    """
    path = Path(path)
    roots = [r.strip() for r in roots if r.strip()]

    if validate_paths:
        ok, problems = validate(roots, db_path, backup_dir)
        if not ok:
            return False, problems

    data = load_raw(path)
    data.setdefault("index", {})["db_path"] = db_path
    if backup_dir:
        data["index"]["backup_dir"] = backup_dir
    elif "backup_dir" in data.get("index", {}):
        del data["index"]["backup_dir"]

    data.setdefault("scan", {})["roots"] = roots

    llm = data.setdefault("llm", {})
    if llm_enabled is not None:
        llm["enabled"] = bool(llm_enabled)
    if llm_endpoint:
        llm["endpoint"] = llm_endpoint
    if llm_model:
        llm["model"] = llm_model

    _write_atomic(path, _to_toml(data))
    return True, []


def _write_atomic(path: Path, text: str) -> None:
    """Replace the file in one step, so a crash cannot truncate the config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _to_toml(data: dict) -> str:
    """Serialise the config. Only the shapes this file actually uses."""
    lines = ["# Tracepaper configuration.",
             "# Managed by the settings page; hand edits are preserved.", ""]

    for section in ("index", "scan", "semantic", "llm", "enrich"):
        values = data.get(section)
        if not values:
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_format(value)}")
        lines.append("")

    for section, values in data.items():
        if section in ("index", "scan", "semantic", "llm", "enrich"):
            continue
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {_format(value)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _format(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def apply(cfg: Config, path: Path | str) -> Config:
    """Reload a config from disk, keeping the same object shape."""
    return replace(Config.load(path))
