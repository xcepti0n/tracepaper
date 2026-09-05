# Decision Log

Short, dated records. Full reasoning lives in [02-alternatives.md](02-alternatives.md).

---

### D-001 — Whole-document binding at ingest; deterministic retrieval at query
**2026-09-04 · Accepted (revised)**

Ingest reads each document as a whole and writes bound records, events, entities, and
passages. Queries read those with fixed algorithms — no model.

Rejected: agent-browses-filesystem (non-reproducible); classic RAG — **chunking destroys
fact associations**, so a W-2's `tax_year` and `gross_salary` land in different chunks
and no retrieved passage can say which year the number belongs to.

*Consequence:* facts that belong together are stored already paired. A better model is a
re-ingest; query code, API, and tool contracts do not change.

---

### D-001a — Two-tier answer contract
**2026-09-04 · Accepted**

Tier 1 returns a **value plus citation** where the fact is written in a document
(*passport expiry*) — no LLM. Tier 2 returns a **complete, reproducibly-ordered evidence
set** where it is not (*best card at Costco*), for a caller's LLM to reason over.

*Consequence:* the determinism guarantee attaches to the evidence set, which the engine
controls. DataManager never generates prose.

---

### D-001b — Open vocabulary, not a closed schema
**2026-09-04 · Accepted**

Extractors emit whatever keys a document actually contains; `key_vocabulary`
canonicalizes synonyms, with user pinning. Supersedes the earlier predefined key list.

*Consequence:* a document type never seen before yields records with zero code changes.

---

### D-001c — Records, events, and entities as retrieval units
**2026-09-04 · Accepted**

Many questions ask about something that *happened*, where the document is incidental.
One flight event links a confirmation email, a boarding pass, and a statement line.
Entities collapse aliases (`COSTCO WHSE #1234` → `Costco`).

*Consequence:* "when did I last fly Alaska" is a filter-and-sort, not a document search.

---

### D-001d — Passage layer as the floor
**2026-09-04 · Accepted**

Every item is also split into passages indexed by BM25 and embeddings, regardless of
whether any extractor understood it.

*Consequence:* with no structured extraction at all, the system degrades to good search
rather than to nothing (NFR-9).

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

All extractors emit **bound records** (`records` + `record_fields`) tagged with `source`
and `confidence` — never loose, independently-extracted fields.
Regex handles regular formats exactly and free; the LLM only fills gaps, constrained to
schema-validated JSON.

*Consequence:* the LLM layer can be deleted and the system still works. Human
corrections outrank everything and survive reindex.

---

### D-004 — Semantic search included, as one signal within the passage floor
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

---

### D-008 — Index stored on Proxmox, not the NAS
**2026-09-04 · Accepted**

SQLite over SMB/NFS is unreliable under concurrent access. The index lives on the
always-on Proxmox host.

*Consequence:* the index is fully rebuildable from the NAS; only the human-authored layer
(corrections, notes, entity merges, face names, pinned vocabulary) is irreplaceable, and
it is backed up separately to the NAS.

---

### D-009 — Change detection by scheduled reconciliation scan, hash-authoritative
**2026-09-04 · Accepted**

Documents live on a Synology NAS mounted read-only over SMB/NFS, where inotify/FSEvents
do not propagate — a watcher on the Proxmox host never fires. A background scan walks the
tree collecting `(uri, size, mtime)`, selects candidates whose metadata changed, and
**hashes only those** to decide what actually changed.

mtime is a filter, never the decision: Synology restores, `rsync`, and sync clients
rewrite it without changing content, and can move it backwards.

Rejected: filesystem watcher (not viable over SMB/NFS); a Synology-side agent (software to
maintain on the NAS, tied to DSM upgrades, still needs reconciliation).

*Consequence:* no missed-event failure mode — state is compared, not consumed. An outage
of the scanner, Proxmox, or the NAS costs delay only; the next pass reconciles. Move
detection falls out of hashing for free.

---

### D-010 — LLM as a swappable HTTP endpoint, ingest only
**2026-09-04 · Accepted**

Whole-document extraction assumes an LLM endpoint (Ollama on the Mac by default) behind a
small interface: `extract(text, shape) → JSON`. Never called at query time.

*Consequence:* the endpoint can be swapped for another server, model, or cloud API
per-source with no pipeline change. Varied document formats are handled by construction —
the model sees full text and layout hints rather than a per-type template. An unreachable
endpoint leaves items `partial` for retry and never blocks search.
