# DataManager — Requirements

Status: agreed 2026-09-04
Owner: vaiibhav

## 1. Problem

Personal data lives on a NAS as an unstructured pile of files: tax documents, receipts,
invoices, statements, manuals, photos. Finding a specific fact ("what was my salary in
2023", "how much did I pay for the dishwasher, and where") means remembering which file
it is in and opening it by hand.

Handing the whole pile to an LLM agent and letting it browse does not solve this. It is
slow, it is non-reproducible (same question, different answer), it degrades on large
corpora, and the quality of every answer is bound to the quality of whatever model is
current. Rebuilding prompts on every model release is not acceptable.

## 2. Goal

A **deterministic search system** over personal data. Same query, same results, every
time, independent of which model is installed. Models are used to *enrich* data at
ingest time; they are never in the query path.

## 3. Core principle — the ingest/query split

This is the single design rule everything else follows from:

> **Models write. Algorithms read.**

- **Ingest (offline, model-assisted):** extract text, OCR scans, derive structured
  fields, assign tags, compute embeddings. Slow, batched, retryable, tolerant of being
  offline for days. May use an LLM.
- **Query (online, fully deterministic):** SQL filters, BM25 full-text ranking, vector
  similarity, and a fixed fusion formula. No model is loaded, no prompt is evaluated,
  no network call is made. Identical inputs give byte-identical outputs.

Consequence: a better model in 2028 is a **re-ingest**, not a rewrite. The query engine,
the API, the UI, and the agent tool contracts do not change.

## 4. Functional requirements

### FR-1 — Item model
The indexed unit is an **item**, not a file. Two kinds:
- **File-backed** — a PDF, image, CSV, email, office doc on the NAS.
- **Native note** — text typed directly into DataManager (something learned, a fact worth
  keeping). No file on disk; DataManager owns the content.

Both kinds share one identity, tag, field, and search model.

### FR-2 — Document types (initial)
PDF (text + scanned), plain text, Markdown, CSV/TSV, XLSX, DOCX, images
(JPEG/PNG/HEIC), EML. Unknown types are indexed by filename and metadata only, never
rejected.

### FR-3 — Structured field extraction
Documents yield typed key/value facts, not just text: `employer`, `tax_year`,
`gross_salary`, `merchant`, `purchase_date`, `amount`, `currency`, `account_last4`,
`document_type`. Every field carries its extractor, its confidence, and a pointer back
to the exact page/line it came from.

### FR-4 — Answering point queries
"What was my salary in 2023" must resolve to a **field lookup**, not a document list:
one number, with the source document and page cited. Not a ranked list of PDFs to read.

### FR-5 — Incremental and event-driven updates
- New file on the NAS → indexed without a full rescan.
- Changed file → re-indexed, previous version retained.
- Deleted/moved file → index reflects it; move is not re-extraction.
- Full reindex is always available and always safe to run.

### FR-6 — Update and correction
- Native notes are editable in place, with version history.
- Extracted fields are correctable by hand; a human correction outranks any extractor
  and **survives re-ingest** permanently.
- Tags can be added, removed, renamed, and merged.

### FR-7 — Photos
Photos are indexed by derived tags, not stored or copied. Tag sources, independent and
separately re-runnable:
- **EXIF** — timestamp, GPS, camera, orientation. Exact, free, never re-derived.
- **Geocoding** — GPS → place names (city, region, country, POI).
- **Faces** — local embedding + clustering. Clusters are named once by the user; that
  name is permanent and applies to every future photo matched to the cluster.
- **Caption/objects** — a local VLM produces a caption and object tags for the fuzzy
  "occasion" dimension.

Target queries: "photos from Goa in 2019", "photos with <person> at a wedding".

### FR-8 — Two front doors, one engine
- **MCP server** — typed tools for an agent (`search`, `get_field`, `get_item`,
  `list_values`). Tool contracts are stable across model generations.
- **Web UI** — browse, search, correct extractions, name face clusters, write notes.

Both call the same query engine. Neither may embed model calls.

### FR-9 — Ranking
Results are ranked by a fixed, documented, inspectable formula. The UI can show why a
result ranked where it did. No learned reranker in the default query path.

## 5. Non-functional requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | **Model independence.** No prompt, model name, or model output shape is referenced at query time. Swapping the ingest model changes data, never code or contracts. |
| NFR-2 | **Reproducibility.** A query run twice on an unchanged index returns identical, identically-ordered results. |
| NFR-3 | **Privacy.** Default is fully local. No document content leaves the LAN unless explicitly enabled per-source. |
| NFR-4 | **Availability.** The search service stays up when the ingest machine is off. Pending enrichment is visible, never silently missing. |
| NFR-5 | **Scale.** 10k–100k items comfortably; photos may dominate the count. |
| NFR-6 | **Recoverability.** The index is fully rebuildable from the NAS plus a small human-authored layer (notes, corrections, face names). That layer is backed up separately and is the only irreplaceable state. |
| NFR-7 | **Read-only source.** DataManager never writes to, moves, or renames NAS originals. |
| NFR-8 | **Resource budget.** Always-on footprint on the Proxmox host: < 1 GB RAM, no GPU, no JVM. |

## 6. Deployment context

- **NAS** — the files. Mounted read-only (SMB/NFS).
- **Proxmox server** — 16 GB RAM, already running other services. Hosts the always-on
  layer: database, search API, web UI, MCP server, file watcher. No models.
- **Mac (Apple Silicon)** — hosts Ollama and other model work. Runs the ingest worker,
  which pulls jobs from a queue. Expected to be intermittently offline; this is normal
  operation, not an outage.

## 7. Explicit non-goals

- Not a document management system. Originals stay where they are, untouched.
- Not a chat interface. DataManager returns data; an agent may narrate it.
- Not a general web search or RAG-over-the-internet tool.
- No multi-user accounts, sharing, or permissions in v1. Single trusted user on a LAN.
- No cloud dependency in the default path.

## 8. Success criteria

1. "What was my salary in 2023?" → a number, with source document and page, in under a
   second, with no model loaded.
2. A new file dropped on the NAS is keyword-searchable within a minute and
   field-searchable once the ingest worker next runs.
3. Ollama is stopped and every query in the test suite still returns identical results.
4. The extraction model is swapped for a different one, ingest is re-run, and no query
   code, API contract, or UI code changes.
5. A hand-corrected field still holds its corrected value after a full reindex.
