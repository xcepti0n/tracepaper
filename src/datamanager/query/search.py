"""Deterministic search (FR-13, NFR-1, NFR-2).

No model, no prompt, no network call. Ranking is BM25 plus fixed boosts, and
ordering is made total by an explicit item_id tie-break so identical inputs
produce byte-identical output.

M1 is keyword-only. M5 adds vector search as a second signal fused by RRF; the
fusion seam is `RankedHit.signals`, already populated here.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Fixed ranking constants. Every tunable lives here so ranking stays auditable
# and reproducible -- never inline magic numbers (FR-13).
BM25_WEIGHT_TEXT = 1.0
BM25_WEIGHT_TITLE = 2.0      # a filename match is a strong signal
BOOST_TITLE_MATCH = 0.5
BOOST_RECENCY_MAX = 0.3
SNIPPET_CHARS = 240


@dataclass
class RankedHit:
    item_id: int
    passage_id: int
    uri: str | None
    title: str
    page: int | None
    snippet: str
    score: float
    signals: dict[str, float] = field(default_factory=dict)

    def explain(self) -> str:
        parts = [f"{name}={value:+.4f}" for name, value in sorted(self.signals.items())]
        return f"score={self.score:.4f} [{' '.join(parts)}]"


@dataclass
class SearchResponse:
    query: str
    hits: list[RankedHit]
    total: int
    fts_query: str

    def __len__(self) -> int:
        return len(self.hits)


# FTS5 treats these as syntax; a user typing them means them literally.
_FTS_SPECIAL = re.compile(r'["\'()*:^-]')
_TOKEN = re.compile(r"[\w][\w\'-]*", re.UNICODE)


def to_fts_query(query: str) -> str:
    """Convert user input into a safe FTS5 MATCH expression.

    Every token is quoted, so punctuation and FTS operators in user input are
    literal rather than syntax. Bare `AND`/`OR`/`NOT` are honoured as operators
    since users type them intentionally.
    """
    tokens = _TOKEN.findall(query)
    if not tokens:
        return ""

    parts: list[str] = []
    for token in tokens:
        upper = token.upper()
        if upper in {"AND", "OR", "NOT"} and parts:
            parts.append(upper)
        else:
            cleaned = _FTS_SPECIAL.sub("", token)
            if cleaned:
                parts.append(f'"{cleaned}"')

    # Strip a trailing operator, which would be a syntax error.
    while parts and parts[-1] in {"AND", "OR", "NOT"}:
        parts.pop()
    return " ".join(parts)


class SearchEngine:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def search(self, query: str, *, limit: int = 20, offset: int = 0,
               kind: str | None = None) -> SearchResponse:
        fts_query = to_fts_query(query)
        if not fts_query:
            return SearchResponse(query=query, hits=[], total=0, fts_query="")

        sql = """
            SELECT
                p.id            AS passage_id,
                p.item_id       AS item_id,
                p.page          AS page,
                p.text          AS text,
                i.uri           AS uri,
                i.title         AS title,
                i.modified_at   AS modified_at,
                bm25(passages_fts, ?, ?) AS bm25_score
            FROM passages_fts
            JOIN passages p ON p.id = passages_fts.rowid
            JOIN items    i ON i.id = p.item_id
            WHERE passages_fts MATCH ?
              AND i.deleted_at IS NULL
        """
        params: list[object] = [BM25_WEIGHT_TEXT, BM25_WEIGHT_TITLE, fts_query]
        if kind:
            sql += " AND i.kind = ?"
            params.append(kind)

        # Deterministic ordering: score first, then passage_id as a total
        # tie-break so equal scores never reorder between runs (NFR-2).
        sql += " ORDER BY bm25_score, p.id LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
                return SearchResponse(query=query, hits=[], total=0, fts_query=fts_query)
            raise

        newest, oldest = self._modified_range()
        terms = {t.lower() for t in _TOKEN.findall(query)}

        hits: list[RankedHit] = []
        for row in rows:
            # bm25() returns negative values, better matches more negative.
            base = -float(row["bm25_score"])
            signals = {"bm25": base}

            title = row["title"] or ""
            if terms and any(term in title.lower() for term in terms):
                signals["title_match"] = BOOST_TITLE_MATCH

            recency = self._recency_boost(row["modified_at"], newest, oldest)
            if recency:
                signals["recency"] = recency

            hits.append(RankedHit(
                item_id=int(row["item_id"]),
                passage_id=int(row["passage_id"]),
                uri=row["uri"],
                title=title or (Path(row["uri"]).name if row["uri"] else ""),
                page=row["page"],
                snippet=self._snippet(row["text"] or "", terms),
                score=sum(signals.values()),
                signals=signals,
            ))

        # Re-sort after boosts, with the same total tie-break.
        hits.sort(key=lambda h: (-h.score, h.passage_id))

        total = self._count(fts_query, kind)
        return SearchResponse(query=query, hits=hits, total=total, fts_query=fts_query)

    def _count(self, fts_query: str, kind: str | None) -> int:
        sql = ("SELECT COUNT(*) AS n FROM passages_fts "
               "JOIN passages p ON p.id = passages_fts.rowid "
               "JOIN items i ON i.id = p.item_id "
               "WHERE passages_fts MATCH ? AND i.deleted_at IS NULL")
        params: list[object] = [fts_query]
        if kind:
            sql += " AND i.kind = ?"
            params.append(kind)
        try:
            row = self.conn.execute(sql, params).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.OperationalError:
            return 0

    def _modified_range(self) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            "SELECT MAX(modified_at) AS newest, MIN(modified_at) AS oldest "
            "FROM items WHERE deleted_at IS NULL AND modified_at IS NOT NULL"
        ).fetchone()
        if not row:
            return None, None
        return row["newest"], row["oldest"]

    @staticmethod
    def _recency_boost(modified_at: str | None, newest: str | None,
                       oldest: str | None) -> float:
        """Small, bounded, deterministic boost for recent documents.

        Derived from the corpus's own timestamp range rather than wall-clock
        time, so results do not drift between runs (NFR-2).

        Returns zero when the corpus spans less than a day: files copied onto
        the NAS in one operation share an mtime, and a boost every document
        receives equally is not a ranking signal -- it only inflates scores and
        makes `--explain` misleading.
        """
        if not modified_at or not newest or not oldest:
            return 0.0
        from datetime import datetime
        try:
            then = datetime.fromisoformat(modified_at)
            now = datetime.fromisoformat(newest)
            first = datetime.fromisoformat(oldest)
        except ValueError:
            return 0.0

        span_days = (now - first).total_seconds() / 86400.0
        if span_days < 1.0:
            return 0.0

        age_days = (now - then).total_seconds() / 86400.0
        if age_days <= 0:
            return BOOST_RECENCY_MAX
        # Scaled by the corpus's own span, so the boost means "recent relative
        # to this collection" rather than to an arbitrary constant.
        return round(BOOST_RECENCY_MAX * (1.0 - min(age_days / span_days, 1.0)), 6)

    @staticmethod
    def _snippet(text: str, terms: set[str]) -> str:
        """Window centred on the first matching term."""
        if not text:
            return ""
        lowered = text.lower()
        best = -1
        for term in terms:
            pos = lowered.find(term)
            if pos != -1 and (best == -1 or pos < best):
                best = pos
        if best == -1:
            return _flatten(text[:SNIPPET_CHARS]) + (
                "…" if len(text) > SNIPPET_CHARS else "")

        start = max(0, best - SNIPPET_CHARS // 3)
        end = min(len(text), start + SNIPPET_CHARS)
        snippet = _flatten(text[start:end])
        return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def _flatten(text: str) -> str:
    """Collapse whitespace so a snippet occupies exactly one output line."""
    return " ".join(text.split())
