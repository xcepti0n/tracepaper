"""Entities and events (FR-5, FR-6)."""

from __future__ import annotations

from pathlib import Path

from tracepaper import entities, events, vocabulary
from tracepaper.index.indexer import Indexer
from tracepaper.scan.scanner import Scanner

FLIGHT_EMAIL = """From: noreply@alaskaair.com
Subject: Your Alaska Airlines itinerary

Confirmation code: ABC123
Flight AS 1234, SEA to PDX
Departure date: 2023-04-15
"""

BOARDING_PASS = """Alaska Airlines Boarding Pass
Confirmation: ABC123
Flight AS 1234
SEA to PDX
Departure: 2023-04-15
Seat 14C
"""

OLDER_FLIGHT = """From: noreply@alaskaair.com
Subject: Alaska Airlines itinerary

Confirmation code: XYZ789
Flight AS 990, SEA to LAX
Departure date: 2022-11-02
"""

RECEIPT = """COSTCO WHOLESALE #1234
Date: 2024-03-15
Order number: ORD-8891
Subtotal: 213.97
Total: 235.80
"""


def build(conn, cfg, nas: Path, files: dict[str, str]):
    for name, body in files.items():
        path = nas / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    Scanner(conn, cfg).scan(nas)
    return Indexer(conn, cfg).run_pending()


# ---------------------------------------------------------------- normalising

def test_statement_noise_folds_to_one_merchant():
    """A card statement spells a merchant differently every time."""
    assert entities.normalize("COSTCO WHOLESALE #1234") == entities.normalize("Costco")
    assert entities.normalize("SQ *BLUE BOTTLE") == entities.normalize("Blue Bottle")


def test_legal_suffixes_are_ignored():
    assert entities.normalize("ACME Corporation") == entities.normalize("ACME Corp")
    assert entities.normalize("Foo Ltd.") == entities.normalize("Foo Limited")


def test_distinct_merchants_stay_distinct():
    """Normalising must not collapse genuinely different businesses."""
    assert entities.normalize("Home Depot") != entities.normalize("Office Depot")


# ------------------------------------------------------------------- entities

def test_aliases_resolve_to_one_entity(conn):
    first = entities.get_or_create(conn, "Costco")
    second = entities.get_or_create(conn, "COSTCO WHOLESALE #1234")

    assert first == second
    assert len(entities.aliases(conn, first)) == 2


def test_entities_can_be_merged_by_hand(conn):
    a = entities.get_or_create(conn, "Alaska Airlines")
    b = entities.get_or_create(conn, "Alaska Air Group")
    assert a != b

    entities.merge(conn, b, a)

    assert entities.resolve(conn, "Alaska Air Group") == a
    assert conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"] == 1


def test_entities_are_linked_from_records(conn, cfg, nas):
    build(conn, cfg, nas, {"flight.eml": FLIGHT_EMAIL})

    found = entities.find(conn, "Alaska Airlines")

    assert found, "the airline should have been linked from the booking"


# --------------------------------------------------------------------- events

def test_flight_event_answers_when_did_i_last_fly(conn, cfg, nas):
    """FR-5: the question is about a flight, not about a document."""
    build(conn, cfg, nas, {"new.eml": FLIGHT_EMAIL, "old.eml": OLDER_FLIGHT})

    rows = events.query(conn, event_type="flight", entity="Alaska Airlines", limit=1)

    assert len(rows) == 1
    assert rows[0]["occurred_on"] == "2023-04-15", "most recent flight expected"


def test_one_flight_from_two_documents_is_one_event(conn, cfg, nas):
    """A confirmation email and a boarding pass describe the same flight."""
    build(conn, cfg, nas, {"conf.eml": FLIGHT_EMAIL, "pass.txt": BOARDING_PASS})

    rows = events.query(conn, event_type="flight")

    assert len(rows) == 1, "the same flight must not become two events"
    evidence = events.evidence(conn, int(rows[0]["id"]))
    assert len(evidence) == 2, "both documents must be cited"


def test_events_carry_evidence(conn, cfg, nas):
    build(conn, cfg, nas, {"flight.eml": FLIGHT_EMAIL})
    row = events.query(conn, event_type="flight")[0]

    evidence = events.evidence(conn, int(row["id"]))

    assert evidence
    assert evidence[0]["uri"].endswith("flight.eml")


def test_event_ordering_is_deterministic(conn, cfg, nas):
    build(conn, cfg, nas, {"a.eml": FLIGHT_EMAIL, "b.eml": OLDER_FLIGHT,
                           "receipt.txt": RECEIPT})

    runs = [[(r["id"], r["occurred_on"]) for r in events.query(conn, limit=50)]
            for _ in range(5)]

    assert all(run == runs[0] for run in runs)


def test_purchase_event_from_receipt(conn, cfg, nas):
    build(conn, cfg, nas, {"costco.txt": RECEIPT})

    rows = events.query(conn)

    assert rows, "a receipt should yield an event"
    assert any(r["occurred_on"] == "2024-03-15" for r in rows)


def test_year_precision_for_tax_forms(conn, cfg, nas):
    """A W-2 dates to a year, not a day. Precision is recorded, not invented."""
    build(conn, cfg, nas, {"w2.txt":
                           "Form W-2 Wage and Tax Statement\nTax Year: 2023\n"
                           "Employer name: ACME Corporation\n"
                           "1 Wages, tips, other compensation 91500.00\n"})

    rows = events.query(conn, event_type="income")

    assert rows
    assert rows[0]["occurred_precision"] == "year"


def test_deleting_evidence_removes_the_event(conn, cfg, nas):
    """An event with no surviving document is a stale row, not a memory."""
    path = nas / "flight.eml"
    build(conn, cfg, nas, {"flight.eml": FLIGHT_EMAIL})
    assert events.query(conn, event_type="flight")

    path.write_text("unrelated content with no booking in it at all")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    assert not events.query(conn, event_type="flight")


def test_reingest_does_not_duplicate_evidence(conn, cfg, nas):
    build(conn, cfg, nas, {"flight.eml": FLIGHT_EMAIL})
    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])

    Indexer(conn, cfg).reindex_item(item_id)
    Indexer(conn, cfg).reindex_item(item_id)

    row = events.query(conn, event_type="flight")[0]
    assert len(events.evidence(conn, int(row["id"]))) == 1


# ----------------------------------------------------------------- vocabulary

def test_box_numbers_are_stripped():
    assert vocabulary.canonical_form("box_2_federal_income_tax") == \
        "federal_income_tax"


def test_seed_aliases_converge(conn):
    assert vocabulary.resolve(conn, "date_of_expiry") == "expiry_date"
    assert vocabulary.resolve(conn, "wages_tips_other_compensation") == "gross_salary"


def test_pinned_mapping_is_never_overridden(conn):
    vocabulary.pin(conn, "custom_key", "my_canonical")

    vocabulary.register(conn, "custom_key")

    assert vocabulary.resolve(conn, "custom_key") == "my_canonical"


def test_merging_a_key_rewrites_stored_rows(conn, cfg, nas):
    """Old data must stay reachable under the new name."""
    build(conn, cfg, nas, {"a.txt": "Widget Count: 12\n"})
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM record_fields WHERE key='widget_count'"
    ).fetchone()["n"] == 1

    vocabulary.merge(conn, "widget_count", "widgets")

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM record_fields WHERE key='widgets'"
    ).fetchone()["n"] == 1


def test_synonyms_are_stored_under_one_key(conn, cfg, nas):
    """The drift that made a query for one name miss the other."""
    build(conn, cfg, nas, {
        "a.txt": "Date of Expiry: 2029-06-11\n",
        "b.txt": "Expiration Date: 2030-01-15\n",
    })

    keys = {r["key"] for r in conn.execute("SELECT DISTINCT key FROM record_fields")}

    assert "expiry_date" in keys
    assert "date_of_expiry" not in keys
    assert "expiration_date" not in keys
