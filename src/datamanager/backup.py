"""Backup and restore of the human-authored layer (NFR-6).

Almost the entire index regenerates from the NAS: passages, records, events,
entities, embeddings. What cannot be regenerated is what a person decided --
corrections, notes, entity merges, pinned vocabulary, named face clusters.

That layer is small, so it exports to a single JSON file that is cheap to back
up to the NAS and safe to restore into a rebuilt index.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import corrections, vocabulary
from .db import transaction, utcnow

FORMAT_VERSION = 1


def export_human_layer(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything a person authored, keyed by content rather than row id.

    Rows are matched back by content hash and path, not by primary key, so a
    restore works against an index rebuilt from scratch where every id differs.
    """
    corrections_out = []
    for row in corrections.list_corrections(conn):
        item = conn.execute(
            "SELECT uri, content_hash, title FROM items WHERE id = ?",
            (row["item_id"],)
        ).fetchone()
        if item is None:
            continue
        corrections_out.append({
            "uri": item["uri"],
            "content_hash": item["content_hash"],
            "title": item["title"],
            "key": row["key"],
            "value_text": row["value_text"],
            "unit": row["unit"],
        })

    notes_out = []
    for item in conn.execute(
        "SELECT id, title, created_at FROM items "
        "WHERE kind = 'note' AND deleted_at IS NULL ORDER BY id"
    ).fetchall():
        versions = conn.execute(
            "SELECT version, text, valid_from FROM item_versions "
            "WHERE item_id = ? ORDER BY version", (item["id"],)
        ).fetchall()
        notes_out.append({
            "title": item["title"],
            "created_at": item["created_at"],
            "versions": [{"version": int(v["version"]), "text": v["text"],
                          "valid_from": v["valid_from"]} for v in versions],
        })

    entities_out = []
    for entity in conn.execute(
        "SELECT id, entity_type, canonical_name FROM entities ORDER BY id"
    ).fetchall():
        aliases = conn.execute(
            "SELECT alias, source FROM entity_aliases WHERE entity_id = ? "
            "ORDER BY alias", (entity["id"],)
        ).fetchall()
        # Only entities carrying a human decision are irreplaceable; the rest
        # re-derive from the documents on the next ingest.
        if not any(a["source"] == "human" for a in aliases):
            continue
        entities_out.append({
            "entity_type": entity["entity_type"],
            "canonical_name": entity["canonical_name"],
            "aliases": [a["alias"] for a in aliases],
        })

    vocab_out = [
        {"key": r["key"], "canonical_key": r["canonical_key"]}
        for r in conn.execute(
            "SELECT key, canonical_key FROM key_vocabulary "
            "WHERE pinned_by_user = 1 ORDER BY key")
    ]

    faces_out = [
        {"cluster_id": int(r["cluster_id"]), "name": r["name"]}
        for r in conn.execute(
            "SELECT cluster_id, name FROM face_clusters "
            "WHERE name IS NOT NULL ORDER BY cluster_id")
    ]

    return {
        "format_version": FORMAT_VERSION,
        "exported_at": utcnow(),
        "corrections": corrections_out,
        "notes": notes_out,
        "entities": entities_out,
        "vocabulary": vocab_out,
        "face_clusters": faces_out,
    }


def write_backup(conn: sqlite3.Connection, path: Path | str) -> dict[str, int]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = export_human_layer(conn)
    path.write_text(json.dumps(payload, indent=2))
    return {
        "corrections": len(payload["corrections"]),
        "notes": len(payload["notes"]),
        "entities": len(payload["entities"]),
        "vocabulary": len(payload["vocabulary"]),
        "face_clusters": len(payload["face_clusters"]),
    }


def restore_backup(conn: sqlite3.Connection, path: Path | str) -> dict[str, int]:
    """Restore into an index rebuilt from the NAS.

    Corrections are matched by content hash first, then by path -- a file that
    moved since the backup is still matched by its content.
    """
    payload = json.loads(Path(path).read_text())
    if payload.get("format_version") != FORMAT_VERSION:
        raise ValueError(
            f"unsupported backup format: {payload.get('format_version')}")

    applied = {"corrections": 0, "notes": 0, "entities": 0,
               "vocabulary": 0, "face_clusters": 0, "unmatched": 0}

    for entry in payload.get("corrections", []):
        item = None
        if entry.get("content_hash"):
            item = conn.execute(
                "SELECT id FROM items WHERE content_hash = ? LIMIT 1",
                (entry["content_hash"],)).fetchone()
        if item is None and entry.get("uri"):
            item = conn.execute("SELECT id FROM items WHERE uri = ?",
                                (entry["uri"],)).fetchone()
        if item is None:
            applied["unmatched"] += 1
            continue
        corrections.correct_field(conn, int(item["id"]), entry["key"],
                                  entry["value_text"], unit=entry.get("unit"))
        applied["corrections"] += 1

    from . import notes as notes_module

    for entry in payload.get("notes", []):
        versions = entry.get("versions") or []
        if not versions:
            continue
        existing = conn.execute(
            "SELECT id FROM items WHERE kind = 'note' AND title = ?",
            (entry["title"],)).fetchone()
        if existing:
            continue         # never overwrite a note that is already there
        item_id = notes_module.create_note(conn, entry["title"],
                                           versions[0]["text"])
        for version in versions[1:]:
            notes_module.update_note(conn, item_id, version["text"])
        applied["notes"] += 1

    from . import entities as entity_module

    for entry in payload.get("entities", []):
        aliases = entry.get("aliases") or [entry["canonical_name"]]
        entity_id = entity_module.get_or_create(
            conn, entry["canonical_name"], entry["entity_type"], source="human")
        for alias in aliases:
            other = entity_module.resolve(conn, alias, entry["entity_type"])
            if other is not None and other != entity_id:
                entity_module.merge(conn, other, entity_id)
            else:
                entity_module.get_or_create(conn, alias, entry["entity_type"],
                                            source="human")
        applied["entities"] += 1

    for entry in payload.get("vocabulary", []):
        vocabulary.pin(conn, entry["key"], entry["canonical_key"])
        applied["vocabulary"] += 1

    with transaction(conn) as c:
        for entry in payload.get("face_clusters", []):
            c.execute("UPDATE face_clusters SET name = ? WHERE cluster_id = ?",
                      (entry["name"], entry["cluster_id"]))
            applied["face_clusters"] += 1

    return applied
