"""Event derivation (FR-5).

Many questions ask about something that *happened*, where the document is
merely where it was recorded. "When did I last fly Alaska" is answered by a
flight event, and its evidence might be a confirmation email, a boarding pass
PDF, or a line on a card statement -- the user does not care which.

Events are derived from records by rules, deterministically. Deduplication is
by a stable key of (type, date, participating entities), so the same flight
recorded in three documents is one event with three pieces of evidence rather
than three events.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field

from . import entities
from .db import utcnow


@dataclass
class DerivedEvent:
    event_type: str
    occurred_on: str | None
    precision: str = "day"
    title: str = ""
    confidence: float = 0.8
    entity_roles: list[tuple[int, str]] = field(default_factory=list)


# Record types and field keys that imply an event, with the date field to use.
# Open vocabulary: an unmatched record simply yields no event.
EVENT_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    # (event_type, matching record_type or key, candidate date keys)
    ("flight", "flight_booking", ("departure_date", "departure", "date")),
    ("income", "tax_form_w2", ("tax_year",)),
    ("purchase", "receipt", ("purchase_date", "date_of_purchase", "document_date",
                             "date")),
    ("payment", "rent_receipt", ("payment_date", "document_date", "date")),
    ("appointment", "appointment", ("appointment_date", "document_date", "date")),
]

# Keys whose presence alone implies an event type, for records the rules above
# do not name. This is what lets an unfamiliar document still yield an event.
KEY_EVENT_HINTS: dict[str, str] = {
    "confirmation_number": "booking",
    "flight_number": "flight",
    "departure_date": "flight",
    "invoice_number": "purchase",
    "order_number": "purchase",
    "amount": "transaction",
    "expiry_date": "expiry",
    "policy_number": "policy",
}

DATE_KEYS = ("document_date", "date", "departure_date", "purchase_date",
             "payment_date", "appointment_date", "issue_date", "expiry_date",
             "date_of_service", "transaction_date")


def derive_for_item(conn: sqlite3.Connection, item_id: int) -> int:
    """Derive events from an item's records. Returns the number attached.

    Existing evidence links for this item are cleared first, so re-ingest
    cannot accumulate duplicate evidence rows.
    """
    conn.execute("DELETE FROM event_evidence WHERE item_id = ?", (item_id,))

    records = conn.execute(
        "SELECT id, record_type, source, confidence FROM records "
        "WHERE item_id = ? ORDER BY id", (item_id,)
    ).fetchall()

    attached = 0
    for record in records:
        if record["record_type"] == "correction":
            continue
        derived = _derive_from_record(conn, record)
        if derived is None:
            continue
        event_id = _upsert_event(conn, derived)
        conn.execute(
            "INSERT INTO event_evidence (event_id, item_id, record_id, passage_id) "
            "VALUES (?, ?, ?, NULL) ON CONFLICT DO NOTHING",
            (event_id, item_id, record["id"]),
        )
        attached += 1

    _prune_orphans(conn)
    return attached


def _derive_from_record(conn: sqlite3.Connection,
                        record: sqlite3.Row) -> DerivedEvent | None:
    fields = {
        r["key"]: r for r in conn.execute(
            "SELECT key, value_text, value_num, value_date FROM record_fields "
            "WHERE record_id = ?", (record["id"],)
        ).fetchall()
    }
    if not fields:
        return None

    event_type, date_keys = _classify(record["record_type"], fields)
    if event_type is None:
        return None

    occurred_on, precision = _pick_date(fields, date_keys)
    if occurred_on is None:
        return None

    entity_roles = entities.link_record(conn, record["id"])

    return DerivedEvent(
        event_type=event_type,
        occurred_on=occurred_on,
        precision=precision,
        title=_title(event_type, fields, conn, entity_roles),
        confidence=float(record["confidence"]),
        entity_roles=entity_roles,
    )


def _classify(record_type: str,
              fields: dict) -> tuple[str | None, tuple[str, ...]]:
    for event_type, matcher, date_keys in EVENT_RULES:
        if record_type == matcher:
            return event_type, date_keys

    for key, event_type in KEY_EVENT_HINTS.items():
        if key in fields:
            return event_type, DATE_KEYS
    return None, ()


def _pick_date(fields: dict, date_keys: tuple[str, ...]) -> tuple[str | None, str]:
    """First usable date, in the order the rule prefers."""
    for key in list(date_keys) + list(DATE_KEYS):
        row = fields.get(key)
        if row is None:
            continue
        if row["value_date"]:
            return row["value_date"], "day"
        # A bare year (a tax form) is a real date at year precision.
        if row["value_num"] and 1900 <= float(row["value_num"]) <= 2200:
            return f"{int(row['value_num'])}-01-01", "year"
        if row["value_text"]:
            m = re.search(r"\b(19\d{2}|20\d{2})\b", row["value_text"])
            if m:
                return f"{m.group(1)}-01-01", "year"
    return None, "day"


def _title(event_type: str, fields: dict, conn: sqlite3.Connection,
           entity_roles: list[tuple[int, str]]) -> str:
    """A short human label. Purely cosmetic -- never used for matching."""
    for entity_id, _ in entity_roles:
        row = conn.execute(
            "SELECT canonical_name FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row:
            return f"{event_type}: {row['canonical_name']}"

    for key in ("merchant", "employer", "description", "procedure", "title"):
        row = fields.get(key)
        if row and row["value_text"]:
            return f"{event_type}: {row['value_text'][:60]}"
    return event_type


def _dedup_key(event: DerivedEvent) -> str:
    """Stable identity: type, date, and participating entities.

    The same flight recorded in an email, a boarding pass, and a statement line
    produces one event with three pieces of evidence.
    """
    parts = [event.event_type, event.occurred_on or ""]
    parts.extend(sorted(f"{eid}:{role}" for eid, role in event.entity_roles))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _upsert_event(conn: sqlite3.Connection, event: DerivedEvent) -> int:
    key = _dedup_key(event)
    row = conn.execute("SELECT id FROM events WHERE dedup_key = ?", (key,)).fetchone()
    if row:
        return int(row["id"])

    cur = conn.execute(
        "INSERT INTO events (event_type, occurred_on, occurred_precision, title, "
        "confidence, source, dedup_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'rule', ?, ?)",
        (event.event_type, event.occurred_on, event.precision, event.title,
         event.confidence, key, utcnow()),
    )
    event_id = int(cur.lastrowid)

    for entity_id, role in event.entity_roles:
        conn.execute(
            "INSERT INTO event_entities (event_id, entity_id, role) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (event_id, entity_id, role),
        )
    return event_id


def _prune_orphans(conn: sqlite3.Connection) -> None:
    """Drop events whose last evidence is gone.

    An event with no surviving document is not a memory, it is a stale row.
    """
    conn.execute(
        "DELETE FROM events WHERE id NOT IN "
        "(SELECT DISTINCT event_id FROM event_evidence)"
    )


# ------------------------------------------------------------------ querying

def query(conn: sqlite3.Connection, *, event_type: str | None = None,
          entity: str | None = None, since: str | None = None,
          until: str | None = None, limit: int = 50,
          order: str = "desc") -> list[sqlite3.Row]:
    """Filter events. Deterministic: filter, sort, tie-break on id."""
    sql = ["SELECT DISTINCT e.id, e.event_type, e.occurred_on, "
           "e.occurred_precision, e.title, e.confidence",
           "FROM events e"]
    params: list[object] = []

    if entity:
        sql.append("JOIN event_entities ee ON ee.event_id = e.id")
        sql.append("JOIN entity_aliases a ON a.entity_id = ee.entity_id")

    sql.append("WHERE 1=1")
    if event_type:
        sql.append("AND e.event_type = ?")
        params.append(event_type)
    if entity:
        sql.append("AND a.alias_norm = ?")
        params.append(entities.normalize(entity))
    if since:
        sql.append("AND e.occurred_on >= ?")
        params.append(since)
    if until:
        sql.append("AND e.occurred_on <= ?")
        params.append(until)

    direction = "DESC" if order.lower() == "desc" else "ASC"
    sql.append(f"ORDER BY e.occurred_on {direction}, e.id {direction}")
    sql.append("LIMIT ?")
    params.append(limit)

    return conn.execute(" ".join(sql), params).fetchall()


def evidence(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    """Every document supporting an event -- the citation for FR-5."""
    return conn.execute(
        "SELECT DISTINCT i.id AS item_id, i.title, i.uri, ev.record_id "
        "FROM event_evidence ev JOIN items i ON i.id = ev.item_id "
        "WHERE ev.event_id = ? AND i.deleted_at IS NULL "
        "ORDER BY i.id", (event_id,)
    ).fetchall()


def event_entities(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.id, e.canonical_name, e.entity_type, ee.role "
        "FROM event_entities ee JOIN entities e ON e.id = ee.entity_id "
        "WHERE ee.event_id = ? ORDER BY ee.role, e.canonical_name",
        (event_id,)
    ).fetchall()
