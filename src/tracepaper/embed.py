"""Passage embeddings (D-004).

Vectors answer the query nothing else can: "that note about the sprinkler
valve" when the note says *irrigation solenoid*. They are the third signal,
behind structured fields and BM25 -- never allowed to override an exact match.

Embeddings are a **rebuildable cache**, not source data. Every vector is stored
with the `model_id` that produced it, so switching models is a re-index rather
than a migration, and the table can be dropped at any time (NFR-6).

Embedding happens at ingest, on the worker. The query side loads the model only
to embed the query string itself; with no model installed, search falls back to
BM25 and still works (NFR-9).
"""

from __future__ import annotations

import logging
import sqlite3
import struct
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Small, fast, and good enough for personal documents. Pinned by name so the
# stored model_id is meaningful.
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_model_cache: dict[str, object] = {}

# An embedding model is not inference: it is a deterministic function from text
# to a vector, and the same query embeds identically forever. So it IS allowed
# in the query path -- unlike an LLM, which is confined to ingest.
#
# What is not allowed is loading it *during a request*: a cold load reaches the
# network and can take tens of seconds, and doing that inside a web worker
# crashed it. Services call `preload()` at startup and then forbid lazy loading,
# so a search either uses a resident model or falls back to keyword search --
# never blocks on a download.
_allow_lazy_load = True


def set_lazy_load(enabled: bool) -> None:
    """Allow or forbid loading a model on demand.

    The web service and MCP server forbid it, so a search can never block on a
    model download or crash a worker mid-request.
    """
    global _allow_lazy_load
    _allow_lazy_load = enabled


def preload(model_id: str = DEFAULT_MODEL) -> bool:
    """Load the model up front, outside any request. Returns success.

    Downloads it on first use, then caches to disk (~90 MB for the default),
    so later starts are offline and fast.
    """
    return load_model(model_id, force=True) is not None


def local_path(model_id: str = DEFAULT_MODEL) -> str | None:
    """Where the model is cached on disk, if it has been downloaded."""
    from pathlib import Path
    cache = Path.home() / ".cache" / "huggingface" / "hub"
    folder = cache / f"models--{model_id.replace('/', '--')}"
    return str(folder) if folder.exists() else None


def is_loaded(model_id: str = DEFAULT_MODEL) -> bool:
    return model_id in _model_cache


@dataclass
class EmbedResult:
    embedded: int = 0
    skipped: int = 0
    model_id: str = ""

    def summary(self) -> str:
        return (f"embedded={self.embedded} skipped={self.skipped} "
                f"model={self.model_id or 'none'}")


def available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except Exception:
        return False


def load_model(model_id: str = DEFAULT_MODEL, *, force: bool = False):
    """Return the cached model, loading it only when that is permitted."""
    if model_id in _model_cache:
        return _model_cache[model_id]
    if not force and not _allow_lazy_load:
        return None
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(model_id)
        _model_cache[model_id] = model
        return model
    except Exception as exc:
        log.warning("cannot load embedding model %s: %s", model_id, exc)
        return None


def pack(vector) -> bytes:
    """Store a vector as float32 bytes. Compact and exactly reproducible."""
    return struct.pack(f"{len(vector)}f", *(float(v) for v in vector))


def unpack(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Vectors are stored normalised, so this is a dot
    product -- but the norms are recomputed anyway so a non-normalised vector
    from a different model cannot silently skew scores."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def embed_pending(conn: sqlite3.Connection, *, model_id: str = DEFAULT_MODEL,
                  batch_size: int = 64, limit: int | None = None) -> EmbedResult:
    """Embed passages that have no current vector.

    Passages whose vector was produced by a different model are re-embedded,
    since mixing models in one index makes similarity meaningless.
    """
    result = EmbedResult(model_id=model_id)

    rows = conn.execute(
        "SELECT p.id, p.text FROM passages p "
        "LEFT JOIN embeddings e ON e.passage_id = p.id AND e.model_id = ? "
        "JOIN items i ON i.id = p.item_id "
        "WHERE e.passage_id IS NULL AND i.deleted_at IS NULL "
        "AND length(trim(p.text)) > 0 "
        "ORDER BY p.id" + (f" LIMIT {int(limit)}" if limit else ""),
        (model_id,),
    ).fetchall()

    if not rows:
        return result

    model = load_model(model_id, force=True)
    if model is None:
        result.skipped = len(rows)
        return result

    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        texts = [row["text"] for row in batch]
        try:
            vectors = model.encode(texts, normalize_embeddings=True,
                                   show_progress_bar=False)
        except Exception as exc:
            log.exception("embedding batch failed: %s", exc)
            result.skipped += len(batch)
            continue

        for row, vector in zip(batch, vectors):
            blob = pack(vector)
            conn.execute(
                "INSERT INTO embeddings (passage_id, vector, model_id, dim) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(passage_id, model_id) DO UPDATE SET "
                "vector = excluded.vector, dim = excluded.dim",
                (row["id"], blob, model_id, len(vector)),
            )
            result.embedded += 1

    return result


def search(conn: sqlite3.Connection, query: str, *, limit: int = 20,
           model_id: str = DEFAULT_MODEL,
           kind: str | None = None) -> list[tuple[int, float]]:
    """Vector search. Returns (passage_id, similarity) ordered deterministically.

    Brute force over stored vectors. At 100k passages this is a few hundred
    milliseconds in Python and needs no index to maintain or tune; a real ANN
    index is the upgrade path if the corpus outgrows it.
    """
    model = load_model(model_id)
    if model is None:
        return []

    try:
        query_vector = list(model.encode([query], normalize_embeddings=True,
                                         show_progress_bar=False)[0])
    except Exception as exc:
        log.warning("cannot embed query: %s", exc)
        return []

    sql = ("SELECT e.passage_id, e.vector FROM embeddings e "
           "JOIN passages p ON p.id = e.passage_id "
           "JOIN items i ON i.id = p.item_id "
           "WHERE e.model_id = ? AND i.deleted_at IS NULL")
    params: list[object] = [model_id]
    if kind:
        sql += " AND i.kind = ?"
        params.append(kind)

    scored: list[tuple[int, float]] = []
    for row in conn.execute(sql, params):
        similarity = cosine(query_vector, unpack(row["vector"]))
        scored.append((int(row["passage_id"]), similarity))

    # Sort by score, then passage_id: a total order, so equal scores never
    # reorder between runs (NFR-2).
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:limit]


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM passages WHERE length(trim(text)) > 0"
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT model_id, COUNT(*) AS n FROM embeddings GROUP BY model_id"
    ).fetchall()
    return {
        "passages": int(total),
        "by_model": {r["model_id"]: int(r["n"]) for r in rows},
    }
