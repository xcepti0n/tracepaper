# DataManager — Alternatives Considered

Status: agreed 2026-09-04

Records what was evaluated and why the chosen option won, so the decision can be
revisited later against the reasoning rather than from scratch.

---

## A. Overall approach

### A1 — Agent browses the NAS directly (filesystem tools)
Give an agent read access and let it `find`/`grep`/open files as it reasons.

- **For:** zero build cost. Works today.
- **Against:** every answer depends on the current model's diligence. Non-reproducible —
  the same question yields different answers on different runs. Cannot answer anything
  requiring a scan of 40k files. No cross-document aggregation. Directly contradicts
  NFR-1 and NFR-2, and is the exact failure the project exists to avoid.
- **Verdict:** rejected. This is the problem statement, not a solution.

### A2 — Classic RAG (chunk → embed → top-k → stuff into a prompt)
The standard vector-store pipeline.

- **For:** well-trodden, many libraries, good on "explain this topic" questions.
- **Against:** the answer is produced *by the model at query time*, so it is neither
  deterministic nor model-independent — precisely NFR-1 and NFR-2. Bad at point facts:
  "salary in 2023" retrieves chunks that look salary-ish and the model may read the
  wrong year off the wrong form. Bad at aggregation ("total spent on appliances").
  Retrieval quality silently drifts when the embedding model changes.
- **Verdict:** rejected as the primary architecture. Vector search is kept as one
  *ranking signal* (see D), not as the answer mechanism.

### A3 — Pure rules/regex extraction, no model anywhere
Hand-written patterns per document type.

- **For:** maximally deterministic and auditable. Cheap to run. No GPU, no Ollama.
- **Against:** every new document format is a code change. Scanned/OCR'd text breaks
  brittle patterns. Photos and free-form notes get essentially nothing. Realistically
  becomes an unbounded maintenance backlog.
- **Verdict:** rejected standalone — but retained as the **highest-priority extractor**
  inside the chosen design, because when a pattern does match it is exact and free.

### A4 — Structured extraction at ingest, deterministic search at query ✅ **CHOSEN**
Models read each item once, offline, and write typed fields, tags, and embeddings into
a database. Queries are SQL + BM25 + vector similarity fused by a fixed formula.

- **For:** satisfies NFR-1 and NFR-2 directly — no model in the query path. Point facts
  become field lookups, which is the right primitive for FR-4. Aggregation is just SQL.
  A better model later is a re-ingest with no code change. Extraction cost is paid once
  per item, not once per query.
- **Against:** more upfront build than A1/A2. Extraction errors are baked into the index
  until re-ingest (mitigated by confidence scores, provenance, and human correction that
  survives reindex — FR-6). Needs a schema, which needs maintenance as new document
  types appear.
- **Verdict:** chosen. The costs are real but bounded and one-time; the alternatives'
  costs recur on every query and every model release.

---

## B. Storage and search engine

Evaluated against NFR-8 (< 1 GB RAM, no JVM, on a 16 GB box already running other
services) and NFR-5 (10k–100k items).

| Option | RAM | FTS | Vectors | Verdict |
|---|---|---|---|---|
| **SQLite + FTS5 + sqlite-vec** | ~50 MB | BM25, built in | via `sqlite-vec` | ✅ **Chosen for v1** |
| Postgres + tsvector + pgvector | ~300 MB+ | good | HNSW, mature | Documented migration target |
| OpenSearch / Elasticsearch | 2–4 GB JVM | excellent | good | Rejected — violates NFR-8 |
| Typesense / Meilisearch | ~200 MB | excellent | basic | Rejected — weak relational/aggregate queries |
| Qdrant / Chroma / LanceDB | varies | none/weak | excellent | Rejected as primary — vector-only, no SQL |

**Chosen: SQLite + FTS5 + sqlite-vec.**
One file, trivial backup, no daemon, no tuning, and it comfortably covers 100k items.
BM25 is built into FTS5. Crucially, structured fields, full text, and vectors all live
in *one* store, so a query can filter and rank in a single statement without joining
across systems.

Rejection of OpenSearch is the notable one, since it was on the table: a JVM cluster is
the wrong shape for a home server that is already busy, and nothing in the requirements
needs distributed sharding at 100k items.

**Migration path (NFR-5 headroom):** the query layer talks to a repository interface,
not raw SQL strings. Moving to Postgres + pgvector — worthwhile if the index passes a
few hundred thousand items or needs real concurrent writers — is a repository
reimplementation plus a data copy, with no change to the API, MCP tools, or UI.

---

## C. Extraction strategy

### C1 — Cloud LLM API at ingest
- **For:** best quality, least maintenance.
- **Against:** tax documents and financial statements leave the house (NFR-3), and there
  is a per-document cost across tens of thousands of items.
- **Verdict:** rejected as default. Left as an opt-in per-source override for a hard
  backlog, off by default.

### C2 — Local small LLM for everything
- **For:** private, free, no rules to maintain.
- **Against:** wasteful and less exact than a regex on formats that are perfectly
  regular (a W-2 has fixed box numbers). Hallucinated field values on low-quality OCR.
- **Verdict:** rejected as the sole mechanism.

### C3 — Layered: deterministic first, model as fallback ✅ **CHOSEN**
Extractors run in priority order and all write to the same `fields` table with a
`source` and `confidence`:

1. **Human correction** — absolute authority, never overwritten (FR-6).
2. **Format/template rules** — known layouts (W-2, 1099, specific banks and utilities).
   Exact, free, instant.
3. **Typed pattern extractors** — dates, currency amounts, account tails, tax years,
   identifiers. Validated by type, not just matched.
4. **Local LLM (Ollama on the Mac)** — only for items where the layers above left gaps.
   Constrained to emit JSON against a fixed schema; anything failing validation is
   discarded rather than stored.

- **For:** cheap and exact on the common case, covers the long tail, degrades gracefully.
  Each layer is independently re-runnable, so improving the model re-runs only layer 4
  and leaves layers 1–3 untouched.
- **Against:** most code of the three options.
- **Verdict:** chosen. The layering is also what makes NFR-1 concrete — layer 4 can be
  deleted entirely and the system still works, just with thinner coverage.

**Prompting constraint (directly serving NFR-1):** layer 4's prompt asks only for
extraction against a fixed JSON schema — "find these fields in this text" — never for
judgment, ranking, or interpretation. That is a task small and old models already do
adequately, so a better model improves recall at the margin instead of being required.

---

## D. Do we need semantic search?

The question was raised explicitly, so the reasoning is recorded.

**Yes — as the third signal, never the first.**

- Structured fields answer point queries *exactly*. For "salary in 2023", vector search
  is actively worse: it returns things that resemble salary text, and resemblance is not
  a number.
- BM25 answers "I remember a distinctive word" — names, model numbers, merchants.
- Vectors answer "I remember the gist but no exact word" — *"that note about fixing the
  sprinkler valve"* when the note says *irrigation solenoid*. Nothing else recovers this.

So all three run and are fused by **Reciprocal Rank Fusion** (fixed constant, no
learned weights, so NFR-2 holds). Vector search is confined to a role where being
approximate is acceptable, and never gets to override an exact field match.

**Embeddings are treated as a rebuildable cache**, not source data: stored with the
model name and version that produced them, and droppable and recomputable at any time.
Changing embedding model is a re-index, never a migration.

---

## E. Photo indexing

### E1 — Store/copy photos into the system
Rejected. Duplicates terabytes for no benefit; NAS already holds them (NFR-7).

### E2 — VLM caption only
Rejected as sole source. Throws away EXIF, which is *exact* — a caption guessing "summer
day" when EXIF states 2019-03-14 is strictly worse. Also re-captioning everything on a
model change would be the only way to improve anything.

### E3 — Layered tag sources ✅ **CHOSEN**
EXIF, geocoding, face clustering, and VLM captioning write independently into the same
tag table, each labelled with its source. Matches the FR-7 breakdown.

- **For:** exact facts stay exact. Face-cluster names, assigned once by hand, are
  permanent human-authored data. Only the fuzzy layer depends on a model, and it can be
  re-run alone.
- **Against:** four pipelines instead of one.
- **Verdict:** chosen — same layering logic as C3, and the same payoff under NFR-1.

---

## F. Ingest topology

### F1 — Everything on the Proxmox host
Rejected. 16 GB already shared with other services; running Ollama there risks the
always-on layer (NFR-8).

### F2 — Everything on the Mac
Rejected. The Mac is not always on; search would die with it (NFR-4).

### F3 — Split: always-on service on Proxmox, ingest worker on the Mac ✅ **CHOSEN**
Proxmox holds the database, API, UI, MCP server, and file watcher. The watcher enqueues
jobs. The Mac's worker claims jobs when it is awake, does OCR/embedding/LLM work, and
writes results back.

- **For:** matches the ingest/query split in hardware. Search stays up regardless of the
  Mac. Heavy work lands on the machine suited to it.
- **Against:** two deployment targets; a queue to operate; enrichment lags when the Mac
  sleeps.
- **Verdict:** chosen. The lag is made explicit rather than hidden — items carry an
  extraction status, and the UI shows what is still pending (NFR-4), so a stale field is
  never mistaken for a missing one.
