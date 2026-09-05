# DataManager — Alternatives Considered

Status: agreed 2026-09-04 (revised)

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
- **Against:** **chunking destroys fact associations.** On a W-2 the tax year is in a
  header box and gross salary is in Box 1; they land in different chunks, and no
  retrieved chunk can say which year the number belongs to. The model then reads a
  number off the wrong form. This is not a tuning problem — the link is gone before
  retrieval starts. Also: the answer is produced *by the model at query time*, so it is
  neither reproducible nor model-independent (NFR-1, NFR-2); aggregation across
  documents is impossible; retrieval quality drifts silently when the embedding model
  changes.
- **Verdict:** rejected as the architecture. Passage retrieval is kept as the **floor**
  (FR-7) and vectors as one ranking signal — but facts are bound at ingest, before
  chunking, and stored resolved.

### A3 — Pure rules/regex extraction, no model anywhere
Hand-written patterns per document type.

- **For:** maximally deterministic and auditable. Cheap to run. No GPU, no Ollama.
- **Against:** every new document format is a code change. Scanned/OCR'd text breaks
  brittle patterns. Photos and free-form notes get essentially nothing. Realistically
  becomes an unbounded maintenance backlog.
- **Verdict:** rejected standalone — but retained as the **highest-priority extractor**
  inside the chosen design, because when a pattern does match it is exact and free.

### A4 — Whole-document binding at ingest; deterministic retrieval at query ✅ **CHOSEN**
Ingest reads each document **as a whole** and writes bound records, events, entities, and
passages. Queries read those layers with fixed algorithms.

- **For:** the binding happens while the full document is in view, so `tax_year` and
  `gross_salary` are stored already paired — the failure in A2 cannot occur. Direct
  answers need no model (Tier 1). Aggregation is arithmetic over records. Passages remain
  as a floor so unrecognized document types stay searchable. A better model later is a
  re-ingest with no code change.
- **Against:** more upfront build. Extraction errors persist until re-ingest (mitigated
  by confidence, provenance, and human corrections that survive reindex — FR-10). Open
  vocabulary needs canonicalization machinery.
- **Verdict:** chosen. Costs are one-time and bounded; the alternatives' costs recur on
  every query and every model release.

---

## A5. Answer contract — what the engine returns

### A5.1 — Engine returns a natural-language answer
Rejected. Requires a model at query time, making output non-reproducible and rebinding
quality to the current model — NFR-1 and NFR-2 both fail.

### A5.2 — Engine returns only ranked documents
Rejected. *"Passport expiry date"* has its answer written in the document; returning a
file to open by hand is the status quo this project replaces.

### A5.3 — Two tiers: direct answer where the fact exists, evidence set otherwise ✅ **CHOSEN**
Tier 1 returns the value plus citation, deterministically, no model. Tier 2 returns a
complete, reproducibly-ordered evidence set for a caller's LLM to reason over.

- **For:** matches the two real query shapes. *"Passport expiry"* needs no LLM;
  *"best card at Costco"* has no answer written anywhere and needs one. The determinism
  guarantee attaches to the **evidence set**, which is the part the engine controls.
- **Against:** callers must handle two response shapes; the Tier 1/Tier 2 boundary is a
  judgment call for ambiguous queries (resolved by rules over query structure and
  extraction confidence, never by a model).
- **Verdict:** chosen. DataManager never generates prose.

---

## A6. Schema vocabulary

### A6.1 — Closed schema (predefined field keys)
Rejected. A lifetime of IDs, leases, visas, appointments, receipts, and statements has no
enumerable key set. Every unfamiliar document type would need a code change — the exact
treadmill the project exists to avoid.

### A6.2 — Open vocabulary with canonicalization ✅ **CHOSEN**
Extractors emit whatever keys the document contains; `key_vocabulary` maps synonyms
(`gross_pay` / `wages` / `gross_salary`) to a canonical key, with user pinning.

- **For:** new document types need no code. Query-time key mapping stays a deterministic
  lookup because the vocabulary is materialized at ingest.
- **Against:** vocabulary drift needs periodic curation; the review UI is real work.
- **Verdict:** chosen.

---

## A7. The unit of retrieval

### A7.1 — Documents
Insufficient alone. *"When did I last fly Alaska"* asks about something that **happened**;
the evidence might be a confirmation email, a boarding pass, or a statement line, and the
user does not care which.

### A7.2 — Documents + records + events + entities ✅ **CHOSEN**
Events carry type, date, entities, and links to every supporting document. Entities
collapse aliases (`COSTCO WHSE #1234` → `Costco`).

- **For:** answers date/history questions directly; assembles Tier 2 evidence coherently;
  one event survives having three different documents as evidence.
- **Against:** event dedup across evidence types is fiddly (an open question in the
  design).
- **Verdict:** chosen.

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
Extractors run in priority order and all emit **bound records** (`records` +
`record_fields`) carrying a `source` and `confidence`:

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

**Yes — as one retrieval signal, and as part of the passage floor (FR-7).**

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
