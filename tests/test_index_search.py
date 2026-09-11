"""Indexing and deterministic search (FR-7, FR-13, NFR-1, NFR-2)."""

from __future__ import annotations

from pathlib import Path

from tracepaper import notes
from tracepaper.index.indexer import Indexer
from tracepaper.query.search import SearchEngine, to_fts_query
from tracepaper.scan.scanner import Scanner


def build(conn, cfg, nas: Path, files: dict[str, str]):
    for name, body in files.items():
        path = nas / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    Scanner(conn, cfg).scan(nas)
    return Indexer(conn, cfg).run_pending()


def test_index_creates_passages_and_versions(conn, cfg, nas):
    result = build(conn, cfg, nas, {"a.txt": "alpha content here"})

    assert result.indexed == 1
    assert result.passages >= 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM item_versions WHERE valid_to IS NULL"
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT extraction_status FROM items"
    ).fetchone()["extraction_status"] == "complete"


def test_search_finds_indexed_content(conn, cfg, nas):
    build(conn, cfg, nas, {
        "tax2023.txt": "Employer ACME Corp. Gross salary 84200 for tax year 2023.",
        "recipe.txt": "Mix flour and water, then bake.",
    })

    response = SearchEngine(conn).search("gross salary")

    assert len(response.hits) == 1
    assert response.hits[0].title == "tax2023.txt"
    assert "84200" in response.hits[0].snippet


def test_search_matches_filename(conn, cfg, nas):
    """A filename is often the only searchable thing on a scanned document."""
    build(conn, cfg, nas, {"passport_renewal.txt": "unrelated body text"})

    response = SearchEngine(conn).search("passport")

    assert len(response.hits) == 1
    assert "title_match" in response.hits[0].signals


def test_scanned_pdf_still_findable_by_filename(conn, cfg, nas):
    """An item with no extractable text keeps a title-only FTS row (FR-2)."""
    (nas / "W2_2023_scan.bin").write_bytes(b"\x00\x01binary scan")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    response = SearchEngine(conn).search("W2_2023_scan")

    assert len(response.hits) == 1


def test_results_are_reproducible(conn, cfg, nas):
    """NFR-2: identical inputs, identical ordering, every time."""
    build(conn, cfg, nas, {
        f"doc{i}.txt": f"shared term appears here, document number {i}"
        for i in range(12)
    })

    engine = SearchEngine(conn)
    runs = [[(h.passage_id, round(h.score, 9))
             for h in engine.search("shared term", limit=12).hits]
            for _ in range(5)]

    assert all(run == runs[0] for run in runs)
    assert len(runs[0]) == 12


def test_ranking_is_totally_ordered_on_tied_scores(conn, cfg, nas):
    """Identical documents score identically; passage_id must break the tie."""
    build(conn, cfg, nas, {f"same{i}.txt": "identical body text" for i in range(6)})

    hits = SearchEngine(conn).search("identical body", limit=6).hits
    ids = [h.passage_id for h in hits]

    assert ids == sorted(ids), "tied scores must fall back to passage_id order"


def test_deleted_items_are_excluded(conn, cfg, nas):
    build(conn, cfg, nas, {"gone.txt": "findme content", "here.txt": "findme too"})
    conn.execute("UPDATE items SET deleted_at = '2026-01-01' WHERE title = 'gone.txt'")

    response = SearchEngine(conn).search("findme")

    assert len(response.hits) == 1
    assert response.hits[0].title == "here.txt"


def test_reindex_replaces_passages_without_orphans(conn, cfg, nas):
    """The FTS5 external-content delete path -- old rows must not survive."""
    path = nas / "a.txt"
    build(conn, cfg, nas, {"a.txt": "original distinctive wording"})

    path.write_text("completely different phrasing now")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    engine = SearchEngine(conn)
    assert len(engine.search("distinctive wording").hits) == 0, "stale FTS rows remain"
    assert len(engine.search("different phrasing").hits) == 1

    # The FTS index and its source table must stay in step.
    src = conn.execute("SELECT COUNT(*) AS n FROM passages_fts_src").fetchone()["n"]
    live = conn.execute("SELECT COUNT(*) AS n FROM passages").fetchone()["n"]
    assert src == live


def test_fts_integrity_after_repeated_reindex(conn, cfg, nas):
    path = nas / "churn.txt"
    build(conn, cfg, nas, {"churn.txt": "revision 0 text"})
    for i in range(1, 5):
        path.write_text(f"revision {i} text with different words {i * 'x'}")
        Scanner(conn, cfg).scan(nas)
        Indexer(conn, cfg).run_pending()

    conn.execute("INSERT INTO passages_fts(passages_fts) VALUES('integrity-check')")
    assert len(SearchEngine(conn).search("revision 4").hits) >= 1


def test_versions_accumulate_on_change(conn, cfg, nas):
    path = nas / "a.txt"
    build(conn, cfg, nas, {"a.txt": "v1 body"})
    path.write_text("v2 body")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    rows = conn.execute(
        "SELECT version, valid_to FROM item_versions ORDER BY version"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["valid_to"] is not None
    assert rows[1]["valid_to"] is None


def test_moved_file_is_not_reindexed(conn, cfg, nas):
    build(conn, cfg, nas, {"a.txt": "stable content", "b.txt": "other content"})
    before = conn.execute(
        "SELECT indexed_at FROM items WHERE title = 'a.txt'"
    ).fetchone()["indexed_at"]

    (nas / "sub").mkdir()
    (nas / "a.txt").rename(nas / "sub" / "a.txt")
    Scanner(conn, cfg).scan(nas)
    result = Indexer(conn, cfg).run_pending()

    assert result.processed == 0, "a move must not queue extraction"
    assert conn.execute(
        "SELECT indexed_at FROM items WHERE title = 'a.txt'"
    ).fetchone()["indexed_at"] == before


# ----------------------------------------------------------------- notes

def test_note_is_searchable(conn, cfg, nas):
    item_id = notes.create_note(conn, "Sprinkler repair",
                                "Replaced the irrigation solenoid in the front zone.")
    Indexer(conn, cfg).run_pending()

    response = SearchEngine(conn).search("irrigation solenoid")

    assert len(response.hits) == 1
    assert response.hits[0].item_id == item_id


def test_note_edit_creates_version_and_reindexes(conn, cfg, nas):
    item_id = notes.create_note(conn, "Note", "first revision wording")
    Indexer(conn, cfg).run_pending()

    notes.update_note(conn, item_id, "second revision wording")
    Indexer(conn, cfg).run_pending()

    engine = SearchEngine(conn)
    assert len(engine.search("first revision").hits) == 0
    assert len(engine.search("second revision").hits) == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM item_versions WHERE item_id = ?", (item_id,)
    ).fetchone()["n"] == 2


# ------------------------------------------------------- query sanitising

def test_fts_query_quotes_tokens():
    assert to_fts_query("hello world") == '"hello" "world"'


def test_fts_operators_are_preserved():
    assert to_fts_query("tax AND 2023") == '"tax" AND "2023"'


def test_fts_special_characters_do_not_break_search(conn, cfg, nas):
    """User punctuation is literal, not FTS syntax."""
    build(conn, cfg, nas, {"a.txt": "account ending 1234 was charged"})
    engine = SearchEngine(conn)

    for query in ['account "1234"', "account (1234)", "account*", "account: 1234",
                  "^account", "acc-ount", "''", "*", "()"]:
        engine.search(query)  # must not raise


def test_empty_query_returns_nothing(conn, cfg, nas):
    build(conn, cfg, nas, {"a.txt": "content"})
    assert SearchEngine(conn).search("   ").hits == []
    assert SearchEngine(conn).search("!!!").hits == []


def test_unicode_and_diacritics(conn, cfg, nas):
    build(conn, cfg, nas, {"visa.txt": "Résumé for visa café application"})
    engine = SearchEngine(conn)
    assert len(engine.search("resume").hits) == 1, "diacritics should fold"
    assert len(engine.search("Résumé").hits) == 1


def test_no_recency_boost_when_corpus_shares_one_timestamp(conn, cfg, nas):
    """Files copied onto the NAS in one operation share an mtime.

    A boost every document receives equally ranks nothing -- it only inflates
    scores and makes --explain misleading.
    """
    build(conn, cfg, nas, {f"doc{i}.txt": "shared searchable term" for i in range(3)})
    conn.execute("UPDATE items SET modified_at = '2026-01-01T00:00:00+00:00'")

    hits = SearchEngine(conn).search("shared searchable").hits

    assert hits
    assert all("recency" not in h.signals for h in hits)


def test_recency_boost_applies_across_a_real_span(conn, cfg, nas):
    build(conn, cfg, nas, {"old.txt": "shared searchable term",
                           "new.txt": "shared searchable term"})
    conn.execute("UPDATE items SET modified_at = '2020-01-01T00:00:00+00:00' "
                 "WHERE title = 'old.txt'")
    conn.execute("UPDATE items SET modified_at = '2026-01-01T00:00:00+00:00' "
                 "WHERE title = 'new.txt'")

    hits = SearchEngine(conn).search("shared searchable").hits
    by_title = {h.title: h for h in hits}

    assert by_title["new.txt"].signals.get("recency", 0) > 0
    assert by_title["old.txt"].signals.get("recency", 0) == 0
    assert by_title["new.txt"].score > by_title["old.txt"].score


def test_snippets_are_single_line(conn, cfg, nas):
    build(conn, cfg, nas, {"multi.txt": "first line here\nsecond line\n\nthird line"})
    hits = SearchEngine(conn).search("second line").hits
    assert hits
    assert "\n" not in hits[0].snippet


def test_list_keys_skips_the_join_when_nothing_is_deleted(conn, cfg, nas):
    """The two joins in list_keys exist only to hide fields of deleted items,
    and they are the whole cost: grouping a million record_fields through them
    is ~465ms and two temp B-trees, against ~48ms on the table alone. Deletes
    are rare, so the join is skipped when there are none -- but the answer has
    to be identical, not merely close."""
    from tracepaper.query.fields import FieldQuery

    conn.execute("INSERT INTO items (kind, uri, extraction_status) "
                 "VALUES ('document', '/a', 'complete')")
    item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO records (item_id, version, record_type, source, "
                 "created_at) VALUES (?, 1, 'payslip', 'pattern', '2026-01-01')",
                 (item_id,))
    record_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for key in ("gross_salary", "gross_salary", "tax_year"):
        conn.execute("INSERT INTO record_fields (record_id, key, value_text) "
                     "VALUES (?, ?, 'x')", (record_id, key))
    conn.commit()

    fq = FieldQuery(conn)
    fast = fq.list_keys()

    joined = [(r["key"], int(r["n"])) for r in conn.execute(
        "SELECT rf.key, COUNT(*) AS n FROM record_fields rf "
        "JOIN records r ON r.id = rf.record_id "
        "JOIN items i ON i.id = r.item_id WHERE i.deleted_at IS NULL "
        "GROUP BY rf.key ORDER BY n DESC, rf.key LIMIT 200")]
    assert fast == joined


def test_list_keys_still_hides_deleted_items(conn, cfg, nas):
    """The fast path must engage only when it is safe: once anything is
    soft-deleted, its fields have to disappear from the vocabulary."""
    from tracepaper.query.fields import FieldQuery

    conn.execute("INSERT INTO items (kind, uri, extraction_status) "
                 "VALUES ('document', '/gone', 'complete')")
    item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO records (item_id, version, record_type, source, "
                 "created_at) VALUES (?, 1, 'payslip', 'pattern', '2026-01-01')",
                 (item_id,))
    record_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO record_fields (record_id, key, value_text) "
                 "VALUES (?, 'only_on_deleted', 'x')", (record_id,))
    conn.execute("UPDATE items SET deleted_at = '2026-01-02' WHERE id = ?",
                 (item_id,))
    conn.commit()

    keys = dict(FieldQuery(conn).list_keys())
    assert "only_on_deleted" not in keys
