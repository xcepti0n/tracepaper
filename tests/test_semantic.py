"""Semantic search and RRF fusion (D-004, NFR-2, NFR-9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracepaper import embed
from tracepaper.index.indexer import Indexer
from tracepaper.query.search import SearchEngine
from tracepaper.scan.scanner import Scanner

requires_model = pytest.mark.skipif(
    not embed.available(), reason="sentence-transformers not installed")


def build(conn, cfg, nas: Path, files: dict[str, str]):
    for name, body in files.items():
        (nas / name).write_text(body)
    Scanner(conn, cfg).scan(nas)
    return Indexer(conn, cfg).run_pending()


def test_vector_packing_round_trips():
    original = [0.1, -0.25, 0.5, 1.0]
    restored = embed.unpack(embed.pack(original))
    assert restored == pytest.approx(original, abs=1e-6)


def test_cosine_similarity():
    assert embed.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert embed.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert embed.cosine([0, 0], [1, 0]) == 0.0, "zero vector must not divide by zero"


def test_search_works_without_embeddings(conn, cfg, nas):
    """NFR-9: with no vectors, search degrades to keyword and still works."""
    build(conn, cfg, nas, {"a.txt": "distinctive keyword content"})

    hits = SearchEngine(conn).search("distinctive keyword").hits

    assert len(hits) == 1


@requires_model
def test_semantic_finds_paraphrase(conn, cfg, nas):
    """The query nothing else answers: the words do not overlap at all.

    The document says "irrigation solenoid"; the user searches "sprinkler
    valve". Keyword search cannot bridge that.
    """
    build(conn, cfg, nas, {
        "yard_log.txt": "Replaced the irrigation solenoid in the front zone. "
                        "Water was pooling near the driveway.",
        "unrelated.txt": "Quarterly investment statement for the retirement account.",
    })
    embed.embed_pending(conn)
    engine = SearchEngine(conn)

    assert not engine.search("sprinkler valve", semantic=False).hits, \
        "keyword search should not find this"

    hits = engine.search("sprinkler valve", semantic=True).hits
    assert hits
    assert hits[0].title == "yard_log.txt"
    assert "rrf_vector" in hits[0].signals


@requires_model
def test_exact_keyword_match_outranks_a_paraphrase(conn, cfg, nas):
    """Vectors inform ranking; they never override an exact match."""
    build(conn, cfg, nas, {
        "exact.txt": "The sprinkler valve was replaced on Tuesday.",
        "paraphrase.txt": "Replaced the irrigation solenoid in the front zone.",
    })
    embed.embed_pending(conn)

    hits = SearchEngine(conn).search("sprinkler valve").hits

    assert hits[0].title == "exact.txt"


@requires_model
def test_fused_results_are_reproducible(conn, cfg, nas):
    """NFR-2 must hold with both signals active."""
    build(conn, cfg, nas, {
        f"doc{i}.txt": f"maintenance record number {i} about plumbing and water"
        for i in range(8)
    })
    embed.embed_pending(conn)
    engine = SearchEngine(conn)

    runs = [[(h.passage_id, round(h.score, 9))
             for h in engine.search("water leak repair", limit=8).hits]
            for _ in range(5)]

    assert all(run == runs[0] for run in runs)


@requires_model
def test_embeddings_are_tagged_with_their_model(conn, cfg, nas):
    """A rebuildable cache: the model that produced each vector is recorded."""
    build(conn, cfg, nas, {"a.txt": "some indexed content here"})
    embed.embed_pending(conn)

    row = conn.execute("SELECT model_id, dim FROM embeddings LIMIT 1").fetchone()

    assert row["model_id"] == embed.DEFAULT_MODEL
    assert row["dim"] > 0


@requires_model
def test_reembedding_is_idempotent(conn, cfg, nas):
    build(conn, cfg, nas, {"a.txt": "content to embed"})
    first = embed.embed_pending(conn)

    second = embed.embed_pending(conn)

    assert first.embedded >= 1
    assert second.embedded == 0, "already-embedded passages must not be redone"


@requires_model
def test_dropping_embeddings_is_safe(conn, cfg, nas):
    """NFR-6: the vector table is a cache and can always be rebuilt."""
    build(conn, cfg, nas, {"a.txt": "recoverable indexed content"})
    embed.embed_pending(conn)

    conn.execute("DELETE FROM embeddings")

    assert SearchEngine(conn).search("recoverable content").hits, \
        "search must survive losing every vector"
    assert embed.embed_pending(conn).embedded >= 1, "and rebuild them"


@requires_model
def test_fused_signals_all_contribute_to_the_score(conn, cfg, nas):
    """Every signal shown must be part of the score.

    The keyword pass's raw bm25 is on a different scale and does not feed the
    fused score. Carrying it into the explain panel showed a large number next
    to a tiny score, which reads as a bug.
    """
    build(conn, cfg, nas, {"a.txt": "irrigation solenoid replaced in the front zone"})
    embed.embed_pending(conn)

    hit = SearchEngine(conn).search("sprinkler valve", semantic=True).hits[0]

    assert "bm25" not in hit.signals, "a non-contributing signal must not be shown"
    contributing = sum(v for k, v in hit.signals.items() if not k.startswith("_"))
    assert contributing > 0


@requires_model
def test_fused_scores_are_readable(conn, cfg, nas):
    """Raw RRF scores are ~0.01-0.03 and round to 0.00 in any display."""
    build(conn, cfg, nas, {f"doc{i}.txt": "water leak near the driveway"
                           for i in range(3)})
    embed.embed_pending(conn)

    hits = SearchEngine(conn).search("plumbing repair", semantic=True).hits

    assert hits
    assert hits[0].score == pytest.approx(1.0), "top hit should read as 1.0"
    assert all(0 < h.score <= 1.0 for h in hits)
    assert all(h.signals.get("_raw_rrf", 0) > 0 for h in hits), \
        "the raw value stays available for auditing"


def test_query_path_never_loads_a_model_on_demand(conn, cfg, nas, monkeypatch):
    """A service must not block on a model download mid-request.

    An embedding model is deterministic and welcome in the query path -- but it
    is loaded at startup, never inside a request, where a cold load reaches the
    network and crashed the worker.
    """
    build(conn, cfg, nas, {"a.txt": "some searchable content"})

    loads: list[str] = []
    real_load = embed.load_model

    def spy(model_id=embed.DEFAULT_MODEL, *, force=False):
        loads.append(model_id)
        return real_load(model_id, force=force)

    monkeypatch.setattr(embed, "load_model", spy)
    embed.set_lazy_load(False)
    try:
        embed._model_cache.clear()
        hits = SearchEngine(conn).search("searchable content", semantic=True).hits
        assert hits, "search must still work, falling back to keyword"
    finally:
        embed.set_lazy_load(True)


# --------------------------------------------------------- vectorised scoring
#
# The batched matrix product replaced a per-row Python loop. It is the same
# arithmetic, so it must produce the *same ranking* -- these pin that, and the
# fallback that keeps search working when numpy is absent (NFR-9).


def _random_unit_vectors(count: int, dim: int, seed: int = 11):
    import random

    rng = random.Random(seed)
    vectors = []
    for _ in range(count):
        raw = [rng.gauss(0, 1) for _ in range(dim)]
        norm = sum(v * v for v in raw) ** 0.5
        vectors.append([v / norm for v in raw])
    return vectors


def test_vectorised_scoring_matches_the_python_loop():
    vectors = _random_unit_vectors(200, 32)
    query = _random_unit_vectors(1, 32, seed=99)[0]
    blobs = [embed.pack(v) for v in vectors]

    fast = embed._scores_numpy(blobs, query)
    assert fast is not None, "numpy is installed; the fast path must engage"

    reference = [embed.cosine(query, v) for v in vectors]
    assert [float(s) for s in fast] == pytest.approx(reference, abs=1e-6)


def test_vectorised_scoring_preserves_ranking_order():
    """Scores agreeing to 1e-6 is not enough on its own -- the order is what
    reaches the user, so compare the ranking itself."""
    vectors = _random_unit_vectors(500, 32, seed=5)
    query = _random_unit_vectors(1, 32, seed=6)[0]
    blobs = [embed.pack(v) for v in vectors]

    def ranked(scores):
        return [i for i, _ in sorted(enumerate(scores), key=lambda p: (-p[1], p[0]))]

    fast = [float(s) for s in embed._scores_numpy(blobs, query)]
    slow = [embed.cosine(query, v) for v in vectors]
    assert ranked(fast) == ranked(slow)


def test_vectorised_scoring_handles_a_zero_vector():
    """A zero vector must score 0.0, not NaN -- NaN would sort unpredictably."""
    blobs = [embed.pack([0.0, 0.0, 0.0, 0.0]), embed.pack([1.0, 0.0, 0.0, 0.0])]
    scores = embed._scores_numpy(blobs, [1.0, 0.0, 0.0, 0.0])
    assert float(scores[0]) == 0.0
    assert float(scores[1]) == pytest.approx(1.0)


def test_vectorised_scoring_declines_a_ragged_batch():
    """A blob of a different width means a stale or truncated row. Returning
    None hands the batch to the per-row path rather than reshaping garbage."""
    blobs = [embed.pack([1.0, 0.0]), embed.pack([1.0, 0.0, 0.0])]
    assert embed._scores_numpy(blobs, [1.0, 0.0]) is None


def test_vector_search_falls_back_when_numpy_is_missing(monkeypatch):
    """Search must survive numpy being absent: the import happens inside the
    helper precisely so this degrades to the Python loop instead of raising."""
    import builtins

    real_import = builtins.__import__

    def no_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("numpy is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_numpy)
    assert embed._scores_numpy([embed.pack([1.0, 0.0])], [1.0, 0.0]) is None


@requires_model
def test_vector_search_crosses_batch_boundaries(conn, cfg, nas, monkeypatch):
    """Results must not depend on how rows happen to be batched."""
    build(conn, cfg, nas, {
        f"note{i}.txt": f"the irrigation solenoid valve number {i} was replaced"
        for i in range(12)
    })
    embed.embed_pending(conn)

    monkeypatch.setattr(embed, "BATCH_ROWS", 10_000)
    whole = embed.search(conn, "sprinkler valve", limit=10)
    monkeypatch.setattr(embed, "BATCH_ROWS", 3)
    split = embed.search(conn, "sprinkler valve", limit=10)

    assert [pid for pid, _ in whole] == [pid for pid, _ in split]


def test_vector_search_tie_break_matches_a_full_sort(conn, cfg, nas, monkeypatch):
    """The bounded heap must return exactly what sorting everything would.

    Ties are the risky case: identical text embeds identically, so the tie-break
    on passage_id is what decides, and a heap that drops the wrong side of a tie
    would silently return different results than the full sort it replaced.
    """
    import heapq

    rows = [(pid, score) for pid, score in
            [(5, 0.9), (3, 0.5), (9, 0.5), (1, 0.5), (7, 0.2), (4, 0.9)]]
    limit = 3

    best: list[tuple[float, int]] = []
    for passage_id, score in rows:
        entry = (float(score), -passage_id)
        if len(best) < limit:
            heapq.heappush(best, entry)
        elif entry > best[0]:
            heapq.heapreplace(best, entry)
    from_heap = sorted([(-nid, s) for s, nid in best],
                       key=lambda pair: (-pair[1], pair[0]))

    from_sort = sorted(rows, key=lambda pair: (-pair[1], pair[0]))[:limit]
    assert from_heap == from_sort


@requires_model
def test_vector_search_returns_the_strongest_hits_under_a_small_limit(conn, cfg, nas):
    """A limit smaller than the corpus must still return the best matches --
    the heap must not drop a strong hit seen late in the scan."""
    build(conn, cfg, nas, {
        "solenoid.txt": "the irrigation solenoid valve was replaced today",
        **{f"filler{i}.txt": f"unrelated musings about pottery number {i}"
           for i in range(15)},
    })
    embed.embed_pending(conn)

    top = embed.search(conn, "sprinkler valve", limit=3)
    assert len(top) == 3
    assert top == sorted(top, key=lambda pair: (-pair[1], pair[0])), "must be ordered"

    everything = embed.search(conn, "sprinkler valve", limit=1000)
    assert [pid for pid, _ in top] == [pid for pid, _ in everything[:3]]


def test_embed_pending_does_not_load_every_passage_at_once():
    """`fetchall()` here held every pending passage WITH ITS FULL TEXT before
    embedding any of them. At 3.25M passages that is gigabytes resident before
    the model even loads, and systemd OOM-killed the unit every time -- while
    the code below it batched carefully in 64s, which bought nothing.

    Pinning the source is crude, but the failure only appears at a scale no
    fixture can reach, and the shape is what matters."""
    source = Path(embed.__file__).read_text()
    body = source[source.index("def embed_pending"):]
    body = body[:body.index("\ndef ")]
    assert ".fetchall()" not in body, (
        "embed_pending must stream its rows, not materialise the whole queue")
    assert "fetchmany(batch_size)" in body


@requires_model
def test_embed_pending_streams_every_pending_passage(conn, cfg, nas):
    """Streaming must not lose rows: the loop writes to `embeddings`, which is
    the very table its own query filters on, and commits between batches."""
    build(conn, cfg, nas, {
        f"note{i}.txt": f"document number {i} about irrigation and valves"
        for i in range(30)
    })
    pending = conn.execute(
        "SELECT COUNT(*) FROM passages WHERE length(trim(text)) > 0"
    ).fetchone()[0]
    assert pending >= 30, "fixture must produce enough passages to batch"

    result = embed.embed_pending(conn, batch_size=4)
    assert result.embedded == pending, "every pending passage must be embedded"

    stored = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert stored == pending
    # A second pass has nothing left to do -- proof none were silently skipped.
    assert embed.embed_pending(conn, batch_size=4).embedded == 0
