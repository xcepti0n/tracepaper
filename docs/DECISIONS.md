# Decision Log

Short, dated records. Full reasoning lives in [02-alternatives.md](02-alternatives.md).

---

### D-001 — Structured extraction at ingest; deterministic search at query
**2026-09-04 · Accepted**

Models enrich data offline; the query path is SQL + BM25 + vectors with a fixed fusion
formula. Rejected: agent-browses-filesystem (non-reproducible), classic RAG (answer
produced by the model at query time, weak on point facts and aggregation).

*Consequence:* a better model is a re-ingest. No query code, API, or tool contract
changes.

---

### D-002 — SQLite + FTS5 + sqlite-vec as the index
**2026-09-04 · Accepted**

Fields, full text, and vectors in one file, ~50 MB RAM, no daemon. Rejected OpenSearch
despite it being on the table: a JVM cluster on a 16 GB box already running other
services violates the resource budget, and nothing at 100k items needs sharding.

*Consequence:* Postgres + pgvector is the documented migration target. The query layer
goes through a repository interface so that move is a reimplementation plus a data copy,
not a rewrite.

---

### D-003 — Layered extraction: human > template > pattern > LLM
**2026-09-04 · Accepted**

All extractors write to one `fields` table tagged with `source` and `confidence`.
Regex handles regular formats exactly and free; the LLM only fills gaps, constrained to
schema-validated JSON.

*Consequence:* the LLM layer can be deleted and the system still works. Human
corrections outrank everything and survive reindex.

---

### D-004 — Semantic search included, but as the third signal
**2026-09-04 · Accepted**

Structured fields first, BM25 second, vectors third, fused by Reciprocal Rank Fusion
(k=60, fixed). Vectors exist for "I remember the gist, not the words" — the note about
the *sprinkler valve* that says *irrigation solenoid*. They never override an exact
field match.

*Consequence:* embeddings are a rebuildable cache tagged with `model_id`. Changing
embedding model is a re-index, never a migration.

---

### D-005 — Photos indexed by layered tags; images never stored
**2026-09-04 · Accepted**

EXIF, geocoding, face clustering, and VLM captions write independently to the shared
tag table. Only the caption layer depends on a model.

*Consequence:* face-cluster names are permanent human-authored data. Improving captions
re-runs one job type and leaves every exact fact intact.

---

### D-006 — Split deployment: service on Proxmox, ingest worker on Mac
**2026-09-04 · Accepted**

Mirrors the ingest/query split in hardware. Search stays up when the Mac sleeps.

*Consequence:* enrichment lags while the Mac is off. Made explicit via
`extraction_status` and a UI indicator — never a silently missing field.

---

### D-007 — The indexed unit is an "item", not a file
**2026-09-04 · Accepted**

Covers file-backed documents, photos, and native typed notes under one identity, tag,
field, and search model. Notes are editable with version history; files are re-ingested
on change.

*Consequence:* no separate subsystem for personal notes, and notes are searchable by the
same tools as documents.
