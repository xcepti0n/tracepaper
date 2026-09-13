"""Unified search: one query, every layer (FR-4, FR-5, FR-12).

The user should not have to know whether "Costco" is an entity, a field value,
or a word in a passage before typing it. One box searches all of them, and the
results are grouped by what kind of answer they are:

    a direct value   ->  "passport expiry"  -> 2029-06-11, with its citation
    things that happened -> "Alaska"        -> flights, dated, with evidence
    matching documents   -> anything else   -> ranked passages

Which layers respond is decided by rules over the query and over the vocabulary
the corpus actually produced -- never by a model. Layers that have nothing to
say return nothing rather than padding the page.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from .. import entities as entity_module
from .. import events as event_module
from .fields import FieldQuery, FieldValue
from .search import RankedHit, SearchEngine

# Words that carry no search intent. Stripped when matching a query against
# field names, so "what is my passport expiry" still finds `expiry_date`.
_STOPWORDS = {
    "what", "whats", "what's", "when", "where", "who", "which", "how", "why",
    "is", "was", "were", "are", "am", "be", "been", "do", "did", "does",
    "my", "mine", "me", "i", "the", "a", "an", "of", "for", "to", "in", "on",
    "at", "from", "with", "and", "or", "much", "many", "last", "first",
    "show", "find", "get", "tell", "give",
}

_TOKEN = re.compile(r"[\w][\w'-]*", re.UNICODE)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")

# A query asking for a value, rather than naming a topic. Kept literal and
# deterministic -- this decides whether the answer banner appears at all.
_INTERROGATIVE = re.compile(
    r"\b(what|whats|what's|when|where|who|which|how much|how many|"
    r"my|expiry|expires|due|total|amount|balance|number)\b"
)

# Values that are syntax rather than facts. The generic `label: value`
# extractor cannot tell a form field from a line of JSON, so source files
# vendored into the corpus produce fields whose values are code fragments.
_CODE_ISH = re.compile(r"""[{}\[\]<>]|::|=>|^\s*(?:def|class|import|return)\b""")


def _is_answerable(value: FieldValue) -> bool:
    """True when a stored value is fit to show as *the* answer.

    A typed value -- a date or a number -- has already proved itself by
    parsing. Free text has not, so it must at least not look like code.
    """
    if value.value_date is not None or value.value_num is not None:
        return True
    text = (value.value_text or "").strip()
    if not text or len(text) > 120:
        return False
    return not _CODE_ISH.search(text)



@dataclass
class UnifiedResult:
    query: str
    answer: FieldValue | None = None
    answer_key: str | None = None
    alternatives: list[FieldValue] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    hits: list[RankedHit] = field(default_factory=list)
    total_hits: int = 0
    matched_keys: list[str] = field(default_factory=list)
    photos: list[dict] = field(default_factory=list)
    photo_filters: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.answer or self.events or self.entities
                    or self.hits or self.photos)


class UnifiedSearch:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.fields = FieldQuery(conn)
        self.search = SearchEngine(conn)

    def query(self, text: str, *, limit: int = 20,
              semantic: bool = True) -> UnifiedResult:
        result = UnifiedResult(query=text)
        if not text.strip():
            return result

        tokens = [t.lower() for t in _TOKEN.findall(text)]
        meaningful = [t for t in tokens if t not in _STOPWORDS]

        constraints = self._constraints(text)
        self._answer(text, meaningful, constraints, result)
        self._events(text, meaningful, result)
        self._entities(meaningful, result)
        self._photos(meaningful, result)

        response = self.search.search(text, limit=limit, semantic=semantic)
        result.hits = response.hits
        result.total_hits = response.total
        return result

    # ------------------------------------------------------------ internals

    def _constraints(self, text: str) -> dict[str, object]:
        """A bare year in the query constrains the answer to that year.

        "salary 2023" means the 2023 form, and because fields are bound per
        record, that can never read another year's number.
        """
        match = _YEAR.search(text)
        if not match:
            return {}
        year = int(match.group(1))
        for key in ("tax_year", "year"):
            if self.conn.execute(
                "SELECT 1 FROM record_fields WHERE key = ? AND value_num = ? LIMIT 1",
                (key, float(year)),
            ).fetchone():
                return {key: year}
        return {}

    def _answer(self, text: str, tokens: list[str],
                constraints: dict[str, object], result: UnifiedResult) -> None:
        """Tier 1: a stored value, when the query names a field we know.

        The direct answer is the loudest thing on the page, so it has to earn
        the slot. "3d printer" is a topic, not a question about a value, and
        answering it with `{"type": "integer"},` -- scraped out of a JSON blob
        in a vendored Python file, whose key happened to normalise to
        `printer` -- is worse than showing nothing.

        Two gates, both deterministic. The query must *ask* for a value, and
        the value must look like one.
        """
        if not self._asks_for_a_value(text, tokens, constraints):
            return

        for key in self._matching_keys(tokens):
            answer = self.fields.get(key, where=constraints, limit=6)
            values = [v for v in answer.values if _is_answerable(v)]
            if not values:
                continue
            result.answer = values[0]
            result.answer_key = answer.key
            result.matched_keys.append(answer.key)
            if any(v.value != values[0].value for v in values[1:]):
                # Disagreement is surfaced, never resolved silently.
                result.alternatives = values[1:]
            return

    def _asks_for_a_value(self, text: str, tokens: list[str],
                          constraints: dict[str, object]) -> bool:
        """True when the query reads as a question about a stored value.

        Bare topic words ("3d printer", "tax returns") are browsing: the
        ranked documents below are the right answer and the value banner is
        noise. A question word, a year constraint, or naming a field outright
        ("passport expiry") all signal the opposite.
        """
        lowered = text.lower()
        if _INTERROGATIVE.search(lowered):
            return True
        if constraints:                      # "salary 2023" names a year
            return True
        # The query is (close to) a field name itself, rather than merely
        # overlapping one: "passport expiry" qualifies, "printer" does not,
        # because a single generic token overlaps far too much.
        if len(tokens) >= 2:
            joined = "_".join(tokens)
            for key, _ in self.fields.list_keys(limit=500):
                if key == joined or set(key.split("_")) == set(tokens):
                    return True
        return False

    def _matching_keys(self, tokens: list[str]) -> list[str]:
        """Field names the query plausibly refers to, best match first.

        Matched against the vocabulary the documents actually produced, so this
        stays a lookup rather than a guess.
        """
        if not tokens:
            return []

        known = [key for key, _ in self.fields.list_keys(limit=500)]
        joined = "_".join(tokens)
        scored: list[tuple[int, int, str]] = []

        for key in known:
            parts = set(key.split("_"))
            overlap = len(parts & set(tokens))
            if key == joined:
                scored.append((100, -len(key), key))
            elif joined in key or key in joined:
                scored.append((50 + overlap, -len(key), key))
            elif overlap:
                # Every word of the key must be present, so "salary" does not
                # match "salary_advance_repayment".
                complete = 10 if parts <= set(tokens) else 0
                scored.append((overlap + complete, -len(key), key))

        scored.sort(reverse=True)
        return [key for _, _, key in scored[:4]]

    def _events(self, text: str, tokens: list[str],
                result: UnifiedResult) -> None:
        rows: list[sqlite3.Row] = []

        # An entity named in the query is the strongest event signal.
        for entity in self._matching_entities(tokens):
            rows.extend(event_module.query(
                self.conn, entity=entity["canonical_name"], limit=10))

        # Otherwise an event type named directly ("flights", "purchases").
        if not rows:
            types = {r["event_type"] for r in self.conn.execute(
                "SELECT DISTINCT event_type FROM events")}
            for token in tokens:
                singular = token.rstrip("s")
                for event_type in types:
                    if event_type in (token, singular):
                        rows.extend(event_module.query(
                            self.conn, event_type=event_type, limit=10))

        seen: set[int] = set()
        for row in rows:
            event_id = int(row["id"])
            if event_id in seen:
                continue
            seen.add(event_id)
            result.events.append({
                "id": event_id,
                "date": row["occurred_on"],
                "precision": row["occurred_precision"],
                "title": row["title"] or row["event_type"],
                "entities": [
                    {"name": e["canonical_name"], "role": e["role"]}
                    for e in event_module.event_entities(self.conn, event_id)],
                "evidence": [
                    {"item_id": int(e["item_id"]), "title": e["title"],
                     "uri": e["uri"]}
                    for e in event_module.evidence(self.conn, event_id)],
            })

    def _matching_entities(self, tokens: list[str]) -> list[dict]:
        found: list[dict] = []
        seen: set[int] = set()
        # Try the longest phrases first: "Alaska Airlines" before "Alaska".
        for length in range(min(4, len(tokens)), 0, -1):
            for start in range(len(tokens) - length + 1):
                phrase = " ".join(tokens[start:start + length])
                if len(phrase) < 3:
                    continue
                for row in entity_module.find(self.conn, phrase):
                    if int(row["id"]) in seen:
                        continue
                    seen.add(int(row["id"]))
                    found.append({
                        "id": int(row["id"]),
                        "canonical_name": row["canonical_name"],
                        "entity_type": row["entity_type"],
                    })
        return found

    def _entities(self, tokens: list[str], result: UnifiedResult) -> None:
        for entity in self._matching_entities(tokens):
            entity["aliases"] = entity_module.aliases(self.conn, entity["id"])
            result.entities.append(entity)


    def _photos(self, tokens: list[str], result: UnifiedResult) -> None:
        """Photos matching tags in the query -- "photos from Goa in 2019".

        Every filter must match the same photo, so a year and a place narrow
        together rather than returning the union.
        """
        if not tokens:
            return

        # Tag values the query actually mentions, as (namespace, value).
        matched: list[tuple[str, str]] = []
        for token in tokens:
            if token in ("photo", "photos", "picture", "pictures", "image",
                         "images"):
                continue
            rows = self.conn.execute(
                "SELECT DISTINCT namespace, value FROM tags "
                "WHERE (lower(value) = ? OR lower(value) = ? "
                "       OR (namespace IN ('year','month') AND value = ?)) "
                "AND value != '_none' LIMIT 4",
                (token, token.rstrip("s"), token),
            ).fetchall()
            for row in rows:
                matched.append((row["namespace"], row["value"]))

        if not matched:
            return

        sql = ["SELECT DISTINCT i.id, i.title, i.uri, i.created_at",
               "FROM items i WHERE i.deleted_at IS NULL AND i.kind = 'photo'"]
        params: list[object] = []
        for namespace, value in matched:
            sql.append("AND EXISTS (SELECT 1 FROM tags t WHERE t.item_id = i.id "
                       "AND t.namespace = ? AND t.value = ?)")
            params.extend([namespace, value])
        sql.append("ORDER BY i.created_at DESC, i.id LIMIT 60")

        rows = self.conn.execute(" ".join(sql), params).fetchall()
        result.photo_filters = [f"{ns}={value}" for ns, value in matched]
        result.photos = [
            {"item_id": int(r["id"]), "title": r["title"], "uri": r["uri"],
             "date": r["created_at"],
             "tags": [f'{t["namespace"]}={t["value"]}' for t in self.conn.execute(
                 "SELECT namespace, value FROM tags WHERE item_id = ? "
                 "ORDER BY namespace LIMIT 8", (r["id"],))]}
            for r in rows
        ]
