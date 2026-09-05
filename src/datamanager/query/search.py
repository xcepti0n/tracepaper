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

# Reciprocal Rank Fusion. k=60 is the standard constant; fixed, never learned,
# so ranking stays reproducible (NFR-2, D-004).
RRF_K = 60
RRF_WEIGHT_BM25 = 1.0
RRF_WEIGHT_VECTOR = 0.8      # vectors inform, they never outrank exact matches
# Vectors below this similarity are noise: brute-force search always returns
# *something*, and an unrelated passage at rank 1 of an empty result set would
# otherwise be promoted by fusion.
MIN_VECTOR_SIMILARITY = 0.25


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
               kind: str | None = None, semantic: bool = True) -> SearchResponse:
        """Keyword and (when available) vector search, fused by RRF.

        Semantic search is additive: with no embeddings or no model installed,
        this degrades to pure BM25 and still works (NFR-9).
        """
        fts_query = to_fts_query(query)
        keyword_hits = self._keyword_search(fts_query, limit, offset, kind, query) \
            if fts_query else []

        vector_ranks: dict[int, int] = {}
        if semantic:
            vector_ranks = self._vector_ranks(query, limit, offset, kind)

        if not keyword_hits and not vector_ranks:
            return SearchResponse(query=query, hits=[], total=0, fts_query=fts_query)

        if not vector_ranks:
            total = self._count(fts_query, kind) if fts_query else 0
            return SearchResponse(query=query, hits=keyword_hits, total=total,
                                  fts_query=fts_query)

        hits = self._fuse(keyword_hits, vector_ranks, query, limit, kind)
        total = max(self._count(fts_query, kind) if fts_query else 0, len(hits))
        return SearchResponse(query=query, hits=hits, total=total,
                              fts_query=fts_query)

    def _keyword_search(self, fts_query: str, limit: int, offset: int,
                        kind: str | None, raw_query: str) -> list[RankedHit]:

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
        terms = {t.lower() for t in _TOKEN.findall(raw_query)}

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
        return hits

    def _vector_ranks(self, query: str, limit: int, offset: int,
                      kind: str | None) -> dict[int, int]:
        """Passage id to its rank in vector search (1-based)."""
        from .. import embed

        if not embed.available():
            return {}
        try:
            scored = embed.search(self.conn, query, limit=(limit + offset) * 3,
                                  kind=kind)
        except Exception:
            return {}
        return {pid: rank for rank, (pid, score) in enumerate(scored, start=1)
                if score >= MIN_VECTOR_SIMILARITY}

    def _fuse(self, keyword_hits: list[RankedHit], vector_ranks: dict[int, int],
              query: str, limit: int, kind: str | None) -> list[RankedHit]:
        """Reciprocal Rank Fusion over the two signals.

        RRF combines ranks rather than scores, so BM25 and cosine similarity --
        which are not on comparable scales -- can be merged without tuning
        either one.
        """
        keyword_ranks = {hit.passage_id: rank
                         for rank, hit in enumerate(keyword_hits, start=1)}
        by_id = {hit.passage_id: hit for hit in keyword_hits}

        # Vector-only hits still need their row loaded to be displayable.
        missing = [pid for pid in vector_ranks if pid not in by_id]
        for hit in self._load_hits(missing, query, kind):
            by_id[hit.passage_id] = hit

        fused: list[RankedHit] = []
        for passage_id, hit in by_id.items():
            signals = dict(hit.signals)
            score = 0.0

            if passage_id in keyword_ranks:
                contribution = RRF_WEIGHT_BM25 / (RRF_K + keyword_ranks[passage_id])
                signals["rrf_bm25"] = contribution
                score += contribution
            if passage_id in vector_ranks:
                contribution = RRF_WEIGHT_VECTOR / (RRF_K + vector_ranks[passage_id])
                signals["rrf_vector"] = contribution
                score += contribution

            # Fixed boosts survive fusion, scaled to RRF's much smaller range.
            for name in ("title_match", "recency"):
                if name in hit.signals:
                    scaled = hit.signals[name] / 100.0
                    signals[name] = scaled
                    score += scaled

            fused.append(RankedHit(
                item_id=hit.item_id, passage_id=passage_id, uri=hit.uri,
                title=hit.title, page=hit.page, snippet=hit.snippet,
                score=score, signals=signals,
            ))

        fused.sort(key=lambda h: (-h.score, h.passage_id))
        return fused[:limit]

    def _load_hits(self, passage_ids: list[int], query: str,
                   kind: str | None) -> list[RankedHit]:
        """Build hits for passages found only by vector search."""
        if not passage_ids:
            return []
        placeholders = ",".join("?" * len(passage_ids))
        sql = (f"SELECT p.id AS passage_id, p.item_id, p.page, p.text, "
               f"i.uri, i.title FROM passages p JOIN items i ON i.id = p.item_id "
               f"WHERE p.id IN ({placeholders}) AND i.deleted_at IS NULL")
        params: list[object] = list(passage_ids)
        if kind:
            sql += " AND i.kind = ?"
            params.append(kind)

        terms = {t.lower() for t in _TOKEN.findall(query)}
        return [
            RankedHit(
                item_id=int(row["item_id"]), passage_id=int(row["passage_id"]),
                uri=row["uri"], title=row["title"] or "", page=row["page"],
                snippet=self._snippet(row["text"] or "", terms),
                score=0.0, signals={},
            )
            for row in self.conn.execute(sql, params).fetchall()
        ]

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
