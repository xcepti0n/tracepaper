# DataManager — Design

Status: agreed 2026-09-04
Implements: [01-requirements.md](01-requirements.md) · Rationale: [02-alternatives.md](02-alternatives.md)

## 1. Architecture

```
        NAS (read-only mount)                    Mac (Apple Silicon)
     ┌────────────────────────┐              ┌──────────────────────────┐
     │  /documents  /photos   │              │   Ingest Worker          │
     └───────────┬────────────┘              │   ├─ text extract / OCR  │
                 │ watch + scan              │   ├─ field extractors    │
                 ▼                           │   ├─ embeddings          │
     ┌────────────────────────┐   claim job  │   └─ Ollama (LLM/VLM)    │
     │  Proxmox (always on)   │◄─────────────┤                          │
     │  ┌──────────────────┐  │   write back └──────────────────────────┘
     │  │  Scanner/Watcher │  │                    (may be offline)
     │  ├──────────────────┤  │
     │  │  Job Queue       │  │
     │  ├──────────────────┤  │
     │  │  SQLite index    │  │  ← fields · FTS5 · vectors · tags
     │  ├──────────────────┤  │
     │  │  Query Engine    │  │  ← NO MODEL, deterministic
     │  ├────────┬─────────┤  │
     │  │ REST   │  MCP    │  │
     │  └───┬────┴────┬────┘  │
     └──────┼─────────┼───────┘
            ▼         ▼
        Web UI     Agent
```

The dashed boundary matters: **everything on the Proxmox side is model-free.** Ollama
exists only inside the ingest worker. Stopping Ollama must not change any query result —
that is success criterion 3.

## 2. Data model

### `items`
The central table. One row per document, photo, or note.

| Column | Notes |
|---|---|
| `id` | stable primary key |
| `kind` | `document` \| `photo` \| `note` |
| `uri` | NAS path, or `null` for native notes |
| `content_hash` | SHA-256 of bytes; drives change detection and dedup |
| `title` | filename, or user-set for notes |
| `mime`, `size_bytes`, `created_at`, `modified_at` | filesystem metadata |
| `indexed_at` | when text extraction last completed |
| `enriched_at` | when field/tag extraction last completed |
| `extraction_status` | `pending` \| `partial` \| `complete` \| `failed` (NFR-4) |
| `deleted_at` | soft delete; a vanished file is not destroyed data |

Move detection: same `content_hash`, new `uri` → update the path only, skip
re-extraction (FR-5).

### `item_versions`
Every content change appends a row: `item_id`, `version`, `content_hash`, `text`,
`valid_from`, `valid_to`. Serves note editing and re-ingest of changed files (FR-6),
and makes "what did this look like before" answerable.

### `fields` — the structured layer, and the heart of FR-4
| Column | Notes |
|---|---|
| `item_id` | |
| `key` | `gross_salary`, `tax_year`, `merchant`, `amount`, … |
| `value_text` | canonical string form |
| `value_num`, `value_date` | typed forms, populated when applicable — these make range queries and aggregation possible |
| `unit` | currency code etc. |
| `source` | `human` \| `template` \| `pattern` \| `llm` |
| `confidence` | 0.0–1.0 |
| `page`, `char_start`, `char_end` | provenance — where in the document (FR-3) |
| `model_id` | which model produced it, `null` for non-LLM sources |

Resolution order when several extractors produce the same key:
`human` > `template` > `pattern` > `llm`, then higher confidence.
A `human` row is never overwritten by re-ingest — that is success criterion 5.

Typed columns are what let a point query be a lookup rather than a search:

```sql
SELECT value_num, item_id, page FROM fields
WHERE key='gross_salary' AND item_id IN (
  SELECT item_id FROM fields WHERE key='tax_year' AND value_num=2023
)
ORDER BY CASE source WHEN 'human' THEN 0 WHEN 'template' THEN 1
                     WHEN 'pattern' THEN 2 ELSE 3 END, confidence DESC
LIMIT 1;
```

That is the whole of "what was my salary in 2023" — no model, sub-millisecond, and it
cites its page.

### `tags`
`item_id`, `namespace`, `value`, `source`, `confidence`.
Namespaces keep facets clean and queryable: `person`, `place`, `year`, `occasion`,
`doctype`, `topic`. Photos and documents share this table.

### `entities` / `entity_aliases`
Canonical people, employers, merchants, places. Aliases collapse *ACME Corp* / *Acme
Corporation* / *ACME* to one entity, so a query for one finds all. Human-curated,
therefore permanent.

### `face_clusters`
`cluster_id`, `name` (null until you name it), `centroid`. Photo faces link to clusters.
Naming a cluster retroactively names every photo in it and every future match (FR-7).

### `items_fts` — FTS5 virtual table
Full text plus title and tag values, external-content against `item_versions`. Provides
BM25.

### `embeddings`
`item_id`, `chunk_index`, `vector`, `model_id`, `dim`, `chunk_text`.
Tagged with `model_id` so a model change is a targeted rebuild. **Fully derivable —
this table can be dropped and recomputed** (per NFR-6 and decision D).

### `jobs`
`id`, `item_id`, `type`, `state`, `attempts`, `last_error`, `claimed_by`, `claimed_at`.
Types: `extract_text`, `ocr`, `extract_fields`, `embed`, `photo_exif`, `photo_faces`,
`photo_caption`. Independent job types are what make each layer separately re-runnable.

### Human-authored layer (the only irreplaceable state, NFR-6)
`fields` where `source='human'`, note content in `item_versions`, `face_clusters.name`,
`entities`, and tags where `source='human'`. Backed up separately from the index; the
rest is regenerable from the NAS.

## 3. Ingest pipeline

```
discover → hash → text extract → chunk → [field extractors] → [embed] → index
```

1. **Discover** — watcher (inotify/FSEvents) plus a nightly reconciliation scan to catch
   anything the watcher missed.
2. **Hash** — unchanged hash ends the job immediately. Idempotent re-runs are free.
3. **Text extract** — per MIME type: PDF text layer; OCR (Tesseract, or a local VLM for
   hard scans) when the text layer is empty or garbage; CSV/XLSX to normalized rows;
   DOCX; EML headers plus body.
4. **Field extraction** — the C3 layer stack: template → pattern → LLM-for-gaps. The LLM
   step is schema-constrained JSON; output that fails validation is dropped, never
   stored.
5. **Embed** — chunk (~512 tokens, overlapping) and embed on the Mac.
6. **Index** — write fields, tags, FTS rows, vectors; set `extraction_status`.

Steps 3–6 are separate job rows, so a partial failure retries in isolation and any
single stage can be re-run corpus-wide without touching the others.

**Photos** follow the same spine with a different stage set: EXIF → geocode → face
embed/cluster → VLM caption. EXIF is exact and never re-derived.

## 4. Query engine (deterministic)

A query is parsed by **rules, not a model**:

```
"salary 2023"        → field probe: key~salary, tax_year=2023
"receipts over $500" → filter: doctype=receipt, amount > 500
"photos goa 2019"    → filter: kind=photo, place=goa, year=2019
"sprinkler valve"    → free text → BM25 + vector
```

### Execution
1. **Structured probe.** If the query maps to known field keys with concrete constraints,
   run the SQL lookup. A confident hit returns a **direct answer** with citation, plus
   supporting items. This is the FR-4 path.
2. **Filters.** Any parsed facets (`year`, `doctype`, `person`, `place`) become SQL
   `WHERE` clauses that constrain everything below.
3. **BM25** over FTS5 on the remaining free text → ranked list A.
4. **Vector search** over embeddings, same filters applied → ranked list B.
5. **Fusion — Reciprocal Rank Fusion**, fixed `k=60`, no learned weights:

   ```
   score(d) = Σ  w_s / (k + rank_s(d))        s ∈ {bm25, vector}
   ```

   Then fixed deterministic boosts: exact field match, recency, human-verified fields.
   All constants live in one config file and are shown in the UI's "why this ranked
   here" panel (FR-9).
6. **Tie-break by `item_id`** so ordering is total and stable — this is what makes
   NFR-2 literally true rather than approximately true.

No step calls a model. Ollama being down changes nothing here.

## 5. Interfaces

### MCP tools (stable contracts, FR-8)
- `search(query, filters?, limit?)` → ranked items with snippets and scores
- `get_field(key, filters)` → typed value(s) with provenance — the point-query tool
- `get_item(id)` → full record: text, fields, tags, versions
- `list_values(key, filters?)` → distinct values, for "which years do I have?"
- `aggregate(key, op, filters)` → sum/avg/count/min/max — "total spent on appliances"
- `add_note(title, text, tags?)` / `update_note(id, text)`
- `correct_field(item_id, key, value)` → writes a `human` field

Deliberately, these tools return **data, not prose**. The agent narrates; DataManager
never does. That is what keeps the contract stable across model generations — and lets
a weaker model still get exact answers, since it only has to pick a tool and read a
number.

### REST + Web UI
Same engine behind `/api/search`, `/api/items/{id}`, `/api/fields`, `/api/notes`.
UI: search with facets, item detail with highlighted field provenance, an extraction
review queue for low-confidence fields, face-cluster naming, note editor, and index
health (pending jobs, failures, last scan).

## 6. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | best document/OCR/ML ecosystem |
| DB | SQLite + FTS5 + sqlite-vec | one file, no daemon, all three search modes co-located (decision B) |
| API | FastAPI + Uvicorn | typed, small, async |
| MCP | official Python MCP SDK | |
| Queue | SQLite-backed job table | no extra broker to run |
| PDF | pypdf → pdfplumber → OCR fallback | cheapest path first |
| OCR | Tesseract, VLM fallback for hard scans | |
| Embeddings | local sentence-transformers on the Mac | swappable, `model_id`-tagged |
| LLM | Ollama on the Mac, JSON-schema constrained | swappable without code change |
| Faces | InsightFace embeddings + clustering | deterministic once clusters are named |
| UI | server-rendered + htmx | no SPA build to maintain |

Every model-touching row above is data-tagged with its `model_id` and re-runnable in
isolation. That is the mechanism behind NFR-1 — not a promise, a schema property.

## 7. Build order

1. **M1 — Skeleton.** Schema, scanner, hashing, text extraction for PDF/txt/md/CSV,
   FTS5 keyword search, CLI. *Searchable corpus, zero models.*
2. **M2 — Structured fields.** Template + pattern extractors, `fields` table, point-query
   path, `get_field`. *"Salary in 2023" works.*
3. **M3 — Interfaces.** REST API, MCP server, web UI, correction flow.
4. **M4 — Semantic layer.** Chunking, embeddings, vector search, RRF fusion.
5. **M5 — Photos.** EXIF, geocoding, face clustering + naming, VLM captions.
6. **M6 — LLM gap-filling.** Ollama extractor for uncovered documents, confidence
   thresholds, review queue.
7. **M7 — Hardening.** Watcher, incremental updates, reconciliation, backup of the
   human-authored layer, reproducibility test suite.

M1–M3 deliver the core promise with no model involved anywhere. Semantic search and LLM
extraction arrive as enhancements to a system that already works without them — which is
the point.

## 8. Open questions

- OCR quality threshold that triggers the VLM fallback — needs calibration on real scans.
- Whether CSV/XLSX rows should become individual items or stay one item with row-level
  provenance. Leaning: one item, row-level provenance.
- Face clustering re-run policy as new photos arrive (incremental assign vs. periodic
  full recluster).
- Retention for `item_versions` on high-churn files.
