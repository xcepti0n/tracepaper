# Issues

Open work, newest first. Tick a box and move it to **Done** with the commit
that closed it. Anyone (you, me) can add here; keep entries short and say what
"done" looks like so it is obvious when to tick it.

## Open

- [ ] **Photo captions need a vision model running.** Captioning is wired up
      and searchable, but it needs `llm_enabled` and an Ollama endpoint on the
      Mac. Without it, photos are findable only by tag and filename.
      *Done when:* captions exist for the bulk of the photo library.

- [ ] **Vendored code is still in the index.** `.venv`, `site-packages` and
      `node_modules` were excluded from *future* scans, but files indexed
      before that are still there. Search now hides them behind the Code
      checkbox, so this is size and speed, not correctness.
      *Done when:* a prune pass removes them and the item count drops.

- [ ] **No highlighting inside an opened document.** A result opens the file at
      the right page (`#page=N`), but the matching words are not marked. Needs
      a real viewer (PDF.js) rather than a download.
      *Done when:* clicking a result shows the page with the terms highlighted.

- [ ] **Scan and enrich can starve each other.** They are not mutually
      exclusive at the systemd level, so the 3am scan timer can compete with a
      running backfill. Sequencing them by hand is not a fix.
      *Done when:* the units cannot run at once, and neither is silently
      skipped when the other holds the lock.

- [ ] **Thumbnails are generated per request.** A 24h browser cache, but no
      disk cache, so a cold grid re-decodes every photo.
      *Done when:* thumbnails survive a restart.

- [ ] **NAS access is wider than it needs to be.** The DSM NFS rules for
      `/volume1/homes` and `/volume1/tracepaper` are still in place; the
      `tracepaper` SMB account was meant to replace them.
      *Done when:* those NFS rules are removed and a scan still works.

## Done

- [x] **No second page of results.** Paging over passages repeated documents
      and returned short pages; it now pages over documents. (`31b6dc9`)
- [x] **The Next link never appeared on a semantic search.** The total counted
      FTS matches, missing every document found by meaning alone, so it always
      equalled the page size. (`097b109`)
- [x] **Code files in ordinary results.** A Search mode picker, code excluded
      by default. 72 code files were matching "3d printer" alone. (`31b6dc9`)
- [x] **Photo descriptions were written but never searched.** (`31b6dc9`)

- [x] **Same document repeated across results.** A long manual filled the page
      with its own pages. Grouped by document, best passage representing it.
      (`15787d0`)
- [x] **Junk direct answers.** `{"type": "integer"},` was shown as an answer for
      "3d printer". The banner now has to earn its slot. (`15787d0`)
- [x] **Result links opened JSON** instead of the file. (`79546e4`)
