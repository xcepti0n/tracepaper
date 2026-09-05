# DataManager

Deterministic search over personal data on a NAS — documents, photos, and notes.

## The idea

> **Models write. Algorithms read.**

Models run **once per item at ingest time** to extract structured fields, tags, and
embeddings. Search itself is pure SQL + BM25 + vector similarity with a fixed fusion
formula — no model, no prompt, no network call in the query path.

This means:
- The same query returns the same results, every time.
- A new model release is a **re-ingest**, not a rewrite. Query code, API contracts, MCP
  tools, and UI never change.
- Stopping Ollama does not change a single search result.
- "What was my salary in 2023?" is a **field lookup returning a number with a page
  citation**, not a ranked list of PDFs to go read.

## Docs

| Doc | Contents |
|---|---|
| [docs/01-requirements.md](docs/01-requirements.md) | What it must do and why |
| [docs/02-alternatives.md](docs/02-alternatives.md) | What was compared, what was rejected, and why |
| [docs/03-design.md](docs/03-design.md) | Schema, pipeline, query algorithm, build order |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decision log |

## Shape

- **NAS** — the files. Mounted read-only; originals are never modified.
- **Proxmox server** — always on, model-free: SQLite index, query engine, REST API, MCP
  server, web UI, file watcher.
- **Mac** — ingest worker: OCR, embeddings, Ollama. Free to be offline; jobs queue up
  and pending enrichment is shown rather than hidden.

## Status

Design agreed. Implementation starts at M1 (see design doc §7).
