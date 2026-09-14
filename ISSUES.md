# Issues

Open work, newest first. Tick a box and move it to **Done** with the commit
that closed it. Anyone (you, me) can add here; keep entries short and say what
"done" looks like so it is obvious when to tick it.

## Open

- [ ] **Photo captions need a vision model running.** Captioning is wired up
      and searchable, but it needs `llm_enabled` and an Ollama endpoint on the
      Mac. Without it, photos are findable only by tag and filename.
      *Done when:* captions exist for the bulk of the photo library.

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

- [x] **Prune and the scanner disagreed, so the index stopped updating.**
      Prune deleted files for "looks like code", the next scan re-added them,
      and the vanish guard then aborted every scan. Being code hides a file
      from search; it is not a reason to stop indexing it. (this commit)
- [x] **A failed scan said "failed" and nothing else.** The reason is stored
      and shown on Status. (this commit)

- [x] **Browse listed field names, led by C++ header noise.** It is a folder
      and file browser now; the field list moved below it, filtered to
      documents, and reads in words. (this commit)
- [x] **Result paths were dead text.** The folder is a link into Browse.
      (this commit)
- [x] **Settings was one 11KB scroll.** Four sections, opening on the one
      with the update button. (`e1fe512`)

- [x] **Settings was read-only documentation.** Folder rules and index
      cleanup are now controls on the page, not commands to copy. (this commit)
- [x] **No way to teach ranking.** Thumbs on each result, keyed on the query
      so one document cannot creep onto unrelated searches. (this commit)
- [x] **Pruning needed a terminal.** Preview and apply are buttons. (this commit)
- [x] **Rules typed through a symlink silently did nothing.** Paths are
      resolved before storing. (this commit)

- [x] **Build output appeared in ordinary results.** A `LICENSE` inside
      `dist/.../typing_extensions-4.14.0.dist-info/` has no extension, so
      extension-based classification called it a document. Code is now decided
      by directory first. (this commit)

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
