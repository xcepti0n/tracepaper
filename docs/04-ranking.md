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
| Reciprocal Rank Fusion, `k=60` | Cormack, Clarke & Büttcher, [*Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods*](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf), SIGIR 2009, pp. 758–759 ([dblp](https://dblp.org/rec/conf/sigir/CormackCB09.html)) |
| BM25 | Robertson, Walker, Jones, Hancock-Beaulieu & Gatford, [*Okapi at TREC-3*](https://trec.nist.gov/pubs/trec3/papers/city.ps.gz), 1994; implemented by [SQLite FTS5](https://www.sqlite.org/fts5.html) |
| Embeddings | Reimers & Gurevych, [*Sentence-BERT*](https://arxiv.org/abs/1908.10084), EMNLP 2019; model [`all-MiniLM-L6-v2`](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2), 384 dimensions, run locally |

On `k=60` specifically, since it is the one magic number in the formula: the
paper states it "was fixed during a pilot investigation and not altered during
subsequent validation", and that it "was near-optimal, but that the choice was
not critical". It is used here unchanged.

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


## Further reading

Ordered by how useful each is if you want to go deeper, rather than by date.

**Start here.** Robertson & Zaragoza,
[*The Probabilistic Relevance Framework: BM25 and Beyond*](https://www.staff.city.ac.uk/~sbrp622/papers/foundations_bm25_review.pdf)
(2009). Book-length but readable, by BM25's own author; explains *why* the
formula has the shape it does rather than only stating it.

**The fusion paper.** Cormack, Clarke & Büttcher,
[RRF](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf) (SIGIR 2009). Two
pages. Worth reading in full precisely because the whole method is one formula —
it is a good demonstration that a simple rank combination beats more elaborate
learned fusion.

**Embeddings.** Reimers & Gurevych,
[*Sentence-BERT*](https://arxiv.org/abs/1908.10084) (EMNLP 2019) — why a model
fine-tuned for sentence similarity beats averaging raw BERT token vectors, which
is what `all-MiniLM-L6-v2` descends from.

**Hybrid retrieval in context.** Lin, Nogueira & Yates,
[*Pretrained Transformers for Text Ranking: BERT and Beyond*](https://arxiv.org/pdf/2010.06467)
(2020). Survey-length; the chapters on combining sparse and dense retrieval
cover the tradeoff this project settles with RRF.

**The implementation.** [SQLite FTS5](https://www.sqlite.org/fts5.html),
especially the `bm25()` auxiliary function — the weights passed to it are
`BM25_WEIGHT_TEXT` and `BM25_WEIGHT_TITLE` in `query/search.py`.

Every link above was checked to resolve. `dl.acm.org` copies of the same papers
exist but sit behind a paywall, so the open-access versions are linked instead.
