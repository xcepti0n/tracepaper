"""Walking the indexed tree, as folders and files.

Browse used to list extracted field names. That answered "what did
extraction find", which is a real question but not the one the word "browse"
sets up, and on a corpus containing source code it led with `namespace_winrt`
and `return_impl`.

This answers the question the tab's name promises: what is in this folder,
what got indexed, and is any of it worth marking as code.

Everything here reads the INDEX, not the disk. Two reasons: the index already
knows what was indexed and what was skipped, which is the interesting part,
and it means the page cannot be used to walk the filesystem outside the
configured roots.
"""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath

from . import rules
from .query import modes
from .rules import canonical_prefix


def _root_for(roots: list[str], path: str) -> str | None:
    """The configured root containing this path, if any.

    Containment is checked on a path-segment boundary, so `/mnt/nas/docs2`
    is not treated as living inside `/mnt/nas/doc`.
    """
    for root in roots:
        trimmed = root.rstrip("/")
        if path == trimmed or path.startswith(trimmed + "/"):
            return trimmed
    return None


def listing(conn: sqlite3.Connection, roots: list[str],
            path: str | None = None, *, limit: int = 500) -> dict:
    """Folders and files directly inside `path`.

    With no path, lists the configured roots themselves, so the first screen
    is the same shape as every screen below it.
    """
    # Resolve, for the same reason rules do: the scanner stores resolved
    # paths, so a configured root of `/var/...` never prefix-matches an item
    # indexed under `/private/var/...`. This is the bug that made every
    # folder listing come back empty.
    roots = [canonical_prefix(str(r)) for r in roots]

    if not path:
        return {
            "path": None,
            "parent": None,
            "folders": [_folder_entry(conn, root, root) for root in roots],
            "files": [],
            "truncated": False,
        }

    path = canonical_prefix(path)
    root = _root_for(roots, path)
    if root is None:
        # Never walk outside a configured root, whatever the URL says.
        return {"path": path, "parent": None, "folders": [], "files": [],
                "outside_roots": True, "truncated": False}

    prefix = path + "/"
    rows = conn.execute(
        "SELECT id, uri, title, kind, size_bytes, extraction_status "
        "FROM items WHERE deleted_at IS NULL AND uri LIKE ? || '%' "
        "ORDER BY uri LIMIT ?", (prefix, limit * 40)).fetchall()

    folders: dict[str, dict] = {}
    files: list[dict] = []
    for row in rows:
        rest = row["uri"][len(prefix):]
        if "/" in rest:
            # Something deeper: count it against the folder it sits under.
            name = rest.split("/", 1)[0]
            entry = folders.setdefault(
                name, {"name": name, "path": prefix + name, "items": 0,
                       "code_items": 0})
            entry["items"] += 1
            if modes.is_code(row["uri"]):
                entry["code_items"] += 1
        elif len(files) < limit:
            files.append({
                "item_id": int(row["id"]),
                "name": rest,
                "uri": row["uri"],
                "kind": row["kind"],
                "size_bytes": row["size_bytes"],
                "status": row["extraction_status"],
                "is_code": modes.is_code(row["uri"]),
            })

    for entry in folders.values():
        entry["rule"] = _rule_for(conn, entry["path"])

    parent = str(PurePosixPath(path).parent)
    if _root_for(roots, path) == path:
        parent = ""           # a root's parent is the list of roots

    return {
        "path": path,
        "parent": parent,
        "rule": _rule_for(conn, path),
        "folders": sorted(folders.values(), key=lambda f: f["name"].lower()),
        "files": files,
        "truncated": len(rows) >= limit * 40,
    }


def _folder_entry(conn: sqlite3.Connection, path: str, name: str) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE deleted_at IS NULL "
        "AND uri LIKE ? || '/%'", (path.rstrip("/"),)).fetchone()["n"]
    return {"name": name, "path": path.rstrip("/"), "items": int(total),
            "code_items": 0, "rule": _rule_for(conn, path)}


def _rule_for(conn: sqlite3.Connection, path: str) -> str | None:
    """The rule covering this folder, if you set one."""
    for rule in rules.RULES:
        if rules.is_ruled(conn, path + "/", rule):
            return rule
    return None
