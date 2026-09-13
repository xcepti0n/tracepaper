"""Which files count as code, and which are documents (search modes)."""

from __future__ import annotations

import pytest

from tracepaper.db import connect
from tracepaper.query import modes

# The path from the bug report, plus its neighbours. Build output carries no
# extension at all, so only the directory says what it is.
BUILD_OUTPUT = [
    "/mnt/nas/documents/Laptop/D/Projects/AI/DexterAI/dist/DexterAI/_internal/"
    "typing_extensions-4.14.0.dist-info/licenses/LICENSE",
    "/mnt/nas/documents/Laptop/D/Projects/AI/DexterAI/dist/DexterAI/_internal/"
    "torch-2.7.1.dist-info/METADATA",
    "/mnt/nas/documents/Laptop/D/Projects/AI/DexterAI/build/DexterAI/PYZ-00.toc",
    "/mnt/nas/documents/Laptop/D/Projects/AI/DexterAI/build/DexterAI/"
    "warn-DexterAI.txt",
    "/mnt/nas/docs/proj/node_modules/left-pad/readme.md",
    "/mnt/nas/docs/proj/.venv/lib/site-packages/pip/__init__.py",
]

SOURCE = [
    "/mnt/nas/docs/proj/main.py",
    "/mnt/nas/docs/proj/app/settings.json",
    "/mnt/nas/docs/proj/styles.css",
    "/mnt/nas/docs/proj/Makefile",
]

# Documents that must survive: each one is deliberately close to a rule.
DOCUMENTS = [
    "/mnt/nas/documents/Personal/taxes/return_2023.pdf",
    "/mnt/nas/docs/ELEGOO User Guide.pdf",
    "/mnt/nas/documents/Distribution list.docx",      # starts with "dist"
    "/mnt/nas/documents/Notes/build a shed.md",       # contains "build"
    "/mnt/nas/documents/Notes/notes-about-makefile.txt",
    "/mnt/nas/documents/Recipes/outback steakhouse.md",   # contains "out"
    "/mnt/nas/photos/IMG_4821.jpg",
]


@pytest.mark.parametrize("uri", BUILD_OUTPUT + SOURCE)
def test_code_is_recognised(uri):
    assert modes.is_code(uri) is True, uri


@pytest.mark.parametrize("uri", DOCUMENTS)
def test_documents_are_not_mistaken_for_code(uri):
    assert modes.is_code(uri) is False, uri


def test_the_sql_filter_agrees_with_the_python_one():
    """Two implementations of one rule, so pin them to each other.

    `is_code` decides at classification time and `sql_filter` decides inside
    the query. If they drift, the Code mode and the default view disagree
    about the same file and neither is obviously wrong.
    """
    conn = connect(":memory:")
    everything = BUILD_OUTPUT + SOURCE + DOCUMENTS
    for item_id, uri in enumerate(everything, start=1):
        conn.execute(
            "INSERT INTO items (id, kind, uri, title, extraction_status) "
            "VALUES (?, 'document', ?, ?, 'complete')",
            (item_id, uri, uri.rsplit("/", 1)[-1]))

    kept = {r["uri"] for r in conn.execute(
        f"SELECT uri FROM items i WHERE {modes.sql_filter('i')}")}
    only_code = {r["uri"] for r in conn.execute(
        f"SELECT uri FROM items i WHERE {modes.sql_only_code('i')}")}

    for uri in everything:
        in_sql = uri not in kept
        assert in_sql == modes.is_code(uri), (
            f"SQL says code={in_sql}, Python says code={modes.is_code(uri)}: "
            f"{uri}")

    assert kept == set(DOCUMENTS)
    assert only_code == set(BUILD_OUTPUT + SOURCE)
    assert not kept & only_code, "the two modes must not overlap"
    conn.close()


# --- Pruning what today's rules would not index --------------------------

def test_prune_preview_groups_by_reason_and_deletes_nothing(conn, cfg, nas):
    """Preview must be a read. It is the screen you decide from."""
    from tracepaper import prune
    from tracepaper.index.indexer import Indexer
    from tracepaper.scan.scanner import Scanner

    junk = nas / "Projects" / "app" / "node_modules" / "left-pad"
    junk.mkdir(parents=True)
    (junk / "index.txt").write_text("module.exports = leftPad;")
    keep = nas / "Personal"
    keep.mkdir(parents=True)
    (keep / "return.txt").write_text("Tax return 2023.")

    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    # Index it first, THEN exclude it -- the case prune exists for.
    conn.execute("INSERT INTO items (kind, uri, title, extraction_status) "
                 "VALUES ('document', ?, 'index.txt', 'complete')",
                 (str(junk / "index.txt"),))
    conn.commit()

    before = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    report = prune.preview(conn, cfg)
    after = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]

    assert after == before, "preview must not delete anything"
    assert report["items"] >= 1
    assert any(r["reason"] == "node_modules" for r in report["reasons"])


def test_prune_removes_the_item_and_everything_hanging_off_it(conn, cfg):
    """Passages and embeddings must go too, or the index keeps answering."""
    from tracepaper import prune

    conn.execute("INSERT INTO items (id, kind, uri, title, extraction_status) "
                 "VALUES (1, 'document', '/nas/p/node_modules/x/a.txt', "
                 "'a.txt', 'complete')")
    conn.execute("INSERT INTO passages (id, item_id, version, ordinal, text) "
                 "VALUES (1, 1, 1, 0, 'some text')")
    conn.execute("INSERT INTO file_state (uri, size_bytes, mtime, "
                 "last_seen_scan) VALUES ('/nas/p/node_modules/x/a.txt', 9, 1.0, 1)")
    conn.commit()

    assert prune.apply(conn, cfg) == 1

    assert conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM passages").fetchone()["n"] == 0, \
        "passages must cascade, or search still returns the pruned file"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM file_state").fetchone()["n"] == 0, \
        "file_state must go, or the path is remembered as seen and never re-indexed"


def test_a_folder_you_marked_as_code_becomes_prunable(conn, cfg):
    """Your own rule is a prune reason, like any built-in exclusion."""
    from tracepaper import prune, rules

    conn.execute("INSERT INTO items (id, kind, uri, title, extraction_status) "
                 "VALUES (1, 'document', '/nas/Projects/DexterAI/notes.txt', "
                 "'notes.txt', 'complete')")
    conn.commit()

    assert prune.preview(conn, cfg)["items"] == 0
    rules.add_rule(conn, "/nas/Projects/DexterAI", "code")

    report = prune.preview(conn, cfg)
    assert report["items"] == 1
    assert any("code" in r["reason"] for r in report["reasons"])
