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

from . import modes
from .. import rules

# Fixed ranking constants. Every tunable lives here so ranking stays auditable
# and reproducible -- never inline magic numbers (FR-13).
BM25_WEIGHT_TEXT = 1.0
BM25_WEIGHT_TITLE = 2.0      # a filename match is a strong signal
BOOST_TITLE_MATCH = 0.5
BOOST_RECENCY_MAX = 0.3
SNIPPET_CHARS = 240

# A document is one result, however many of its passages match. Ranking still
# happens per passage -- that is what BM25 and the vectors score -- so the
# passage pass has to look deeper than `limit` before collapsing, or a single
# long manual fills the page and hides everything else.
#
# 8x covers the common case. It is not a guarantee: a 400-page manual can own
# every passage in the window on its own, and then one deeper pass runs (see
# GROUP_RETRY_FACTOR) rather than returning a two-row page.
GROUP_OVERFETCH = 8

# The retry when the first window collapsed into too few documents. One
# retry, not a loop: the second pass is wide enough that a still-short page
# means the corpus really has that few matching documents.
GROUP_RETRY_FACTOR = 12
MAX_GROUP_SCAN = 5000        # never scan the whole index to fill one page
MAX_PASSAGES_PER_DOC = 5     # how many extra pages to keep for the expander

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
class PassageMatch:
    """One more matching passage inside an already-listed document."""
    passage_id: int
    page: int | None
    snippet: str
    score: float


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
    # Further passages from the SAME document, best first. A document appears
    # once in the results; this is where its other matching pages go.
    more: list["PassageMatch"] = field(default_factory=list)

    @property
    def passage_count(self) -> int:
        return 1 + len(self.more)

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
               kind: str | None = None, semantic: bool = True,
               code: str = "exclude") -> SearchResponse:
        """Keyword and (when available) vector search, fused by RRF.

        Semantic search is additive: with no embeddings or no model installed,
        this degrades to pure BM25 and still works (NFR-9).

        `code` is one of "exclude" (the default), "include" or "only".
        Source and config files are a large part of a developer's NAS and
        almost never the answer to a question about their documents, so they
        are filtered out unless asked for. They stay in the index either way.
        """
        fts_query = to_fts_query(query)
        # Rank passages deeply, then collapse to documents: `limit` counts
        # documents, so the passage pass must over-fetch to have anything left
        # after a long document's pages are folded into one result.
        # Rank from the top every time and slice documents at the end.
        # Passing `offset` down to the passage query would page over passages,
        # which is a different unit than the one the results are in.
        deep = (limit + offset) * GROUP_OVERFETCH
        keyword_hits = self._keyword_search(fts_query, deep, 0, kind, query,
                                            code) if fts_query else []

        vector_ranks: dict[int, int] = {}
        if semantic:
            vector_ranks = self._vector_ranks(query, deep, 0, kind,
                                              code)

        if not keyword_hits and not vector_ranks:
            return SearchResponse(query=query, hits=[], total=0, fts_query=fts_query)

        if not vector_ranks:
            self._apply_human_signals(keyword_hits, query)
            keyword_hits.sort(key=lambda h: (-h.score, h.passage_id))
            hits = _group_by_document(keyword_hits, limit, offset)
            total = self._count(fts_query, kind, code) if fts_query else 0
            # One long document can own the whole window, leaving a page with
            # two rows on it while other documents wait just past the edge.
            if len(hits) < limit and offset + len(hits) < total:
                wider = min((limit + offset) * GROUP_OVERFETCH
                            * GROUP_RETRY_FACTOR, MAX_GROUP_SCAN)
                if wider > deep:
                    keyword_hits = self._keyword_search(
                        fts_query, wider, 0, kind, query, code)
                    self._apply_human_signals(keyword_hits, query)
                    keyword_hits.sort(key=lambda h: (-h.score, h.passage_id))
                    hits = _group_by_document(keyword_hits, limit, offset)
            return SearchResponse(query=query, hits=hits, total=total,
                                  fts_query=fts_query)

        # Fetch wide enough to know whether a further page exists, then page
        # the grouped documents. `_count` alone cannot answer that here: it
        # counts FTS matches, and FTS ANDs its terms, so "3d printer" counted
        # 3 documents while fusion returned 20 -- the other 17 came from
        # vectors. `total` then equalled the page size and the Next link
        # never appeared.
        wanted = limit + offset + 1          # +1: is there anything after us?
        fused = self._fuse_all(keyword_hits, vector_ranks, query, kind, code)
        if len(fused) < wanted:
            wider = min(wanted * GROUP_OVERFETCH * GROUP_RETRY_FACTOR,
                        MAX_GROUP_SCAN)
            if wider > deep:
                keyword_hits = self._keyword_search(
                    fts_query, wider, 0, kind, query, code) \
                    if fts_query else []
                vector_ranks = self._vector_ranks(query, wider, 0, kind, code)
                fused = self._fuse_all(keyword_hits, vector_ranks, query,
                                       kind, code)

        self._apply_human_signals(fused, query)
        fused.sort(key=lambda h: (-h.score, h.passage_id))
        hits = fused[offset:offset + limit]
        # Everything grouped is known to match; the FTS count is a floor that
        # misses vector-only documents, so take whichever is larger.
        total = max(self._count(fts_query, kind, code) if fts_query else 0,
                    len(fused))
        _rescale(hits)
        return SearchResponse(query=query, hits=hits, total=total,
                              fts_query=fts_query)

    def _keyword_search(self, fts_query: str, limit: int, offset: int,
                        kind: str | None, raw_query: str,
                        code: str = "exclude") -> list[RankedHit]:

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
        sql += _code_clause(code, self.conn)

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
                      kind: str | None,
                      code: str = "exclude") -> dict[int, int]:
        """Passage id to its rank in vector search (1-based)."""
        from .. import embed

        if not embed.available():
            return {}
        try:
            scored = embed.search(self.conn, query, limit=(limit + offset) * 3,
                                  kind=kind, code=code)
        except Exception:
            return {}
        return {pid: rank for rank, (pid, score) in enumerate(scored, start=1)
                if score >= MIN_VECTOR_SIMILARITY}

    def _fuse_all(self, keyword_hits: list[RankedHit],
                  vector_ranks: dict[int, int], query: str, kind: str | None,
                  code: str = "exclude") -> list[RankedHit]:
        """Reciprocal Rank Fusion over the two signals, grouped, unsliced.

        RRF combines ranks rather than scores, so BM25 and cosine similarity --
        which are not on comparable scales -- can be merged without tuning
        either one.

        Returns every grouped document it found. The caller slices the page
        out of that, because how many documents exist is the only way to know
        whether there is a page after this one.
        """
        keyword_ranks = {hit.passage_id: rank
                         for rank, hit in enumerate(keyword_hits, start=1)}
        by_id = {hit.passage_id: hit for hit in keyword_hits}

        # Vector-only hits still need their row loaded to be displayable.
        missing = [pid for pid in vector_ranks if pid not in by_id]
        for hit in self._load_hits(missing, query, kind, code):
            by_id[hit.passage_id] = hit

        fused: list[RankedHit] = []
        for passage_id, hit in by_id.items():
            # Start clean: the keyword pass's raw bm25 is on a different scale
            # and does NOT contribute to the fused score. Carrying it over made
            # the explain panel show a large number next to a tiny score, which
            # reads as a bug. Its rank is what matters here, via rrf_bm25.
            signals: dict[str, float] = {}
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
        # A very large limit, because the slice happens in the caller.
        return _group_by_document(fused, len(fused), 0)

    def _apply_human_signals(self, hits: list[RankedHit], query: str) -> None:
        """Fold in your folder boosts and what you taught this query.

        Applied to hits that ALREADY matched, so neither signal can pull an
        irrelevant document into the results -- they only reorder what the
        query found. Both land in `signals`, so a moved result still explains
        itself.
        """
        if not hits:
            return
        item_ids = list({hit.item_id for hit in hits})
        boosts = rules.boosted_items(self.conn, item_ids)
        feedback = rules.feedback_for(self.conn, query)
        if not boosts and not feedback:
            return

        # Scale to whatever range this pass scored in: RRF sits near 0.02,
        # raw BM25 in the tens. A fixed increment would be invisible in one
        # and overwhelming in the other.
        scale = max((hit.score for hit in hits), default=1.0) or 1.0

        for hit in hits:
            weight = boosts.get(hit.item_id)
            if weight:
                amount = rules.BOOST_WEIGHT * weight * scale
                hit.signals["your_boost"] = amount
                hit.score += amount
            adjustment = feedback.get(hit.item_id)
            if adjustment:
                amount = rules.FEEDBACK_WEIGHT * adjustment * scale
                hit.signals["your_feedback"] = amount
                hit.score += amount

    def _load_hits(self, passage_ids: list[int], query: str,
                   kind: str | None,
                   code: str = "exclude") -> list[RankedHit]:
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
        sql += _code_clause(code, self.conn)

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

    def _count(self, fts_query: str, kind: str | None,
               code: str = "exclude") -> int:
        """How many DOCUMENTS match -- the unit the results are now in.

        Counting passages here meant "8 results" printed above a single row,
        because one guide matched on eight pages.
        """
        sql = ("SELECT COUNT(DISTINCT p.item_id) AS n FROM passages_fts "
               "JOIN passages p ON p.id = passages_fts.rowid "
               "JOIN items i ON i.id = p.item_id "
               "WHERE passages_fts MATCH ? AND i.deleted_at IS NULL")
        params: list[object] = [fts_query]
        if kind:
            sql += " AND i.kind = ?"
            params.append(kind)
        sql += _code_clause(code, self.conn)
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


def _rescale(hits: list[RankedHit]) -> None:
    """Make RRF scores readable, in place.

    Fusion produces values around 0.01-0.03, which all round to 0.00 in any
    display. Rescale so the best hit on the page is 1.0 and the rest are
    relative to it. Order is untouched, and the raw contributions stay in
    `signals` for auditing.
    """
    if not hits or hits[0].score <= 0:
        return
    top = hits[0].score
    for hit in hits:
        hit.signals["_raw_rrf"] = hit.score
        hit.score = hit.score / top


def _code_clause(code: str, conn: sqlite3.Connection | None = None) -> str:
    """The WHERE fragment selecting code, documents, or both.

    Applied in SQL rather than in Python so it takes effect before LIMIT --
    filtering afterwards returns a nearly empty page whenever the top-ranked
    passages were the ones being filtered out.

    Your own folder rules are part of this, and they win. A folder you marked
    as code is code even if nothing about its filenames says so, and a folder
    you marked as hidden never appears in any mode.
    """
    marked_code = rules.sql_clause(conn, "code") if conn is not None else "0"
    hidden = f" AND NOT {rules.sql_clause(conn, 'hide')}" if conn is not None else ""

    if code == "include":
        return hidden
    if code == "only":
        return f" AND ({modes.sql_only_code('i')} OR {marked_code}){hidden}"
    # Exclude: drop anything the classifier calls code, plus anything you did.
    return f" AND {modes.sql_filter('i')} AND NOT {marked_code}{hidden}"


def _group_by_document(hits: list[RankedHit], limit: int,
                       offset: int = 0) -> list[RankedHit]:
    """Collapse passage hits into one result per document.

    A 40-page printer manual that mentions the query on every page was
    returning 20 results that were all the same PDF, pushing every other
    document off the page. The document is what the user is looking for; the
    pages are how they navigate inside it once they open it.

    The document takes the score and position of its BEST passage, so ranking
    is unchanged -- this only removes the repeats beneath it. Input order is
    assumed to be the final ranking already, which makes the first passage
    seen for a document its best one.

    Deterministic throughout (NFR-2): dicts preserve insertion order, so equal
    scores keep the passage_id tie-break the caller already applied.
    """
    by_item: dict[int, RankedHit] = {}
    for hit in hits:
        leader = by_item.get(hit.item_id)
        if leader is None:
            # `more` is per-result state; a fresh list avoids aliasing the
            # one a caller may have handed in.
            hit.more = []
            by_item[hit.item_id] = hit
            continue
        if len(leader.more) < MAX_PASSAGES_PER_DOC:
            leader.more.append(PassageMatch(
                passage_id=hit.passage_id, page=hit.page,
                snippet=hit.snippet, score=hit.score))
    # Slice DOCUMENTS, not passages. Paging on the passage offset put page 2
    # in the middle of a long manual's run, so it repeated documents page 1
    # had already shown and returned short pages.
    return list(by_item.values())[offset:offset + limit]


def _flatten(text: str) -> str:
    """Collapse whitespace so a snippet occupies exactly one output line."""
    return " ".join(text.split())
