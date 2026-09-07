# Tracepaper

Search over a lifetime of personal documents on a NAS — IDs, passports, visas, leases,
receipts, tax forms, appointments, statements, photos, and notes.

*Tracing paper is the sheet you lay over a document to copy out what matters
without altering the original — and a paper trail is what you follow back to
the source. Both are what this does: your documents are never written to, and
every answer arrives with the citation that proves it.*

## The problem

There is no search today. Finding anything means remembering which file holds it,
locating it by hand, and opening it.

Plain search — keyword or semantic — does not fix this, because **chunking destroys fact
associations**. On a W-2 the tax year sits in a header box and gross salary sits in Box 1.
Chunk it for embeddings and they land in different chunks; no retrieved passage can say
which year that number belongs to, and a model reading it may take the figure off the
wrong form.

## The approach

**Bind facts at ingest, while the whole document is in view. Store them already resolved.**

Ingest reads each document as a whole and writes four layers:

| Layer | What it is | Answers |
|---|---|---|
| **Records** | bound fact groups — `tax_year` + `gross_salary` in *one* row group | "how much tax in 2023" |
| **Events** | things that happened, with date, entities, and every piece of evidence | "when did I last fly Alaska" |
| **Entities** | canonical people/orgs/merchants with aliases collapsed | "everything about Costco" |
| **Passages** | every document chunked, BM25 + embeddings — the floor | anything else, including document types no extractor understands |

Query time is **pure algorithm**: filters, BM25, vector similarity, fixed-formula fusion.
No prompt, no model, no network call.

> **Models write. Algorithms read.**

## What it returns

**Tier 1 — direct answer, no LLM.** The fact is written in a document.
*"What is my passport expiry date?"* → the date, plus document and page.

**Tier 2 — evidence set for an LLM to reason over.** The answer isn't written anywhere.
*"What card is best to pay at Costco?"* → cards held, their benefit terms, prior Costco
spend. An agent on top concludes.

The guarantee is on the **evidence set**: same question, same evidence, same order, every
time. Only the final reasoning varies by model — and that step belongs to the caller.
Tracepaper never generates prose.

## Why it won't go stale

- No fixed schema. Extractors emit whatever keys a document contains; synonyms are
  canonicalized. **A document type never seen before needs zero code.**
- A better model in 2028 is a **re-ingest**. Query code, API, MCP tools, and UI don't
  change.
- Stop Ollama and every search returns identical results.
- With no structured extraction at all, passage search still covers the whole corpus.

## Docs

| Doc | Contents |
|---|---|
| [docs/01-requirements.md](docs/01-requirements.md) | What it must do, and the W-2 case that drives the design |
| [docs/02-alternatives.md](docs/02-alternatives.md) | What was compared and rejected, with reasons |
| [docs/03-design.md](docs/03-design.md) | Schema, pipeline, query algorithm, build order |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decision log |

## Shape

- **Synology NAS** — the documents, mounted read-only. Never modified.
- **Proxmox** — always on, model-free: index, query engine, REST API, web UI, MCP server,
  reconciliation scanner.
- **Mac** — ingest worker: OCR, whole-document extraction, embeddings, and the LLM
  endpoint. Free to be offline; jobs queue and pending enrichment is shown, not hidden.

Change detection is a **background reconciliation scan** — filesystem events don't cross
SMB/NFS. Cheap `(size, mtime)` comparison picks candidates; a **content hash decides**
what actually changed, so a Synology restore or an `rsync` that rewrites mtime doesn't
churn the corpus through re-extraction.

## Status

**All milestones shipped.** 172 tests passing. Ready for you to test.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[formats,semantic,web,ocr-macos]"   # ocr on Linux

cp tracepaper.example.toml tracepaper.toml    # set roots and db_path
.venv/bin/tracepaper scan --index --embed
.venv/bin/tracepaper serve                              # http://127.0.0.1:8823
```

To deploy on Proxmox, run [`deploy/proxmox-install.sh`](deploy/) on the host —
it creates the LXC, mounts both Synology shares, and leaves a running service
behind. See [deploy/README.md](deploy/README.md).
[docs/DEPLOY.md](docs/DEPLOY.md) covers the manual install and VM/bare-metal.

### What it does

```bash
# Tier 1 — a value with a citation, no LLM
tracepaper get gross_salary --where tax_year=2023
tracepaper get expiry_date
tracepaper agg amount sum --where merchant=Costco     # total + every contributing doc

# Events — things that happened, whatever document recorded them
tracepaper events --entity "Alaska Airlines" --last

# Search — keyword and semantic, fused deterministically
tracepaper search "sprinkler valve" --explain          # finds "irrigation solenoid"

# Tier 2 — evidence for an LLM to reason over, never a verdict
tracepaper ask "which card is best at Costco" --entity Costco --json

# Vocabulary discovered from your documents, not declared
tracepaper keys
tracepaper values tax_year

# Corrections outrank every extractor, permanently
tracepaper correct 12 gross_salary 92000.00
tracepaper backup /mnt/nas/backups/human-layer.json    # the irreplaceable part

tracepaper serve            # web UI + REST API
tracepaper-mcp index.db     # 11 typed tools for an agent
```

### Formats

PDF (text layer, OCR when scanned), images and screenshots (OCR), DOCX, XLSX,
CSV/TSV, EML, Markdown, plain text. Anything unrecognised is indexed by
filename rather than rejected, and photos additionally carry EXIF tags.

| Milestone | |
|---|---|
| M1 Floor — scanner, extraction, passages, keyword search | ✅ |
| M2 Records — bound facts, Tier 1 answers, corrections | ✅ |
| M3 Entities and events | ✅ |
| M4 REST API, web UI, MCP server | ✅ |
| M5 Semantic search with RRF fusion | ✅ |
| M6 LLM gap-filling for prose | ✅ |
| M7 Aggregation and Tier 2 evidence | ✅ |
| M8 Photos — EXIF, tags, face clusters | ✅ |
| M9 Hardening — backup, restore, reproducibility | ✅ |
