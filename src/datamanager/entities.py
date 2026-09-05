"""Entity resolution (FR-6).

`COSTCO WHSE #1234` on a card statement and `Costco` in an email are one
merchant. Without resolution, "everything about Costco" misses most of it and
Tier 2 evidence gathering is incomplete.

Resolution is deterministic: normalise, strip corporate noise, then match
exactly against known aliases. No fuzzy scoring and no embeddings -- a merchant
silently merged into the wrong one is worse than two rows the user merges by
hand, and fuzzy matching is not reproducible across library versions (NFR-2).
"""

from __future__ import annotations

import re
import sqlite3

from .db import transaction, utcnow

# Legal-form suffixes and store noise. Stripped for matching only; the
# canonical name keeps whatever the user or the first sighting called it.
_NOISE = re.compile(
    r"\b(?:inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|sa|nv|bv|"
    r"pvt|pte|llp|lp|holdings|group|intl|international|the)\b\.?",
    re.IGNORECASE,
)

# Store/branch identifiers that vary per transaction but mean one merchant.
_BRANCH = re.compile(
    r"(?:\B#\s*\d+|\bstore\s*\d+|\bwhse\s*\d*|\bwholesale\b|\bstr\s*\d+|"
    r"\b\d{4,}\b)",
    re.IGNORECASE,
)

# Card-statement prefixes and payment noise.
_STATEMENT_NOISE = re.compile(
    r"^(?:sq|tst|pos|pmt|ach|debit|credit|purchase|payment|recurring)[\s*\-]+",
    re.IGNORECASE,
)


def normalize(name: str) -> str:
    """Fold a name to its matching form.

    Aggressive by design -- it only decides whether two spellings are the same
    entity, and never replaces what is shown to the user.
    """
    text = name.strip().lower()
    text = _STATEMENT_NOISE.sub("", text)
    text = _BRANCH.sub(" ", text)
    text = _NOISE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def resolve(conn: sqlite3.Connection, name: str,
            entity_type: str = "organization") -> int | None:
    """Find an existing entity for a name. Returns None when unknown."""
    norm = normalize(name)
    if not norm:
        return None
    row = conn.execute(
        "SELECT e.id FROM entity_aliases a JOIN entities e ON e.id = a.entity_id "
        "WHERE a.alias_norm = ? AND e.entity_type = ? LIMIT 1",
        (norm, entity_type),
    ).fetchone()
    return int(row["id"]) if row else None


def get_or_create(conn: sqlite3.Connection, name: str,
                  entity_type: str = "organization",
                  source: str = "pattern") -> int:
    """Resolve a name to an entity, creating one if it is new."""
    norm = normalize(name)
    if not norm:
        raise ValueError(f"name normalises to nothing: {name!r}")

    existing = resolve(conn, name, entity_type)
    if existing is not None:
        _add_alias(conn, existing, name, norm, source)
        return existing

    cur = conn.execute(
        "INSERT INTO entities (entity_type, canonical_name, created_at) "
        "VALUES (?, ?, ?) ON CONFLICT(entity_type, canonical_name) DO NOTHING",
        (entity_type, name.strip(), utcnow()),
    )
    if cur.lastrowid:
        entity_id = int(cur.lastrowid)
    else:
        row = conn.execute(
            "SELECT id FROM entities WHERE entity_type = ? AND canonical_name = ?",
            (entity_type, name.strip()),
        ).fetchone()
        entity_id = int(row["id"])

    _add_alias(conn, entity_id, name, norm, source)
    return entity_id


def _add_alias(conn: sqlite3.Connection, entity_id: int, alias: str,
               alias_norm: str, source: str) -> None:
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias, alias_norm, source) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(alias, entity_id) DO NOTHING",
        (entity_id, alias.strip(), alias_norm, source),
    )


def merge(conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
    """Merge two entities by hand. Permanent, and outranks any rule (FR-10)."""
    if source_id == target_id:
        return
    with transaction(conn) as c:
        c.execute(
            "UPDATE OR IGNORE entity_aliases SET entity_id = ?, source = 'human' "
            "WHERE entity_id = ?", (target_id, source_id),
        )
        c.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (source_id,))
        c.execute(
            "UPDATE OR IGNORE event_entities SET entity_id = ? WHERE entity_id = ?",
            (target_id, source_id),
        )
        c.execute("DELETE FROM event_entities WHERE entity_id = ?", (source_id,))
        c.execute("DELETE FROM entities WHERE id = ?", (source_id,))


def find(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Look up entities by any known alias, for search and the UI."""
    norm = normalize(name)
    if not norm:
        return []
    return conn.execute(
        "SELECT DISTINCT e.id, e.entity_type, e.canonical_name "
        "FROM entities e JOIN entity_aliases a ON a.entity_id = e.id "
        "WHERE a.alias_norm = ? OR a.alias_norm LIKE ? "
        "ORDER BY e.canonical_name",
        (norm, f"%{norm}%"),
    ).fetchall()


def aliases(conn: sqlite3.Connection, entity_id: int) -> list[str]:
    return [r["alias"] for r in conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
        (entity_id,)
    ).fetchall()]


def list_all(conn: sqlite3.Connection, entity_type: str | None = None,
             limit: int = 200) -> list[sqlite3.Row]:
    sql = ("SELECT e.id, e.entity_type, e.canonical_name, "
           "COUNT(DISTINCT a.id) AS alias_count, "
           "COUNT(DISTINCT ee.event_id) AS event_count "
           "FROM entities e "
           "LEFT JOIN entity_aliases a ON a.entity_id = e.id "
           "LEFT JOIN event_entities ee ON ee.entity_id = e.id ")
    params: list[object] = []
    if entity_type:
        sql += "WHERE e.entity_type = ? "
        params.append(entity_type)
    sql += "GROUP BY e.id ORDER BY event_count DESC, e.canonical_name LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


# Field keys whose values name an organization, and the role they play.
ORG_FIELD_ROLES: dict[str, str] = {
    "merchant": "merchant",
    "employer": "employer",
    "airline": "airline",
    "carrier": "airline",
    "landlord": "landlord",
    "clinic": "provider",
    "provider": "provider",
    "vendor": "merchant",
    "issuer": "issuer",
    "bank": "bank",
    "insurer": "insurer",
    "company": "organization",
    "contractor": "contractor",
    "store": "merchant",
}


def link_record(conn: sqlite3.Connection, record_id: int) -> list[tuple[int, str]]:
    """Create or resolve entities named by a record's fields.

    Returns (entity_id, role) pairs for event derivation to attach.
    """
    rows = conn.execute(
        "SELECT key, value_text FROM record_fields WHERE record_id = ?",
        (record_id,),
    ).fetchall()

    linked: list[tuple[int, str]] = []
    for row in rows:
        role = ORG_FIELD_ROLES.get(row["key"])
        if not role or not row["value_text"]:
            continue
        value = row["value_text"].strip()
        # Values long enough to be prose are descriptions, not names.
        if not value or len(value) > 80:
            continue
        try:
            entity_id = get_or_create(conn, value, "organization")
        except ValueError:
            continue
        linked.append((entity_id, role))
    return linked
