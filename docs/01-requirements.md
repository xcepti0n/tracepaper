# DataManager — Requirements

Status: agreed 2026-09-04 (revised)
Owner: vaiibhav

## 1. Problem

A lifetime of personal documents sits on a NAS: ID cards, passports, visas, leases, rent
receipts, tax forms, appointments, flight confirmations, card statements, manuals,
warranties, photos. There is **no search today**. Finding anything means remembering
which file holds it, locating that file by hand, and opening it.

Two things that do not solve this:

- **Letting an agent browse the files.** Slow, non-reproducible, degrades on large
  corpora, and every answer is bound to whatever model is current.
- **Plain search over document text** — keyword or semantic. It retrieves *passages that
  look relevant*, which is not the same as the fact being asked for. Worked example
  below.

### Why plain search fails — the W-2 case

On a W-2, the tax year sits in a header box and gross salary sits in Box 1. They are
physically far apart with unrelated boxes between them. Chunk the document for
embeddings and the two land in different chunks. Search *"2023 salary"* and you match a
chunk holding one or the other; the retrieved passage cannot say which year the number
belongs to. A model reading it may take the number off the wrong form entirely.

**The association between `tax_year` and `gross_salary` has to be made once, at ingest,
while the whole document is in view — and stored already resolved.** No amount of
query-time cleverness recovers a link that was destroyed by chunking.

This is the central requirement. Everything else follows from it.

## 2. Goal

Transform an unstructured pile into a **searchable, queryable representation**, so that:

- Facts that are written in a document are **returned directly**, with a citation, and
  with no model involved.
- Questions that require reasoning get a **complete, reproducible evidence set** to
  reason over.

The transformation is the product. The stored index is a rebuildable by-product.

## 3. The contract — two tiers

### Tier 1 — Direct answer (no LLM)
The answer is a value present in a document.

> *"What is my passport expiry date?"* · *"How much tax did I pay in 2023?"* ·
> *"When did I last fly Alaska Airlines?"*

Returns the **value plus its citation** (document, page, passage). Deterministic, no
model loaded. If several documents could answer, all candidates are returned ranked, not
silently collapsed to one.

### Tier 2 — Evidence for reasoning (LLM on top)
The answer is not written anywhere and must be worked out.

> *"What card is best for me to pay at Costco?"*

Returns **all relevant context**: which cards are held, their benefit terms, prior Costco
spend. An LLM — or the user — draws the conclusion.

**The guarantee is on the evidence set, not the prose.** The same question returns the
same evidence, in the same order, every time. Only the final reasoning step varies by
model, and it is the caller's, not DataManager's.

DataManager never generates prose. It returns values and evidence.

## 4. Core principle

> **Models write. Algorithms read.**

- **Ingest (offline, model-assisted):** understand each document as a whole; bind related
  facts together; emit records, events, entities, passages, embeddings. Slow, batched,
  retryable, may use an LLM.
- **Query (online, deterministic):** filters, BM25, vector similarity, fixed fusion. No
  prompt evaluated, no model loaded, no network call. Identical inputs, identical
  outputs.

A better model in 2028 is a **re-ingest**. Query code, API contracts, MCP tools, and UI
do not change.

## 5. Functional requirements

### FR-1 — Item model
The indexed unit is an **item**: a file-backed document or photo, or a **native note**
typed directly into DataManager. One identity, tag, and search model for both.

### FR-2 — Document types
PDF (text and scanned), images (JPEG/PNG/HEIC), plain text, Markdown, CSV/TSV, XLSX,
DOCX, EML. Unknown types are indexed by filename and metadata — never rejected.

### FR-3 — Whole-document binding (the W-2 requirement)
Extraction operates on the **full document**, not on chunks. Facts that belong together
are bound into one **record** at ingest: a W-2 yields a single record carrying
`tax_year`, `employer`, `gross_salary`, `federal_withheld` together. A passport yields
one record with `passport_number`, `expiry_date`, `nationality`.

Once bound, retrieving one field never risks pairing it with another document's value.

### FR-4 — Open vocabulary
There is **no fixed list of field keys**. Extractors emit whatever facts a document
actually contains. A document type never seen before yields records without new code.
Synonymous keys discovered across the corpus are canonicalized over time; the user can
rename and merge keys.

Rationale: a lifetime of heterogeneous documents has no enumerable schema. A closed
schema means code changes forever — the exact treadmill this project exists to avoid.

### FR-5 — Events
Many questions ask about something that **happened**, where the document is merely where
it was recorded. A flight is an event whether the evidence is a confirmation email, a
boarding pass PDF, or a card statement line.

Events carry `type`, `date`, participating entities, and links to every piece of
supporting evidence. *"When did I last fly Alaska"* is: filter events, sort by date,
return with citation.

### FR-6 — Entities and aliases
People, organizations, merchants, places, and accounts are canonical entities. Aliases
collapse *COSTCO WHSE #1234* on a statement to the same entity as *Costco* in an email.
Required for "everything about X" and for Tier 2 evidence gathering.

### FR-7 — Passage layer
Every item is also split into passages, each independently retrievable by keyword and by
embedding. This is the **floor**: any document, including types no extractor understands,
is searchable at the passage level. When structured extraction is thin, the system
degrades to good search rather than to nothing.

### FR-8 — Aggregation
Sum, count, min, max, and latest over records and events — *"how much tax did I pay in
2023"*, *"total rent paid last year"*. Every contributing document is listed so the
number can be audited.

### FR-9 — Incremental updates
New file indexed without a full rescan. Changed file re-indexed with prior version
retained. Moved file detected by content hash — a path change, not re-extraction.
Deleted file soft-deleted. Full reindex always available and always safe.

### FR-10 — Correction
Native notes editable with version history. Extracted values correctable by hand; a
human correction outranks every extractor and **survives re-ingest permanently**. Tags
and keys can be added, renamed, and merged.

### FR-11 — Photos
Indexed by derived tags; images are never copied or stored. Tag sources, independently
re-runnable: **EXIF** (timestamp, GPS, camera — exact), **geocoding** (GPS → place
names), **face clustering** (named once by the user, permanent thereafter), **VLM
caption** for the fuzzy "occasion" dimension.

### FR-12 — Two front doors, one engine
**MCP tools** for agents and a **web UI** for the user, both over the same query engine.
Neither embeds model calls. Tool contracts stay stable across model generations.

### FR-13 — Ranking
Fixed, documented, inspectable formula. The UI can show why a result ranked where it
did. No learned reranker in the default query path.

## 6. Non-functional requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | **Model independence.** No prompt, model name, or model-output shape is referenced at query time. Swapping the ingest model changes data, never code or contracts. |
| NFR-2 | **Reproducibility.** A query run twice against an unchanged index returns identical, identically-ordered results — including Tier 2 evidence sets. |
| NFR-3 | **Privacy.** Fully local by default. No document content leaves the LAN unless explicitly enabled per-source. |
| NFR-4 | **Availability.** Search stays up when the ingest machine is off. Pending enrichment is visible, never silently missing. |
| NFR-5 | **Scale.** 10k–100k items; photos may dominate the count. |
| NFR-6 | **Recoverability.** The index is fully rebuildable from the NAS plus a small human-authored layer (notes, corrections, face names, entity merges). That layer is the only irreplaceable state and is backed up separately. |
| NFR-7 | **Read-only source.** NAS originals are never modified, moved, or renamed. |
| NFR-8 | **Resource budget.** Always-on footprint: < 1 GB RAM, no GPU, no JVM. |
| NFR-9 | **Graceful degradation.** With no structured extraction at all, passage search still works over the entire corpus. |

## 7. Deployment context

- **NAS** — the documents. Mounted read-only.
- **Proxmox server** — 16 GB, already running other services. Always-on layer: index,
  query engine, REST API, web UI, MCP server, file watcher. **No models.**
- **Mac (Apple Silicon)** — Ollama and the ingest worker. Expected to be intermittently
  offline; that is normal operation, not an outage.

## 8. Non-goals

- Not a document management system — originals stay untouched.
- Not a chat interface. DataManager returns values and evidence; callers narrate.
- Tier 2 conclusions are **out of scope for the engine** — it supplies evidence, not
  verdicts.
- No multi-user accounts or permissions in v1.
- No cloud dependency in the default path.

## 9. Success criteria

1. *"Passport expiry date"* → the date, with document and page, no model loaded.
2. *"How much tax did I pay in 2023"* → a number, plus every document that contributed.
3. *"When did I last fly Alaska"* → a date and the source, regardless of whether the
   evidence was an email, a PDF, or a statement line.
4. *"Best card at Costco"* → cards held, their relevant terms, and prior Costco spend —
   the same evidence set every time.
5. Ollama stopped: every query in the test suite returns identical results.
6. Extraction model swapped and re-ingested: no query code, contract, or UI change.
7. A hand-corrected value survives a full reindex.
8. A document type no extractor understands is still findable by passage search.
