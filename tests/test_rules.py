"""Folder rules and query feedback: the human layer over ranking."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracepaper import rules
from tracepaper.index.indexer import Indexer
from tracepaper.query.search import SearchEngine
from tracepaper.scan.scanner import Scanner


@pytest.fixture
def corpus(conn, cfg, nas):
    (nas / "Projects").mkdir()
    (nas / "Personal").mkdir()
    (nas / "Projects" / "readme.txt").write_text(
        "The printer project. " + "Printer notes here. " * 40)
    (nas / "Personal" / "printer_warranty.txt").write_text(
        "Printer warranty. " + "Warranty details. " * 40)
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    return conn


def _titles(conn, query="printer", **kw):
    return [h.title for h in SearchEngine(conn).search(
        query, semantic=False, **kw).hits]


def test_normalizing_a_query_ignores_word_order_and_filler():
    assert rules.normalize_query("the 3D Printer") == \
           rules.normalize_query("printer 3d") == "3d printer"


def test_a_folder_marked_as_code_leaves_normal_results(corpus, nas):
    assert "readme.txt" in _titles(corpus)

    rules.add_rule(corpus, str(nas / "Projects"), "code")

    assert "readme.txt" not in _titles(corpus), \
        "a folder you marked as code must not appear in normal search"
    assert "printer_warranty.txt" in _titles(corpus), "the rest is untouched"


def test_a_folder_marked_as_code_is_still_reachable_in_code_mode(corpus, nas):
    rules.add_rule(corpus, str(nas / "Projects"), "code")
    assert "readme.txt" in _titles(corpus, code="only")


def test_a_hidden_folder_is_gone_from_every_mode(corpus, nas):
    rules.add_rule(corpus, str(nas / "Projects"), "hide")

    for code in ("exclude", "include", "only"):
        assert "readme.txt" not in _titles(corpus, code=code), \
            f"hidden must mean hidden, including code={code}"


def test_a_boost_reorders_but_does_not_invent_results(corpus, nas):
    before = _titles(corpus)
    rules.add_rule(corpus, str(nas / "Personal"), "boost", weight=1.0)
    after = _titles(corpus)

    assert set(before) == set(after), "a boost must not add or remove results"
    assert after[0] == "printer_warranty.txt"


def test_feedback_only_applies_to_the_query_it_was_given_for(corpus):
    """The point of keying on the query.

    "Always rank this document first" is what a naive implementation learns,
    and it poisons unrelated searches. Feedback must be inert elsewhere.
    """
    item_id = corpus.execute(
        "SELECT id FROM items WHERE title = 'printer_warranty.txt'"
    ).fetchone()["id"]

    baseline = _titles(corpus, "printer project")
    rules.record_feedback(corpus, "printer warranty", item_id, "up")

    assert _titles(corpus, "printer warranty")[0] == "printer_warranty.txt"
    assert _titles(corpus, "printer project") == baseline, \
        "feedback for one query must not move another"


def test_a_thumbs_up_replaces_an_earlier_thumbs_down(corpus):
    item_id = corpus.execute(
        "SELECT id FROM items WHERE title = 'readme.txt'").fetchone()["id"]

    rules.record_feedback(corpus, "printer", item_id, "down")
    rules.record_feedback(corpus, "printer", item_id, "up")

    scores = rules.feedback_for(corpus, "printer")
    assert scores[item_id] > 0, "changing your mind must not leave both signals"


def test_feedback_saturates(corpus):
    """One habit must not freeze a query's ranking for good."""
    item_id = corpus.execute(
        "SELECT id FROM items WHERE title = 'readme.txt'").fetchone()["id"]

    for _ in range(50):
        rules.record_feedback(corpus, "printer", item_id, "up")

    assert rules.feedback_for(corpus, "printer")[item_id] <= 1.0


def test_feedback_cannot_pull_in_a_document_that_does_not_match(corpus):
    """It reorders what the query found. It is not a second retrieval path."""
    item_id = corpus.execute(
        "SELECT id FROM items WHERE title = 'readme.txt'").fetchone()["id"]
    rules.record_feedback(corpus, "kangaroo", item_id, "up")

    assert _titles(corpus, "kangaroo") == []


def test_a_moved_result_says_why(corpus, nas):
    """A ranking you cannot explain is one you cannot debug."""
    rules.add_rule(corpus, str(nas / "Personal"), "boost")
    hits = SearchEngine(corpus).search("printer", semantic=False).hits

    best = next(h for h in hits if h.title == "printer_warranty.txt")
    assert "your_boost" in best.signals


def test_rules_survive_a_rescan(corpus, cfg, nas):
    """Rules are keyed on the path, so re-indexing must not clear them."""
    rules.add_rule(corpus, str(nas / "Projects"), "code")
    Scanner(corpus, cfg).scan(nas)
    Indexer(corpus, cfg).run_pending()

    assert "readme.txt" not in _titles(corpus)


def test_removing_a_rule_restores_the_results(corpus, nas):
    prefix = str(nas / "Projects")
    rules.add_rule(corpus, prefix, "code")
    assert rules.remove_rule(corpus, prefix) is True
    assert "readme.txt" in _titles(corpus)


def test_a_rule_matches_a_path_reached_through_a_symlink(corpus, nas, tmp_path):
    """Found while checking the UI: every rule silently did nothing.

    The scanner stores resolved paths. On macOS `/var` is a symlink to
    `/private/var`, so a rule typed as `/var/...` -- which is what the shell
    and the file manager both show you -- matched no indexed item at all. The
    rule saved fine and had no effect, which is the worst way to fail.

    Reproduced here with an explicit symlink, so it holds on Linux too.
    """
    from tracepaper import rules as rules_module

    link = tmp_path / "shortcut"
    link.symlink_to(nas, target_is_directory=True)
    typed = str(link / "Projects")            # a real path, via the symlink
    assert Path(typed).resolve() != Path(typed), "the test needs a symlink"

    rules_module.add_rule(corpus, typed, "code")

    assert "readme.txt" not in _titles(corpus), \
        "a rule typed through a symlink must still match"


def test_a_trailing_slash_does_not_break_a_rule(corpus, nas):
    from tracepaper import rules as rules_module

    rules_module.add_rule(corpus, str(nas / "Projects") + "/", "code")
    assert "readme.txt" not in _titles(corpus)


def test_the_stored_prefix_is_resolved(tmp_path):
    from tracepaper import rules as rules_module

    target = tmp_path / "docs"
    target.mkdir()
    assert rules_module.canonical_prefix(str(target) + "/") == str(target.resolve())
    # A path that does not exist is still a usable rule -- an unmounted share
    # should not stop you writing one.
    missing = str(tmp_path / "not_mounted" / "docs")
    assert rules_module.canonical_prefix(missing).endswith("not_mounted/docs")


def test_marking_a_folder_as_code_clears_what_it_already_indexed(corpus, nas, cfg):
    """A rule governed only future scans, so 7,212 rows sat in the cleanup
    panel indefinitely. The scanner cannot clear them: it excludes ruled paths
    from the missing set, which is what keeps the vanish guard from aborting."""
    from tracepaper import prune

    before = corpus.execute(
        "SELECT COUNT(*) AS n FROM items WHERE uri LIKE ?",
        (str(nas / "Projects") + "/%",)).fetchone()["n"]
    assert before, "the fixture must have indexed something to clear"

    removed = prune.prune_prefix(corpus, str(nas / "Projects"))
    assert removed == before

    after = corpus.execute(
        "SELECT COUNT(*) AS n FROM items WHERE uri LIKE ?",
        (str(nas / "Projects") + "/%",)).fetchone()["n"]
    assert after == 0, "the rows the rule orphaned must be gone"

    # The rest of the index is untouched.
    assert corpus.execute(
        "SELECT COUNT(*) AS n FROM items WHERE uri LIKE ?",
        (str(nas / "Personal") + "/%",)).fetchone()["n"], "unrelated rows must stay"

    # Nothing is left claiming these paths were seen, or removing the rule
    # later would leave the files permanently un-reindexable.
    assert corpus.execute(
        "SELECT COUNT(*) AS n FROM file_state WHERE uri LIKE ?",
        (str(nas / "Projects") + "/%",)).fetchone()["n"] == 0

    # And it no longer shows up as pending cleanup, which is the whole point.
    assert prune.preview(corpus, cfg)["items"] == 0


def test_clearing_a_prefix_does_not_touch_a_sibling_with_the_same_start(corpus, nas):
    """A plain LIKE on the prefix would match /Projects2 as well."""
    from tracepaper import prune

    prune.prune_prefix(corpus, str(nas / "Project"))
    assert corpus.execute(
        "SELECT COUNT(*) AS n FROM items WHERE uri LIKE ?",
        (str(nas / "Projects") + "/%",)).fetchone()["n"], (
            "/Project must not match /Projects")


def test_only_rules_the_scanner_skips_delete_anything():
    """`boost` and friends change ranking, not membership. Deleting for those
    would remove files the next scan re-adds, forever."""
    from tracepaper.api import _SKIPPED_BY_SCANNER
    from tracepaper import rules as rules_module

    assert set(_SKIPPED_BY_SCANNER) <= set(rules_module.RULES)
    for rule in rules_module.RULES:
        if rule not in ("code", "hide"):
            assert rule not in _SKIPPED_BY_SCANNER, (
                f"{rule} does not stop the scanner, so it must not delete rows")


def test_the_skip_list_matches_what_the_scanner_actually_queries():
    """These are two lists in two files. When they disagree, a prune and a
    scan fight over the same rows."""
    import re
    from pathlib import Path
    from tracepaper.api import _SKIPPED_BY_SCANNER

    source = (Path(__file__).resolve().parents[1] / "src" / "tracepaper"
              / "scan" / "scanner.py").read_text()
    match = re.search(r"WHERE rule IN \(([^)]*)\)", source)
    assert match, "could not find the scanner's rule query"
    in_scanner = set(re.findall(r"'([a-z]+)'", match.group(1)))
    assert in_scanner == set(_SKIPPED_BY_SCANNER), (
        f"scanner skips {in_scanner}, api deletes for {set(_SKIPPED_BY_SCANNER)}")
