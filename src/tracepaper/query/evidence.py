"""Tier 2: evidence assembly (requirements §3).

Some questions have no answer written in any document. "What card is best to
pay at Costco?" requires combining card benefit terms with which cards are
held and what was actually spent -- that is reasoning, and it belongs to the
caller's LLM, not to this engine.

What the engine guarantees is the **evidence set**: the same question returns
the same evidence, in the same order, every time. Tracepaper never generates
prose; it hands back records, events and passages with citations.

Entirely deterministic -- no model is loaded here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .. import entities as entity_module
from .. import events as event_module
from .fields import FieldQuery, FieldValue
from .search import SearchEngine


@dataclass
class EvidenceItem:
    kind: str                    # record | event | passage
    title: str
    uri: str | None
    item_id: int
    detail: str
    date: str | None = None
    score: float = 0.0

    def citation(self) -> str:
        return f"{self.title} ({self.uri or f'item {self.item_id}'})"


@dataclass
class EvidenceSet:
    question: str
    entities: list[str] = field(default_factory=list)
    facts: list[FieldValue] = field(default_factory=list)
    events: list[EvidenceItem] = field(default_factory=list)
    passages: list[EvidenceItem] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.facts or self.events or self.passages)

    def as_dict(self) -> dict:
        """Serialisable form, for the MCP tools and the API."""
        return {
            "question": self.question,
            "entities": self.entities,
            "facts": [
                {"key": f.key, "value": f.value, "unit": f.unit,
                 "source": f.source, "citation": f.citation(),
                 "item_id": f.item_id}
                for f in self.facts
            ],
            "events": [
                {"detail": e.detail, "date": e.date, "citation": e.citation(),
                 "item_id": e.item_id}
                for e in self.events
            ],
            "passages": [
                {"text": p.detail, "citation": p.citation(),
                 "item_id": p.item_id, "score": round(p.score, 6)}
                for p in self.passages
            ],
        }


class EvidenceQuery:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.fields = FieldQuery(conn)
        self.search = SearchEngine(conn)

    def gather(self, question: str, *, entity: str | None = None,
               keys: list[str] | None = None, limit: int = 10,
               semantic: bool = True) -> EvidenceSet:
        """Assemble everything relevant to a question.

        Three sources, each deterministic and each ordered by a total sort so
        the set is reproducible (NFR-2):

        - facts    -- structured values, optionally about a named entity
        - events   -- what happened, most recent first
        - passages -- ranked text, for context nothing structured captured
        """
        result = EvidenceSet(question=question)

        if entity:
            found = entity_module.find(self.conn, entity)
            result.entities = [row["canonical_name"] for row in found]

        result.facts = self._facts(keys, entity, limit)
        result.events = self._events(entity, limit)
        result.passages = self._passages(question, limit, semantic)
        return result

    def _facts(self, keys: list[str] | None, entity: str | None,
               limit: int) -> list[FieldValue]:
        collected: list[FieldValue] = []

        for key in keys or []:
            collected.extend(self.fields.get(key, limit=limit).values)

        if entity:
            # Every field of every record that names this entity, so the caller
            # sees the whole context rather than a key it had to guess.
            rows = self.conn.execute(
                "SELECT DISTINCT r.id FROM records r "
                "JOIN record_fields rf ON rf.record_id = r.id "
                "JOIN items i ON i.id = r.item_id "
                "WHERE i.deleted_at IS NULL AND rf.value_text LIKE ? "
                "ORDER BY r.id LIMIT ?",
                (f"%{entity}%", limit),
            ).fetchall()
            for row in rows:
                collected.extend(self._record_values(int(row["id"])))

        # De-duplicate on identity, preserving the order established above.
        seen: set[tuple] = set()
        unique: list[FieldValue] = []
        for value in collected:
            identity = (value.item_id, value.key, value.value_text)
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(value)
        return unique[:limit * 2]

    def _record_values(self, record_id: int) -> list[FieldValue]:
        rows = self.conn.execute(
            "SELECT rf.key, rf.value_text, rf.value_num, rf.value_date, rf.unit, "
            "rf.confidence AS field_confidence, r.id AS record_id, r.record_type, "
            "r.source, r.page, i.id AS item_id, i.title, i.uri "
            "FROM record_fields rf JOIN records r ON r.id = rf.record_id "
            "JOIN items i ON i.id = r.item_id "
            "WHERE r.id = ? ORDER BY rf.key", (record_id,)
        ).fetchall()
        return [
            FieldValue(
                key=r["key"], value_text=r["value_text"], value_num=r["value_num"],
                value_date=r["value_date"], unit=r["unit"], item_id=int(r["item_id"]),
                item_title=r["title"] or "", uri=r["uri"], page=r["page"],
                record_id=int(r["record_id"]), record_type=r["record_type"],
                source=r["source"], confidence=float(r["field_confidence"]),
            )
            for r in rows
        ]

    def _events(self, entity: str | None, limit: int) -> list[EvidenceItem]:
        rows = event_module.query(self.conn, entity=entity, limit=limit)
        out: list[EvidenceItem] = []
        for row in rows:
            evidence = event_module.evidence(self.conn, int(row["id"]))
            if not evidence:
                continue
            first = evidence[0]
            out.append(EvidenceItem(
                kind="event", title=first["title"] or "", uri=first["uri"],
                item_id=int(first["item_id"]),
                detail=row["title"] or row["event_type"],
                date=row["occurred_on"],
            ))
        return out

    def _passages(self, question: str, limit: int,
                  semantic: bool) -> list[EvidenceItem]:
        response = self.search.search(question, limit=limit, semantic=semantic)
        return [
            EvidenceItem(
                kind="passage", title=hit.title, uri=hit.uri, item_id=hit.item_id,
                detail=hit.snippet, score=hit.score,
            )
            for hit in response.hits
        ]
