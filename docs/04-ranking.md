# How ranking works

Every number in a search result comes from one formula. Nothing is learned or
tuned at runtime, so the same query over the same index returns the same order
forever (NFR-2).

```
score = 1.0 / (60 + keyword_rank)      # BM25, via SQLite FTS5
      + 0.8 / (60 + vector_rank)       # cosine similarity, MiniLM-L6-v2
      + title_match / 100              # query words in the filename
      + recency / 100                  # recently modified
```

The constants live in `src/tracepaper/query/search.py` and the "How it works"
page in the UI interpolates them, so the documentation cannot drift from the
ranking it describes.

## Reading a result line

```json
"signals": {"rrf_bm25": 0.015873, "rrf_vector": 0.011940,
            "title_match": 0.005, "recency": 0.002831,
            "_raw_rrf": 0.035644}
```

Each RRF contribution inverts to a rank: `1.0/(60+3) = 0.015873` is keyword
rank 3; `0.8/(60+7) = 0.011940` is vector rank 7.

`score` is rescaled so the top hit is `1.0`, because raw RRF values sit around
0.01–0.03 and round to 0.00 in any display. Order is untouched; `_raw_rrf`
keeps the true sum.

## Why ranks rather than scores

BM25 is unbounded. Cosine similarity runs −1 to 1. Combining them directly
needs a normalisation that shifts as the corpus grows, which means the same
query can reorder as unrelated documents arrive. Ranks are comparable by
construction, and RRF needs no training data.

## Why keyword outranks meaning

`RRF_WEIGHT_BM25 = 1.0` against `RRF_WEIGHT_VECTOR = 0.8`. A document
containing the exact words should never lose to one that merely seems related.
Vectors exist to find *irrigation solenoid* when you typed *sprinkler valve* --
to add recall, not to overrule evidence.

## Why there is a similarity floor

`MIN_VECTOR_SIMILARITY = 0.25`. Brute-force vector search always returns
*something*. On a query with no real semantic match, the least-unrelated
passage would land at vector rank 1 and fusion would promote it. Below the
floor it is discarded as noise.

## Provenance

| Piece | Source |
|---|---|
| Reciprocal Rank Fusion, `k=60` | Cormack, Clarke & Büttcher, *Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods*, SIGIR 2009. k=60 is the paper's constant, used unchanged. |
| BM25 | Robertson, Walker, Jones, Hancock-Beaulieu & Gatford, *Okapi at TREC-3*, 1994; probabilistic relevance framework of Robertson & Spärck Jones. Provided by SQLite FTS5. |
| Embeddings | `sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions, run locally. |

The two weights, the two boosts and the similarity floor are this project's own
choices. They are fixed constants, auditable in one file, never adjusted per
query.

## Vector search is exact, with a deadline

Every stored vector is compared -- no approximate index, so no recall loss and
nothing to tune or keep in sync. Scoring is one batched matrix product
(`embed.py`), roughly a second per million passages.

The scan stops at `VECTOR_SCAN_BUDGET_SECONDS` and fuses whatever it scored.
Under heavy disk contention that trades recall for responsiveness; keyword
results are unaffected. A search answering on BM25 alone is the documented
fallback (NFR-9), and one that hangs is not.

## What an LLM does not do

No language model takes part in ranking, and none reads documents to answer a
query. An embedding model is permitted in the query path because it is a
deterministic function from text to a vector. An inference model is confined to
ingest, where its output is stored with its provenance and can be corrected --
and a human correction outranks every extractor, permanently.
