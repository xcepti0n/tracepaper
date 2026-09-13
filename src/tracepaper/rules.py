"""Rules you set by hand, and what search learns from your clicks.

Two human layers over ranking, both deterministic and both revertible:

    path_rules      "this folder is code" / "hide it" / "rank it up"
    query_feedback  "for THESE words, this document is the one I wanted"

Neither involves a model. A rule is a row you can read, and feedback is a
count, so any result can still be explained by pointing at the arithmetic.

The important restraint is in `query_feedback`: it is keyed on the query, not
on the document. "This file is always best" is not something a search engine
should learn -- it makes one document creep to the top of unrelated searches.
"For the words `3d printer`, this manual is the one he opens" is safe, because
it can only apply when those words come back.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .db import transaction, utcnow

RULES = ("code", "hide", "boost")

# How much a boost rule and accumulated feedback may move a result. Both are
# deliberately small: they reorder near-ties, they do not override relevance.
# A document that does not match the query cannot be promoted into the results
# by either mechanism, because both are applied to hits that already matched.
BOOST_WEIGHT = 0.25
FEEDBACK_WEIGHT = 0.35

# Feedback saturates: the fifth click on the same result should not count as
# much as the first, or one habit would freeze the ranking for that query.
FEEDBACK_SATURATION = 3.0

_TOKEN = re.compile(r"[\w][\w'-]*", re.UNICODE)

# Mirrors query.unified._STOPWORDS. Kept local so normalising a query for
# storage never depends on import order at query time.
_STOPWORDS = {
    "what", "whats", "when", "where", "who", "which", "how", "why",
    "is", "was", "were", "are", "am", "be", "been", "do", "did", "does",
    "my", "mine", "me", "i", "the", "a", "an", "of", "for", "to", "in", "on",
    "at", "from", "with", "and", "or", "much", "many", "last", "first",
    "show", "find", "get", "tell", "give",
}


def normalize_query(text: str) -> str:
    """A query reduced to its meaningful words, sorted.

    "the 3d printer" and "Printer 3D" both become "3d printer", so feedback
    given once applies to the ways you would rephrase the same search. Sorting
    makes word order irrelevant, which is right for keyword-ish queries and is
    the conservative choice: at worst two genuinely different queries share
    feedback, and the effect is bounded by FEEDBACK_WEIGHT either way.
    """
    words = sorted({w.lower() for w in _TOKEN.findall(text)
                    if w.lower() not in _STOPWORDS})
    return " ".join(words)


# ------------------------------------------------------------ path rules

def canonical_prefix(prefix: str) -> str:
    """The path as the scanner would have recorded it.

    A rule typed one way and an item indexed another never match, and the
    failure is silent -- the rule simply does nothing. Three ways that
    happens, all of them things a person would reasonably type:

      - `/var/...` when the scanner resolved it to `/private/var/...`
        (macOS symlinks `/var`; Linux does the same for `/tmp` under systemd)
      - a trailing slash
      - `~` or a relative path

    `resolve()` handles all three. It does not require the path to exist: a
    rule for a folder that is not mounted right now is still a valid rule.
    """
    expanded = Path(prefix).expanduser()
    try:
        resolved = str(expanded.resolve())
    except (OSError, RuntimeError):
        resolved = str(expanded)
    return resolved.rstrip("/") or "/"


def add_rule(conn: sqlite3.Connection, prefix: str, rule: str, *,
             weight: float = 1.0, note: str | None = None) -> dict:
    """Create or replace a rule for a path prefix."""
    if rule not in RULES:
        raise ValueError(f"unknown rule: {rule}")
    prefix = canonical_prefix(prefix)
    with transaction(conn) as c:
        c.execute(
            "INSERT INTO path_rules (prefix, rule, weight, note, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(prefix) DO UPDATE SET rule = excluded.rule, "
            "  weight = excluded.weight, note = excluded.note",
            (prefix, rule, float(weight), note, utcnow()))
    return {"prefix": prefix, "rule": rule, "weight": weight}


def remove_rule(conn: sqlite3.Connection, prefix: str) -> bool:
    with transaction(conn) as c:
        # Try what was asked for first, so a rule stored before canonicalising
        # existed can still be removed by its literal path.
        cursor = c.execute("DELETE FROM path_rules WHERE prefix = ?", (prefix,))
        if not cursor.rowcount:
            cursor = c.execute("DELETE FROM path_rules WHERE prefix = ?",
                               (canonical_prefix(prefix),))
    return cursor.rowcount > 0


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    """Every rule, with how many indexed items it currently covers.

    The count is the point: a rule you cannot see the effect of is a rule you
    will not trust. It answers "did that actually match anything?".
    """
    rows = conn.execute(
        "SELECT r.id, r.prefix, r.rule, r.weight, r.note, r.created_at, "
        "  (SELECT COUNT(*) FROM items i "
        "   WHERE i.deleted_at IS NULL AND i.uri LIKE r.prefix || '%') AS items "
        "FROM path_rules r ORDER BY r.rule, r.prefix"
    ).fetchall()
    return [dict(row) for row in rows]


def _prefixes(conn: sqlite3.Connection, rule: str) -> list[str]:
    return [r["prefix"] for r in conn.execute(
        "SELECT prefix FROM path_rules WHERE rule = ?", (rule,))]


def sql_clause(conn: sqlite3.Connection, rule: str, alias: str = "i") -> str:
    """A predicate matching items under any prefix with this rule.

    Returns "0" (never true) when no such rule exists, so callers can always
    interpolate it without special-casing the empty set.

    Values are escaped for a literal rather than bound, because this is
    composed into SQL that already carries positional parameters in a fixed
    order. Prefixes come from the local database, and the escape below is the
    same one SQLite itself uses.
    """
    prefixes = _prefixes(conn, rule)
    if not prefixes:
        return "0"
    parts = []
    for prefix in prefixes:
        escaped = prefix.replace("'", "''").replace("\\", "\\\\") \
                        .replace("%", "\\%").replace("_", "\\_")
        parts.append(f"{alias}.uri LIKE '{escaped}%' ESCAPE '\\'")
    return "(" + " OR ".join(parts) + ")"


def is_ruled(conn: sqlite3.Connection, uri: str | None, rule: str) -> bool:
    """Whether a single path falls under a rule. The Python half of above."""
    if not uri:
        return False
    return any(uri.startswith(prefix) for prefix in _prefixes(conn, rule))


def boosted_items(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, float]:
    """Boost weight per item, for those that have one."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT i.id, MAX(r.weight) AS weight FROM items i "
        f"JOIN path_rules r ON r.rule = 'boost' AND i.uri LIKE r.prefix || '%' "
        f"WHERE i.id IN ({placeholders}) GROUP BY i.id", item_ids).fetchall()
    return {int(r["id"]): float(r["weight"]) for r in rows}


# --------------------------------------------------------------- feedback

def record_feedback(conn: sqlite3.Connection, query: str, item_id: int,
                    signal: str) -> None:
    """Remember that this document was wanted (or not) for these words."""
    if signal not in ("up", "down", "open"):
        raise ValueError(f"unknown signal: {signal}")
    normalized = normalize_query(query)
    if not normalized:
        return
    with transaction(conn) as c:
        # An explicit thumbs-up and a thumbs-down are opposites, so taking one
        # clears the other. Otherwise a change of mind leaves both standing
        # and they quietly cancel.
        if signal in ("up", "down"):
            opposite = "down" if signal == "up" else "up"
            c.execute("DELETE FROM query_feedback WHERE query_norm = ? "
                      "AND item_id = ? AND signal = ?",
                      (normalized, item_id, opposite))
        c.execute(
            "INSERT INTO query_feedback (query_norm, item_id, signal, count, "
            "  updated_at) VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(query_norm, item_id, signal) DO UPDATE SET "
            "  count = count + 1, updated_at = excluded.updated_at",
            (normalized, item_id, signal, utcnow()))


def clear_feedback(conn: sqlite3.Connection, query: str,
                   item_id: int | None = None) -> int:
    normalized = normalize_query(query)
    with transaction(conn) as c:
        if item_id is None:
            cursor = c.execute(
                "DELETE FROM query_feedback WHERE query_norm = ?", (normalized,))
        else:
            cursor = c.execute(
                "DELETE FROM query_feedback WHERE query_norm = ? AND item_id = ?",
                (normalized, item_id))
    return cursor.rowcount


def feedback_for(conn: sqlite3.Connection, query: str) -> dict[int, float]:
    """Per-item adjustment in [-1, 1] for this query, before weighting.

    An explicit thumbs-up or thumbs-down counts for much more than an open:
    you meant it, whereas opening a result can just as easily mean it looked
    promising and was not.
    """
    normalized = normalize_query(query)
    if not normalized:
        return {}
    rows = conn.execute(
        "SELECT item_id, signal, count FROM query_feedback WHERE query_norm = ?",
        (normalized,)).fetchall()

    scores: dict[int, float] = {}
    for row in rows:
        weight = {"up": 1.0, "down": -1.0, "open": 0.3}[row["signal"]]
        # Saturating, so repeated clicks have diminishing effect.
        magnitude = min(float(row["count"]) / FEEDBACK_SATURATION, 1.0)
        scores[int(row["item_id"])] = scores.get(int(row["item_id"]), 0.0) \
            + weight * magnitude
    return {item: max(-1.0, min(1.0, value)) for item, value in scores.items()}


def list_feedback(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Everything search has been told, newest first, for review and undo."""
    rows = conn.execute(
        "SELECT f.query_norm, f.item_id, f.signal, f.count, f.updated_at, "
        "       i.title, i.uri "
        "FROM query_feedback f JOIN items i ON i.id = f.item_id "
        "WHERE f.signal IN ('up', 'down') "
        "ORDER BY f.updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]
