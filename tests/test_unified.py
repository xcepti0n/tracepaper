"""Unified search: one query box over every layer (FR-12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracepaper.index.indexer import Indexer
from tracepaper.query.unified import UnifiedSearch
from tracepaper.scan.scanner import Scanner

W2_2023 = """Form W-2 Wage and Tax Statement
Tax Year: 2023
Employer name: ACME Corporation
1 Wages, tips, other compensation 91500.00
"""

W2_2022 = """Form W-2 Wage and Tax Statement
Tax Year: 2022
Employer name: ACME Corporation
1 Wages, tips, other compensation 79500.00
"""

PASSPORT = """Passport Details
Passport Number: X1234567
Date of Expiry: 2029-06-11
"""

FLIGHT = """From: noreply@alaskaair.com
Subject: Alaska Airlines itinerary
Confirmation code: ABC123
Flight AS 1234, SEA to PDX
Departure date: 2023-04-15
"""


@pytest.fixture
def corpus(conn, cfg, nas):
    for name, body in {"w2_2023.txt": W2_2023, "w2_2022.txt": W2_2022,
                       "passport.txt": PASSPORT, "flight.eml": FLIGHT}.items():
        (nas / name).write_text(body)
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    return conn


def test_a_field_question_returns_a_direct_answer(corpus):
    result = UnifiedSearch(corpus).query("passport expiry", semantic=False)

    assert result.answer is not None
    assert result.answer.value == "2029-06-11"
    assert result.answer_key == "expiry_date"


def test_natural_phrasing_still_finds_the_field(corpus):
    """Filler words must not stop a field lookup."""
    result = UnifiedSearch(corpus).query("what is my passport expiry",
                                         semantic=False)
    assert result.answer is not None
    assert result.answer.value == "2029-06-11"


def test_a_year_in_the_query_constrains_the_answer(corpus):
    """"salary 2023" must read the 2023 form, never the 2022 one."""
    search = UnifiedSearch(corpus)

    assert search.query("salary 2023", semantic=False).answer.value == 91500.0
    assert search.query("salary 2022", semantic=False).answer.value == 79500.0


def test_an_entity_query_returns_events(corpus):
    result = UnifiedSearch(corpus).query("alaska", semantic=False)

    assert result.entities, "the airline should be recognised"
    assert result.events, "and its flight surfaced"
    assert result.events[0]["date"] == "2023-04-15"
    assert result.events[0]["evidence"], "with the document that evidences it"


def test_plain_text_falls_through_to_documents(corpus):
    """No field, no entity -- the passage floor still answers."""
    result = UnifiedSearch(corpus).query("wage statement", semantic=False)

    assert result.answer is None
    assert result.hits


def test_one_query_can_return_several_kinds_at_once(corpus):
    result = UnifiedSearch(corpus).query("alaska flight", semantic=False)

    assert result.events
    assert result.hits


def test_empty_query_returns_nothing(corpus):
    assert UnifiedSearch(corpus).query("").is_empty


def test_nonsense_query_is_empty_not_an_error(corpus):
    result = UnifiedSearch(corpus).query("zzzz qqqq", semantic=False)
    assert result.is_empty


def test_disagreement_is_surfaced_not_resolved(corpus, cfg, nas):
    """Two documents with different values must both be shown."""
    (nas / "w2_amended.txt").write_text(W2_2023.replace("91500.00", "93000.00"))
    Scanner(corpus, cfg).scan(nas)
    Indexer(corpus, cfg).run_pending()

    result = UnifiedSearch(corpus).query("salary 2023", semantic=False)

    assert result.answer is not None
    assert result.alternatives, "a conflicting value must not be hidden"


def test_results_are_reproducible(corpus):
    search = UnifiedSearch(corpus)
    runs = [
        (r.answer.value if r.answer else None,
         [e["id"] for e in r.events],
         [h.passage_id for h in r.hits])
        for r in (search.query("alaska 2023", semantic=False) for _ in range(5))
    ]
    assert all(run == runs[0] for run in runs)


def test_key_matching_requires_every_word(corpus, cfg, nas):
    """"salary" must not match "salary_advance_repayment"."""
    (nas / "loan.txt").write_text("Salary advance repayment: 500.00\n")
    Scanner(corpus, cfg).scan(nas)
    Indexer(corpus, cfg).run_pending()

    result = UnifiedSearch(corpus).query("salary 2023", semantic=False)

    assert result.answer.value == 91500.0


# --- One document, one result -------------------------------------------

# Long enough to split into many passages (~1200 chars each), which is the
# whole point: a real manual mentions the query on page after page.
MANUAL = "\n\n".join(
    f"Section {n}. Setting up the 3d printer. "
    + "Bed levelling for the 3d printer is covered in detail here. " * 30
    for n in range(1, 16)
)
MENTION = "I bought a 3d printer in March and it arrived late.\n"


@pytest.fixture
def printer_corpus(conn, cfg, nas):
    (nas / "elegoo_printer_guide.txt").write_text(MANUAL)
    for name in ("diary.txt", "receipts.txt", "wishlist.txt"):
        (nas / name).write_text(MENTION)
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    return conn


def test_one_document_appears_once_however_many_passages_match(printer_corpus):
    """A long manual matching on every page is one result, not fifteen.

    Before grouping, the guide's passages filled every slot and the three
    other documents were pushed off the page entirely.
    """
    result = UnifiedSearch(printer_corpus).query("3d printer", semantic=False)

    item_ids = [hit.item_id for hit in result.hits]
    assert len(item_ids) == len(set(item_ids)), (
        f"the same document is listed more than once: {item_ids}")
    assert len(result.hits) == 4, "every matching document should be reachable"


def test_the_guide_still_wins_and_keeps_its_other_pages(printer_corpus):
    result = UnifiedSearch(printer_corpus).query("3d printer", semantic=False)

    best = result.hits[0]
    assert "elegoo" in (best.uri or "").lower(), "the guide should rank first"
    # Its remaining matches are folded underneath rather than discarded.
    assert best.more, "the other matching passages should still be reachable"
    assert best.passage_count > 1
    assert all(m.passage_id != best.passage_id for m in best.more)


def test_the_total_counts_documents_not_passages(printer_corpus):
    """"15 results" above a single row was the passage count leaking out."""
    result = UnifiedSearch(printer_corpus).query("3d printer", semantic=False)
    assert result.total_hits == 4


# --- The direct answer has to earn its slot ------------------------------

JSON_BLOB = """Printer: {"type": "integer"},
Serial: {"type": "string"},
"""


@pytest.fixture
def noisy_corpus(conn, cfg, nas):
    """A vendored source file whose `label: value` lines look like fields."""
    (nas / "chat_template_utils.txt").write_text(JSON_BLOB)
    (nas / "printer_notes.txt").write_text(MENTION)
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    return conn


def test_a_topic_query_gets_no_direct_answer(noisy_corpus):
    """"3d printer" is browsing, not a question about a stored value."""
    result = UnifiedSearch(noisy_corpus).query("3d printer", semantic=False)
    assert result.answer is None, (
        f"a topic query must not produce a value banner, got "
        f"{result.answer.value_text!r}")


def test_a_code_fragment_is_never_shown_as_an_answer(noisy_corpus):
    """Even when asked outright, `{"type": "integer"},` is not an answer."""
    result = UnifiedSearch(noisy_corpus).query("what is my printer",
                                               semantic=False)
    if result.answer is not None:
        assert "{" not in result.answer.value_text
