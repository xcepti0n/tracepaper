# DataManager — Design

Status: agreed 2026-09-04 (revised)
Implements: [01-requirements.md](01-requirements.md) · Rationale: [02-alternatives.md](02-alternatives.md)

## 1. The shape of the solution

Ingest reads each document **as a whole** and writes four layers. Queries read those
layers with fixed algorithms.

```
                        ONE DOCUMENT (W-2, 2023)
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
        ┌───────────┐     ┌───────────┐      ┌───────────┐
        │  RECORD   │     │  EVENTS   │      │ PASSAGES  │
        │ (bound)   │     │           │      │           │
        │ tax_year  │     │ income    │      │ chunk 1   │
        │  = 2023   │     │ event     │      │ chunk 2   │
        │ employer  │     │ 2023      │      │ chunk 3   │
        │  = ACME   │     │           │      │ +vectors  │
        │ gross_sal │     └─────┬─────┘      └───────────┘
        │  = 84,200 │           │                  │
        └─────┬─────┘           │                  │
              └────────┬────────┴──────────────────┘
                       ▼
                  ┌──────────┐
                  │ ENTITIES │  ACME Corp ← "ACME", "Acme Corporation"
                  └──────────┘
```

`tax_year` and `gross_salary` land in **one record**, bound while the whole document was
in view. Chunking cannot separate them because the binding was made before chunking and
stored resolved. That is the W-2 requirement (FR-3), and it is the reason this design
exists.

Passages are produced **as well**, never instead — they are the floor that keeps
unrecognized document types searchable (FR-7, NFR-9).

## 2. Architecture

```
      NAS (read-only)                        Mac (Apple Silicon)
   ┌──────────────────┐                ┌────────────────────────────┐
   │ documents/photos │                │  Ingest Worker             │
   └────────┬─────────┘                │  ├─ text extract / OCR     │
            │ watch + scan             │  ├─ whole-doc understanding│
            ▼                          │  ├─ record/event/entity    │
   ┌──────────────────────┐  claim job │  ├─ passage embeddings     │
   │ Proxmox (always on)  │◄───────────┤  └─ Ollama (LLM / VLM)     │
   │  ┌────────────────┐  │ write back └────────────────────────────┘
   │  │ Watcher + Queue│  │                  (may be offline)
   │  ├────────────────┤  │
   │  │ Index (SQLite) │  │  records · events · entities
   │  │                │  │  passages · FTS5 · vectors
   │  ├────────────────┤  │
   │  │ Query Engine   │  │  ← NO MODEL. deterministic.
   │  ├───────┬────────┤  │
   │  │ REST  │  MCP   │  │
   │  └───┬───┴───┬────┘  │
   └──────┼───────┼───────┘
          ▼       ▼
       Web UI   Agent ──► (Tier 2: agent reasons over returned evidence)
```

Everything on the Proxmox side is **model-free**. Ollama lives only in the ingest worker.
Stopping it must not change any query result (success criterion 5).

## 3. Data model

### `items`
`id`, `kind` (`document`|`photo`|`note`), `uri` (null for notes), `content_hash`,
`title`, `mime`, `size_bytes`, `created_at`, `modified_at`, `indexed_at`, `enriched_at`,
`extraction_status` (`pending`|`partial`|`complete`|`failed`), `deleted_at`.

Same hash at a new path → update the path, skip re-extraction (FR-9).

### `item_versions`
`item_id`, `version`, `content_hash`, `text`, `valid_from`, `valid_to`. Note edits and
changed-file re-ingest both append (FR-10).

### `records` — bound fact groups (FR-3)
One row per coherent fact-group found in a document. A W-2 produces one; a bank statement
might produce one per transaction.

`id`, `item_id`, `record_type` (open vocabulary — `tax_form`, `passport`, `lease`,
`flight_booking`, …), `source` (`human`|`template`|`pattern`|`llm`), `confidence`,
`page`, `char_start`, `char_end`, `model_id`, `extractor_version`.

### `record_fields` — the bound key/values
`record_id`, `key`, `value_text`, `value_num`, `value_date`, `unit`, `confidence`,
`char_start`, `char_end`.

**Keys are open vocabulary** (FR-4) — whatever the document contained. Typed columns
(`value_num`, `value_date`) are populated when the value parses, which is what makes
range filters and aggregation possible without a fixed schema.

Because fields hang off a `record_id`, "the 2023 gross salary" is a single row group.
There is no join across independently-extracted facts and therefore no way to pair the
wrong year with the wrong number.

### `key_vocabulary` — canonicalization (FR-4)
`key`, `canonical_key`, `occurrences`, `pinned_by_user`.
Extractors invent `gross_pay`, `gross_salary`, `wages`; this table maps them to one
canonical key. Automatic proposals, user-confirmable, and user overrides are permanent.

### `events` (FR-5)
`id`, `event_type` (open vocabulary), `occurred_on`, `occurred_precision`
(`day`|`month`|`year`), `title`, `confidence`, `source`.
`event_entities`: `event_id`, `entity_id`, `role` (`airline`, `merchant`, `landlord`, …).
`event_evidence`: `event_id`, `item_id`, `record_id?`, `passage_id?`.

One flight = one event, with evidence rows for the confirmation email, the boarding pass,
and the statement line. Deduplicated on `(event_type, date, key entities)`.

### `entities` / `entity_aliases` (FR-6)
`entities`: `id`, `entity_type`, `canonical_name`, `attributes` (JSON).
`entity_aliases`: `entity_id`, `alias`, `source`.
`COSTCO WHSE #1234` and `Costco` resolve to one entity. User merges are permanent.

### `passages` (FR-7)
`id`, `item_id`, `version`, `ordinal`, `text`, `page`, `char_start`, `char_end`.
Split on structure (headings, table rows, paragraphs) rather than blind fixed windows.

### `passages_fts`
FTS5 external-content over `passages` plus item title and tag values → BM25.

### `embeddings`
`passage_id`, `vector`, `model_id`, `dim`.
Tagged with `model_id`; **fully rebuildable and safe to drop** (NFR-6).

### `tags`
`item_id`, `namespace` (`person`|`place`|`year`|`occasion`|`doctype`|`topic`), `value`,
`source`, `confidence`. Shared by documents and photos.

### `face_clusters` (FR-11)
`cluster_id`, `name` (null until named), `centroid`. Naming a cluster retroactively names
every photo in it and every future match.

### `jobs`
`id`, `item_id`, `type`, `state`, `attempts`, `last_error`, `claimed_by`, `claimed_at`.
Types: `extract_text`, `ocr`, `extract_records`, `link_entities`, `derive_events`,
`embed`, `photo_exif`, `photo_faces`, `photo_caption`.

Separate job types are what let any single layer be re-run corpus-wide without touching
the others.

### Human-authored layer (the only irreplaceable state, NFR-6)
Rows where `source='human'`, note content, `face_clusters.name`, entity merges, pinned
vocabulary. Backed up separately; everything else regenerates from the NAS.

## 4. Ingest pipeline

```
discover → hash → extract text → understand whole doc → records
                                                      → events
                                                      → entities
                       └────────→ split passages ─────→ embed
```

1. **Discover** — watcher (inotify/FSEvents) plus a nightly reconciliation scan.
2. **Hash** — unchanged hash ends the job. Idempotent re-runs are free.
3. **Text extract** — PDF text layer; OCR (Tesseract, VLM for hard scans) when the layer
   is empty or garbage; CSV/XLSX to normalized rows; DOCX; EML headers and body.
4. **Whole-document understanding** — the layered extractor stack, over the **full
   document text**, never over chunks:
   - **Human corrections** — absolute authority, never overwritten.
   - **Templates** — known layouts (W-2, specific banks, airlines, government IDs).
     Exact, free, instant.
   - **Typed patterns** — dates, amounts, account tails, document numbers. Validated by
     type, not just matched.
   - **LLM (Ollama)** — for anything the layers above did not cover. Prompted for
     *extraction against a JSON shape*, never for judgment or ranking. Output failing
     validation is discarded, not stored.

   All four emit **records** with fields already bound together.
5. **Entity linking** — resolve names in records against `entities`, creating or aliasing.
6. **Event derivation** — rules over records: a `flight_booking` record becomes a flight
   event; a rent receipt becomes a payment event. Dedup against existing events.
7. **Passages + embeddings** — split and embed on the Mac.

Steps 4–7 are independent jobs. Improving the LLM re-runs step 4 only; templates,
patterns, human corrections, entity merges, and cluster names all survive untouched.
That is NFR-1 as a schema property rather than a promise.

## 5. Query engine (deterministic)

Query parsing is **rules over the query string plus the vocabulary the corpus actually
produced** — no model. Because `key_vocabulary` and `entities` are already populated,
mapping *"salary"* onto `gross_salary` or *"Alaska"* onto the airline entity is a lookup.

### Execution

**Step 1 — Parse.** Extract time constraints (`2023`, `last`), entity mentions
(`Alaska Airlines`, `Costco`), key mentions (`salary`, `expiry`), and free text.

**Step 2 — Structured probe (Tier 1).** If the query maps to known keys/event types with
concrete constraints, query records and events directly:

```sql
-- "passport expiry date"
SELECT rf.value_date, r.item_id, r.page
FROM records r JOIN record_fields rf ON rf.record_id = r.id
WHERE r.record_type = 'passport' AND rf.key = 'expiry_date'
ORDER BY r.source_rank, r.confidence DESC;

-- "when did I last fly Alaska"
SELECT e.occurred_on, ev.item_id
FROM events e
JOIN event_entities ee ON ee.event_id = e.id
JOIN event_evidence ev ON ev.event_id = e.id
WHERE e.event_type = 'flight' AND ee.entity_id = :alaska
ORDER BY e.occurred_on DESC LIMIT 1;
```

Both are direct answers with citations, no model, sub-millisecond. The year/salary
pairing is safe because it was bound at ingest into one record.

**Step 3 — Aggregation (FR-8).** Recognized aggregate intent sums over records/events and
returns the total **plus every contributing row**, so the number is auditable.

**Step 4 — Retrieval.** In parallel, always:
- BM25 over `passages_fts` → list A
- Vector search over `embeddings` → list B
- Record/event matches → list C

**Step 5 — Fusion.** Reciprocal Rank Fusion, fixed `k=60`, no learned weights:

```
score(d) = Σ  w_s / (k + rank_s(d))     s ∈ {bm25, vector, structured}
```

Then fixed boosts: exact field match, human-verified, recency, entity match. All
constants live in one config file and are exposed in the UI's "why this ranked here"
panel (FR-13). **Tie-break by `item_id`** so ordering is total and stable (NFR-2).

**Step 6 — Response.** Either a **direct answer** (Tier 1: value + citation) or an
**evidence set** (Tier 2: ranked passages, records, events, entities). Never prose.

### Tier 2 worked example — *"What card is best to pay at Costco?"*

The engine does not answer this. It returns, deterministically:
- entity `Costco` and all linked events (prior spend, with amounts and dates)
- records of type `credit_card` → the cards held
- passages from card terms/benefit documents matching cashback/rewards categories

Same evidence set, same order, every time. The agent on top compares and concludes. The
reasoning varies by model; **the evidence does not** — which is the guarantee that
matters (NFR-2).

## 6. Storage placement

Index lives on the **Proxmox** box (SQLite file), not the NAS — SQLite over SMB/NFS is
unreliable under concurrent access. It is fully rebuildable from the NAS (NFR-6); only
the human-authored layer is backed up separately, to the NAS.

## 7. Technology

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | document/OCR/ML ecosystem |
| Index | SQLite + FTS5 + sqlite-vec | records, text, vectors co-located; ~50 MB RAM, no daemon |
| API | FastAPI + Uvicorn | typed, small |
| MCP | official Python MCP SDK | |
| Queue | SQLite job table | no extra broker |
| PDF | pypdf → pdfplumber → OCR | cheapest path first |
| OCR | Tesseract, VLM fallback | |
| Embeddings | sentence-transformers on Mac | swappable, `model_id`-tagged |
| LLM | Ollama, JSON-shape constrained | swappable, no code change |
| Faces | InsightFace + clustering | deterministic once named |
| UI | server-rendered + htmx | no SPA build |

Every model-touching row is tagged with `model_id` and re-runnable in isolation.

## 8. Interfaces

### MCP tools (stable contracts, FR-12)
- `search(query, filters?, limit?)` → ranked evidence with citations
- `get_value(key, filters)` → Tier 1 direct answer with provenance
- `get_events(type?, entity?, date_range?)` → events with evidence
- `get_entity(name)` → canonical entity, aliases, linked records and events
- `aggregate(key|event_type, op, filters)` → total plus contributing rows
- `get_item(id)` → full record: text, records, events, tags, versions
- `list_keys()` / `list_values(key)` → discovered vocabulary
- `add_note` / `update_note` / `correct_value`

Tools return **data, not prose**. A weaker model only has to pick a tool and read a
value — which is what keeps the contract stable across model generations.

### REST + Web UI
Same engine. UI: search with facets, item detail with highlighted provenance, review
queue for low-confidence records, entity merge, key vocabulary management, face-cluster
naming, note editor, index health.

## 9. Build order

1. **M1 — Floor.** Schema, scanner, hashing, text extraction, passages, FTS5, CLI.
   *Every document searchable by keyword. No models.*
2. **M2 — Records.** Template + pattern extractors, bound records, `get_value`.
   *Passport expiry works, no LLM.*
3. **M3 — Entities + events.** Linking, aliases, event derivation. *"Last Alaska flight"
   works.*
4. **M4 — Interfaces.** REST, MCP, web UI, correction flow.
5. **M5 — Semantic.** Embeddings, vector search, RRF fusion.
6. **M6 — LLM extraction.** Ollama for uncovered documents, open-vocabulary keys,
   canonicalization, review queue. *Coverage extends to arbitrary document types.*
7. **M7 — Aggregation + Tier 2.** Aggregates with audit trails, evidence-set assembly.
8. **M8 — Photos.** EXIF, geocoding, faces, captions.
9. **M9 — Hardening.** Watcher, incremental updates, reconciliation, backup,
   reproducibility suite.

M1 alone replaces "open files by hand" with working search. M2–M3 deliver direct answers
with no LLM anywhere.

## 10. Open questions

- OCR quality threshold that triggers VLM fallback — calibrate on real scans.
- Statement line items: one record per transaction vs. one per statement. Leaning
  per-transaction, for event derivation.
- Event dedup strictness across evidence types (email vs. boarding pass vs. statement).
- Incremental face clustering vs. periodic full recluster.
- `item_versions` retention on high-churn files.
