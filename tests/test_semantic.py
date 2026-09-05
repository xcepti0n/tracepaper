"""Semantic search and RRF fusion (D-004, NFR-2, NFR-9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from datamanager import embed
from datamanager.index.indexer import Indexer
from datamanager.query.search import SearchEngine
from datamanager.scan.scanner import Scanner

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
