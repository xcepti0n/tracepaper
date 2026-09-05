-- DataManager index schema.
-- See docs/03-design.md §3. Tables beyond M1 are created now so that later
-- milestones add extractors, not migrations.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- items

CREATE TABLE IF NOT EXISTS items (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('document', 'photo', 'note')),
    uri               TEXT UNIQUE,          -- NULL for native notes
    content_hash      TEXT,
    title             TEXT,
    mime              TEXT,
    size_bytes        INTEGER,
    created_at        TEXT,
    modified_at       TEXT,
    indexed_at        TEXT,                 -- text extraction completed
    enriched_at       TEXT,                 -- record/event extraction completed
    extraction_status TEXT NOT NULL DEFAULT 'pending'
                      CHECK (extraction_status IN ('pending','partial','complete','failed')),
    deleted_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_hash    ON items(content_hash);
CREATE INDEX IF NOT EXISTS idx_items_status  ON items(extraction_status);
CREATE INDEX IF NOT EXISTS idx_items_deleted ON items(deleted_at);

CREATE TABLE IF NOT EXISTS item_versions (
    id           INTEGER PRIMARY KEY,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    content_hash TEXT,
    text         TEXT,
    valid_from   TEXT NOT NULL,
    valid_to     TEXT,                      -- NULL = current version
    UNIQUE (item_id, version)
);

CREATE INDEX IF NOT EXISTS idx_versions_current
    ON item_versions(item_id) WHERE valid_to IS NULL;

-- ------------------------------------------------------- change detection
-- What the scanner last saw on the NAS. Drives FR-9: (size, mtime) selects
-- candidates, content_hash decides what actually changed.

CREATE TABLE IF NOT EXISTS file_state (
    uri             TEXT PRIMARY KEY,
    size_bytes      INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    content_hash    TEXT,
    last_seen_scan  INTEGER NOT NULL,
    last_indexed_at TEXT,
    miss_count      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_file_state_hash ON file_state(content_hash);
CREATE INDEX IF NOT EXISTS idx_file_state_seen ON file_state(last_seen_scan);

CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    root        TEXT NOT NULL,
    seen        INTEGER NOT NULL DEFAULT 0,
    candidates  INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    added       INTEGER NOT NULL DEFAULT 0,
    moved       INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running','complete','failed','interrupted'))
);

-- -------------------------------------------------------------- passages
-- The floor (FR-7): every item is searchable here regardless of whether any
-- extractor understood it.

CREATE TABLE IF NOT EXISTS passages (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    ordinal    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    page       INTEGER,
    char_start INTEGER,
    char_end   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_passages_item ON passages(item_id, version, ordinal);

-- External-content FTS5 over passages. content_rowid ties rows to passages.id
-- so BM25 hits map straight back to provenance.
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
    text,
    title,
    content = 'passages_fts_src',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Materialized source view for the FTS index: passage text plus its item title,
-- so a filename match ranks alongside body text.
CREATE TABLE IF NOT EXISTS passages_fts_src (
    id    INTEGER PRIMARY KEY,   -- mirrors passages.id
    text  TEXT,
    title TEXT
);

-- --------------------------------------------------------------- records
-- Bound fact groups (FR-3). Fields hang off record_id, so tax_year and
-- gross_salary from one document can never be paired with another's.

CREATE TABLE IF NOT EXISTS records (
    id                INTEGER PRIMARY KEY,
    item_id           INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    version           INTEGER NOT NULL,
    record_type       TEXT NOT NULL,          -- open vocabulary
    source            TEXT NOT NULL CHECK (source IN ('human','template','pattern','llm')),
    confidence        REAL NOT NULL DEFAULT 1.0,
    page              INTEGER,
    char_start        INTEGER,
    char_end          INTEGER,
    model_id          TEXT,
    extractor_version TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_item ON records(item_id, version);
CREATE INDEX IF NOT EXISTS idx_records_type ON records(record_type);

CREATE TABLE IF NOT EXISTS record_fields (
    id         INTEGER PRIMARY KEY,
    record_id  INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,               -- open vocabulary
    value_text TEXT,
    value_num  REAL,
    value_date TEXT,                        -- ISO-8601
    unit       TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    char_start INTEGER,
    char_end   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_rf_record ON record_fields(record_id);
CREATE INDEX IF NOT EXISTS idx_rf_key    ON record_fields(key);
CREATE INDEX IF NOT EXISTS idx_rf_num    ON record_fields(key, value_num);
CREATE INDEX IF NOT EXISTS idx_rf_date   ON record_fields(key, value_date);

CREATE TABLE IF NOT EXISTS key_vocabulary (
    key           TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL,
    occurrences   INTEGER NOT NULL DEFAULT 0,
    pinned_by_user INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_vocab_canon ON key_vocabulary(canonical_key);

-- ---------------------------------------------------- entities and events

CREATE TABLE IF NOT EXISTS entities (
    id             INTEGER PRIMARY KEY,
    entity_type    TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    attributes     TEXT,                    -- JSON
    created_at     TEXT NOT NULL,
    UNIQUE (entity_type, canonical_name)
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id        INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    source    TEXT NOT NULL,
    -- Keyed on the surface form: "Costco" and "COSTCO WHOLESALE #1234"
    -- normalise alike but are both worth keeping, since they are what the UI
    -- shows and what explains why two records matched.
    UNIQUE (alias, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_alias_norm ON entity_aliases(alias_norm);

CREATE TABLE IF NOT EXISTS events (
    id                 INTEGER PRIMARY KEY,
    event_type         TEXT NOT NULL,       -- open vocabulary
    occurred_on        TEXT,                -- ISO-8601
    occurred_precision TEXT CHECK (occurred_precision IN ('day','month','year')),
    title              TEXT,
    confidence         REAL NOT NULL DEFAULT 1.0,
    source             TEXT NOT NULL,
    dedup_key          TEXT UNIQUE,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_type_date ON events(event_type, occurred_on);

CREATE TABLE IF NOT EXISTS event_entities (
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role      TEXT,
    PRIMARY KEY (event_id, entity_id, role)
);

CREATE TABLE IF NOT EXISTS event_evidence (
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    record_id  INTEGER REFERENCES records(id) ON DELETE SET NULL,
    passage_id INTEGER REFERENCES passages(id) ON DELETE SET NULL,
    PRIMARY KEY (event_id, item_id, record_id, passage_id)
);

-- ------------------------------------------------------------------ tags

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    namespace  TEXT NOT NULL,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    UNIQUE (item_id, namespace, value, source)
);

CREATE INDEX IF NOT EXISTS idx_tags_ns ON tags(namespace, value);

-- ------------------------------------------------------------------ jobs

CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY,
    item_id    INTEGER REFERENCES items(id) ON DELETE CASCADE,
    type       TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'queued'
               CHECK (state IN ('queued','claimed','done','failed')),
    priority   INTEGER NOT NULL DEFAULT 100,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    claimed_by TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(state, priority, id);

-- At most one outstanding job per (item, type). Completed and failed rows are
-- history and may repeat, so the constraint covers only pending work -- a
-- blanket UNIQUE would make a second indexing pass over a changed file fail.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_pending
    ON jobs(item_id, type) WHERE state IN ('queued', 'claimed');
