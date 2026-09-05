# DataManager

Search over a lifetime of personal documents on a NAS — IDs, passports, visas, leases,
receipts, tax forms, appointments, statements, photos, and notes.

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
DataManager never generates prose.

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

- **NAS** — the documents, mounted read-only. Never modified.
- **Proxmox** — always on, model-free: index, query engine, REST API, web UI, MCP server,
  file watcher.
- **Mac** — ingest worker: OCR, whole-document extraction, embeddings, Ollama. Free to be
  offline; jobs queue and pending enrichment is shown, not hidden.

## Status

Design agreed. Implementation starts at M1 (design doc §9).

M1 alone replaces "open files by hand" with working search over everything.
M2–M3 add direct answers with no LLM involved.
