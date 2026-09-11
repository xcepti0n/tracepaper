"""Tier 1: direct answers from bound records (FR-3, FR-4, FR-8).

A value written in a document is returned as a value, with its citation --
never as a list of files to open. Entirely deterministic: SQL over records,
no model, no prompt.

The safety property comes from the schema, not from this code: fields hang off
a `record_id`, so filtering on one key and reading another always stays inside
one document's bound record. "The 2023 gross salary" cannot pair 2023 with a
different year's number, because the pairing was made at ingest.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field as dc_field

# Mirrors extract.records.SOURCE_RANK, expressed for SQL ordering.
_SOURCE_ORDER = (
    "CASE r.source WHEN 'human' THEN 0 WHEN 'template' THEN 1 "
    "WHEN 'pattern' THEN 2 ELSE 3 END"
)


@dataclass
class FieldValue:
    """One answer, with everything needed to cite and audit it."""
    key: str
    value_text: str
    value_num: float | None
    value_date: str | None
    unit: str | None
    item_id: int
    item_title: str
    uri: str | None
    page: int | None
    record_id: int
    record_type: str
    source: str
    confidence: float

    @property
    def value(self):
        """The most specific typed form available."""
        if self.value_date is not None:
            return self.value_date
        if self.value_num is not None:
            return self.value_num
        return self.value_text

    def citation(self) -> str:
        where = self.uri or f"item {self.item_id}"
        page = f", p.{self.page}" if self.page else ""
        return f"{self.item_title}{page} ({where})"


@dataclass
class FieldAnswer:
    key: str
    values: list[FieldValue] = dc_field(default_factory=list)
    constraints: dict = dc_field(default_factory=dict)

    @property
    def best(self) -> FieldValue | None:
        return self.values[0] if self.values else None

    @property
    def is_unambiguous(self) -> bool:
        """True when one document answers, or all candidates agree."""
        if len(self.values) <= 1:
            return bool(self.values)
        first = self.values[0].value
        return all(v.value == first for v in self.values)


class FieldQuery:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, key: str, *, where: dict[str, object] | None = None,
            record_type: str | None = None, limit: int = 10) -> FieldAnswer:
        """Look up a field, optionally constrained by sibling fields.

        `where` constrains *within the same record*: get("gross_salary",
        where={"tax_year": 2023}) reads the salary from the record that also
        holds tax_year 2023 -- never from a different document.
        """
        canonical = self._canonical(key)
        sql = [
            "SELECT rf.key, rf.value_text, rf.value_num, rf.value_date, rf.unit,",
            "       rf.confidence AS field_confidence,",
            "       r.id AS record_id, r.record_type, r.source, r.confidence,",
            "       r.page, i.id AS item_id, i.title, i.uri",
            "FROM record_fields rf",
            "JOIN records r ON r.id = rf.record_id",
            "JOIN items   i ON i.id = r.item_id",
            "WHERE rf.key = ? AND i.deleted_at IS NULL",
        ]
        params: list[object] = [canonical]

        if record_type:
            sql.append("AND r.record_type = ?")
            params.append(record_type)

        # Each constraint is an EXISTS against the *same* record_id. This is
        # where the binding pays off.
        for ckey, cvalue in (where or {}).items():
            sql.append(
                "AND EXISTS (SELECT 1 FROM record_fields c "
                "WHERE c.record_id = rf.record_id AND c.key = ? "
                "AND (c.value_num = ? OR c.value_text = ? OR c.value_date = ?))"
            )
            params.extend([self._canonical(ckey), _as_num(cvalue),
                           str(cvalue), str(cvalue)])

        sql.append(f"ORDER BY {_SOURCE_ORDER}, r.confidence DESC, "
                   "rf.confidence DESC, i.id, rf.id")
        sql.append("LIMIT ?")
        params.append(limit)

        rows = self.conn.execute(" ".join(sql), params).fetchall()
        values = [
            FieldValue(
                key=row["key"], value_text=row["value_text"],
                value_num=row["value_num"], value_date=row["value_date"],
                unit=row["unit"], item_id=int(row["item_id"]),
                item_title=row["title"] or "", uri=row["uri"], page=row["page"],
                record_id=int(row["record_id"]), record_type=row["record_type"],
                source=row["source"], confidence=float(row["field_confidence"]),
            )
            for row in rows
        ]
        return FieldAnswer(key=canonical, values=values,
                           constraints=dict(where or {}))

    def aggregate(self, key: str, op: str, *,
                  where: dict[str, object] | None = None,
                  record_type: str | None = None) -> tuple[float | None, list[FieldValue]]:
        """Sum/avg/count/min/max over a field (FR-8).

        Returns the result *and every contributing value*, so the number can be
        audited back to its documents rather than taken on trust.
        """
        op = op.lower()
        if op not in {"sum", "avg", "count", "min", "max"}:
            raise ValueError(f"unsupported aggregate: {op}")

        answer = self.get(key, where=where, record_type=record_type, limit=10_000)
        contributing = self._deduplicate(answer.values)

        if op == "count":
            return float(len(contributing)), contributing

        numbers = [v.value_num for v in contributing if v.value_num is not None]
        if not numbers:
            return None, contributing

        result = {
            "sum": sum(numbers),
            "avg": sum(numbers) / len(numbers),
            "min": min(numbers),
            "max": max(numbers),
        }[op]
        return result, contributing

    def _deduplicate(self, values: list[FieldValue]) -> list[FieldValue]:
        """Collapse restatements of the same fact before aggregating.

        Both failure directions are ordinary on a NAS and both are wrong:

        - The same tax form kept as a scan *and* an export would count its
          salary twice.
        - Two rent receipts for the same amount in different months are two
          real payments and must both count.

        So the identity of a fact is the value **plus its whole sibling
        context** -- every other field in the record it was bound to. Two
        documents restating one form share that context; January and February
        rent do not, because their record carries a differing month.
        """
        by_item: dict[int, FieldValue] = {}
        for value in values:
            by_item.setdefault(value.item_id, value)

        out: list[FieldValue] = []
        contexts: list[dict[str, str]] = []
        for value in by_item.values():
            context = self._record_context(value.record_id)
            if any(self._same_fact(value, context, kept, kept_context)
                   for kept, kept_context in zip(out, contexts)):
                continue
            out.append(value)
            contexts.append(context)
        return out

    def _record_context(self, record_id: int) -> dict[str, str]:
        """Sibling fields of a record, keyed for comparison."""
        rows = self.conn.execute(
            "SELECT key, value_text FROM record_fields WHERE record_id = ?",
            (record_id,)
        ).fetchall()
        return {r["key"]: r["value_text"] for r in rows}

    @staticmethod
    def _same_fact(a: FieldValue, a_context: dict[str, str],
                   b: FieldValue, b_context: dict[str, str]) -> bool:
        """True when two values are one fact restated in two documents.

        Only the keys the two records *share* are compared. One copy of a form
        may extract more fields than another -- a scan yielding an extra box
        does not make it a different form -- so requiring identical context
        would fail to merge exactly the duplicates worth merging.

        Conversely, two records that disagree on any shared key (January vs
        February rent) are distinct facts and both are counted.
        """
        if (a.key, a.value_text, a.value_num, a.value_date) != \
           (b.key, b.value_text, b.value_num, b.value_date):
            return False

        shared = set(a_context) & set(b_context)
        if len(shared) <= 1:          # only the field itself: too little to judge
            return False
        return all(a_context[key] == b_context[key] for key in shared)

    def list_keys(self, prefix: str | None = None, limit: int = 200) -> list[tuple[str, int]]:
        """Discovered vocabulary with usage counts (FR-4).

        The two joins exist only to hide fields belonging to deleted items.
        They are also the whole cost: grouping a million record_fields through
        them takes ~460ms and two temp B-trees, because LIMIT cannot apply
        until the grouping is done. Grouping the table alone is ~47ms, since
        idx_rf_key orders the scan.

        Deletions are rare -- an item is soft-deleted only when it vanishes
        from the NAS -- so check for one first and take the cheap path when
        there are none. The result is identical either way; this is not an
        approximation.
        """
        params: list[object] = []
        deleted = self.conn.execute(
            "SELECT 1 FROM items WHERE deleted_at IS NOT NULL LIMIT 1"
        ).fetchone()

        if deleted:
            sql = ["SELECT rf.key, COUNT(*) AS n FROM record_fields rf",
                   "JOIN records r ON r.id = rf.record_id",
                   "JOIN items i ON i.id = r.item_id",
                   "WHERE i.deleted_at IS NULL"]
            if prefix:
                sql.append("AND rf.key LIKE ?")
                params.append(f"{prefix}%")
        else:
            sql = ["SELECT rf.key, COUNT(*) AS n FROM record_fields rf"]
            if prefix:
                sql.append("WHERE rf.key LIKE ?")
                params.append(f"{prefix}%")

        sql.append("GROUP BY rf.key ORDER BY n DESC, rf.key LIMIT ?")
        params.append(limit)
        return [(r["key"], int(r["n"]))
                for r in self.conn.execute(" ".join(sql), params).fetchall()]

    def list_values(self, key: str, limit: int = 200) -> list[tuple[str, int]]:
        """Distinct values for a key -- "which tax years do I have?" """
        rows = self.conn.execute(
            "SELECT rf.value_text, COUNT(*) AS n FROM record_fields rf "
            "JOIN records r ON r.id = rf.record_id "
            "JOIN items i ON i.id = r.item_id "
            "WHERE rf.key = ? AND i.deleted_at IS NULL "
            "GROUP BY rf.value_text ORDER BY n DESC, rf.value_text LIMIT ?",
            (self._canonical(key), limit),
        ).fetchall()
        return [(r["value_text"], int(r["n"])) for r in rows]

    def _canonical(self, key: str) -> str:
        """Resolve a key through the vocabulary map (FR-4)."""
        from .. import vocabulary
        from ..extract.records import normalize_key
        return vocabulary.resolve(self.conn, normalize_key(key))


def _as_num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
