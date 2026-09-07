"""Record extraction and Tier 1 answers (FR-3, FR-4, FR-8, FR-10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracepaper import corrections
from tracepaper.extract import records
from tracepaper.index.indexer import Indexer
from tracepaper.query.fields import FieldQuery
from tracepaper.scan.scanner import Scanner

W2_2023 = """Form W-2  Wage and Tax Statement
Tax Year: 2023

Employer name: ACME Corporation
Employer EIN: 12-3456789

1  Wages, tips, other compensation     91500.00
2  Federal income tax withheld         13725.00
"""

W2_2022 = """Form W-2  Wage and Tax Statement
Tax Year: 2022

Employer name: ACME Corporation

1  Wages, tips, other compensation     79500.00
2  Federal income tax withheld         11925.00
"""

PASSPORT = """Passport Details
Passport Number: X1234567
Nationality: India
Date of Issue: 2019-06-12
Date of Expiry: 2029-06-11
"""


def build(conn, cfg, nas: Path, files: dict[str, str]):
    for name, body in files.items():
        path = nas / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    Scanner(conn, cfg).scan(nas)
    return Indexer(conn, cfg).run_pending()


# ------------------------------------------------------- whole-document binding

def test_w2_binds_year_to_salary(conn, cfg, nas):
    """The case the whole design exists for (FR-3).

    Tax year and gross salary sit far apart on the form. Bound at ingest, a
    query for one year can never return another year's number.
    """
    build(conn, cfg, nas, {"w2_2023.txt": W2_2023, "w2_2022.txt": W2_2022})
    fq = FieldQuery(conn)

    assert fq.get("gross_salary", where={"tax_year": 2023}).best.value == 91500.0
    assert fq.get("gross_salary", where={"tax_year": 2022}).best.value == 79500.0


def test_constraint_stays_inside_one_record(conn, cfg, nas):
    """A constraint must not match across documents.

    Both forms share an employer, so a naive implementation joining on
    employer alone would happily return the wrong year's salary.
    """
    build(conn, cfg, nas, {"w2_2023.txt": W2_2023, "w2_2022.txt": W2_2022})

    answer = FieldQuery(conn).get("gross_salary",
                                  where={"tax_year": 2023, "employer": "ACME Corporation"})

    assert len(answer.values) == 1
    assert answer.best.value == 91500.0


def test_answer_carries_a_citation(conn, cfg, nas):
    """Tier 1 returns a value *plus* where it came from."""
    build(conn, cfg, nas, {"tax/w2_2023.txt": W2_2023})

    best = FieldQuery(conn).get("gross_salary", where={"tax_year": 2023}).best

    assert best.item_title == "w2_2023.txt"
    assert best.uri and best.uri.endswith("w2_2023.txt")
    assert best.source == "template"
    assert "w2_2023.txt" in best.citation()


def test_passport_expiry_needs_no_model(conn, cfg, nas):
    build(conn, cfg, nas, {"passport.txt": PASSPORT})

    answer = FieldQuery(conn).get("expiry_date")

    assert answer.best.value == "2029-06-11"
    assert answer.best.value_date == "2029-06-11"


def test_unknown_document_type_still_yields_fields(conn, cfg, nas):
    """Open vocabulary (FR-4): no code exists for this document type."""
    build(conn, cfg, nas, {"vet.txt":
                           "Veterinary Invoice\n"
                           "Clinic: Bayside Animal Hospital\n"
                           "Pet name: Mochi\n"
                           "Microchip ID: 985141002\n"
                           "Total due: 248.50\n"})

    fq = FieldQuery(conn)
    assert fq.get("pet_name").best.value_text == "Mochi"
    assert fq.get("microchip_id").best.value_text == "985141002"
    assert fq.get("total_due").best.value == 248.50


def test_keys_are_discovered_not_declared(conn, cfg, nas):
    build(conn, cfg, nas, {"w2.txt": W2_2023, "passport.txt": PASSPORT})

    keys = dict(FieldQuery(conn).list_keys())

    assert "gross_salary" in keys
    assert "passport_number" in keys


def test_list_values_answers_which_years_exist(conn, cfg, nas):
    build(conn, cfg, nas, {"a.txt": W2_2023, "b.txt": W2_2022})

    values = dict(FieldQuery(conn).list_values("tax_year"))

    assert set(values) == {"2023", "2022"}


# ------------------------------------------------------------------ aggregation

def test_aggregate_sums_and_lists_contributors(conn, cfg, nas):
    """FR-8: the number must be auditable back to its documents."""
    build(conn, cfg, nas, {"a.txt": W2_2023, "b.txt": W2_2022})

    total, contributing = FieldQuery(conn).aggregate("gross_salary", "sum")

    assert total == pytest.approx(171000.0)
    assert len(contributing) == 2
    assert all(v.uri for v in contributing), "every contributor must be citable"


def test_aggregate_respects_constraints(conn, cfg, nas):
    build(conn, cfg, nas, {"a.txt": W2_2023, "b.txt": W2_2022})

    total, contributing = FieldQuery(conn).aggregate(
        "gross_salary", "sum", where={"tax_year": 2023})

    assert total == pytest.approx(91500.0)
    assert len(contributing) == 1


def test_aggregate_counts_distinct_documents(conn, cfg, nas):
    build(conn, cfg, nas, {"a.txt": W2_2023, "b.txt": W2_2022})

    count, _ = FieldQuery(conn).aggregate("gross_salary", "count")

    assert count == 2


# ------------------------------------------------------------------ corrections

def test_correction_outranks_extraction(conn, cfg, nas):
    build(conn, cfg, nas, {"w2.txt": W2_2023})
    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])

    corrections.correct_field(conn, item_id, "gross_salary", "92000.00")

    best = FieldQuery(conn).get("gross_salary").best
    assert best.value == 92000.0
    assert best.source == "human"


def test_correction_survives_full_reindex(conn, cfg, nas):
    """Success criterion 7 -- the property that makes corrections worth making."""
    build(conn, cfg, nas, {"w2.txt": W2_2023})
    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])
    corrections.correct_field(conn, item_id, "gross_salary", "92000.00")

    Indexer(conn, cfg).reindex_item(item_id)

    best = FieldQuery(conn).get("gross_salary").best
    assert best.value == 92000.0, "a hand-corrected value must survive reindex"
    assert best.source == "human"


def test_correction_survives_document_change(conn, cfg, nas):
    """Even when the file itself is edited and re-extracted."""
    path = nas / "w2.txt"
    build(conn, cfg, nas, {"w2.txt": W2_2023})
    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])
    corrections.correct_field(conn, item_id, "gross_salary", "92000.00")

    path.write_text(W2_2023.replace("91500.00", "93000.00"))
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    assert FieldQuery(conn).get("gross_salary").best.value == 92000.0


def test_removing_a_correction_restores_extraction(conn, cfg, nas):
    build(conn, cfg, nas, {"w2.txt": W2_2023})
    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])
    corrections.correct_field(conn, item_id, "gross_salary", "92000.00")

    assert corrections.remove_correction(conn, item_id, "gross_salary")
    Indexer(conn, cfg).reindex_item(item_id)

    best = FieldQuery(conn).get("gross_salary").best
    assert best.value == 91500.0
    assert best.source == "template"


def test_corrections_are_listable_for_backup(conn, cfg, nas):
    """NFR-6: this layer is the only irreplaceable state."""
    build(conn, cfg, nas, {"w2.txt": W2_2023})
    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])
    corrections.correct_field(conn, item_id, "gross_salary", "92000.00")

    rows = corrections.list_corrections(conn)

    assert len(rows) == 1
    assert rows[0]["key"] == "gross_salary"


# ------------------------------------------------------------- value parsing

@pytest.mark.parametrize("raw,expected", [
    ("2023-06-11", "2023-06-11"),
    ("11 June 2019", "2019-06-11"),
    ("June 11, 2019", "2019-06-11"),
    ("25/12/2023", "2023-12-25"),      # day > 12, unambiguous
    ("12/25/2023", "2023-12-25"),      # month position unambiguous
])
def test_date_parsing(raw, expected):
    assert records.parse_date(raw) == expected


def test_ambiguous_date_is_refused():
    """A wrong date silently poisons a Tier 1 answer; refuse rather than guess."""
    assert records.parse_date("05/06/2023") is None


def test_impossible_date_is_refused():
    assert records.parse_date("2023-02-30") is None


@pytest.mark.parametrize("raw,value,unit", [
    ("91500.00", 91500.0, None),
    ("$1,234.56", 1234.56, "USD"),
    ("€2.500,00", 2.5, "EUR"),          # European format: first parse wins
    ("1,234", 1234.0, None),
    ("142.50 USD", 142.5, "USD"),
])
def test_amount_parsing(raw, value, unit):
    parsed, parsed_unit = records.parse_amount(raw)
    assert parsed == pytest.approx(value)
    assert parsed_unit == unit


def test_address_is_not_parsed_as_an_amount(conn, cfg, nas):
    """"Seattle, WA 98101" contains digits but is not a number."""
    build(conn, cfg, nas, {"a.txt": "Address: 500 Industrial Way, Seattle, WA 98101\n"})

    best = FieldQuery(conn).get("address").best

    assert best.value_num is None
    assert "Seattle" in best.value_text


def test_normalize_key():
    assert records.normalize_key("Date of Expiry") == "date_of_expiry"
    assert records.normalize_key("Employer's Name") == "employer_s_name"
    assert records.normalize_key("  Total Due  ") == "total_due"


# -------------------------------------------------------------- source ranking

def test_template_outranks_generic_pattern(conn, cfg, nas):
    """A W-2 template match must beat the open-vocabulary fallback."""
    build(conn, cfg, nas, {"w2.txt": W2_2023})

    best = FieldQuery(conn).get("tax_year").best

    assert best.source == "template"


def test_extractor_failure_does_not_lose_other_records(conn, cfg, nas):
    """One broken extractor must not cost a document its other fields."""
    class Exploding(records.Extractor):
        name = "exploding"
        source = "pattern"

        def matches(self, text, title):
            return True

        def extract(self, text, title):
            raise RuntimeError("boom")

    original = list(records.EXTRACTORS)
    records.EXTRACTORS.insert(0, Exploding())
    try:
        build(conn, cfg, nas, {"passport.txt": PASSPORT})
        assert FieldQuery(conn).get("passport_number").best.value_text == "X1234567"
    finally:
        records.EXTRACTORS[:] = original


# ------------------------------------------------------- aggregate deduplication

def test_same_form_in_two_formats_is_not_double_counted(conn, cfg, nas):
    """A tax form kept as both a scan and an export is ordinary on a NAS.

    Counting its salary twice would silently inflate every total.
    """
    build(conn, cfg, nas, {"w2_2023.txt": W2_2023,
                           "scans/w2_2023_scan.txt": W2_2023,
                           "w2_2022.txt": W2_2022})

    total, contributing = FieldQuery(conn).aggregate("gross_salary", "sum")

    assert total == pytest.approx(171000.0), "duplicate form counted twice"
    assert len(contributing) == 2


def test_distinct_documents_sharing_an_amount_are_both_counted(conn, cfg, nas):
    """The other direction: dedup must not swallow genuinely separate facts.

    Two rent payments of the same amount in different months are two payments.
    """
    build(conn, cfg, nas, {
        "rent_january.txt": "Rent Receipt\nMonth: January 2024\nAmount paid: 2450.00\n",
        "rent_february.txt": "Rent Receipt\nMonth: February 2024\nAmount paid: 2450.00\n",
    })

    total, contributing = FieldQuery(conn).aggregate("amount_paid", "sum")

    assert len(contributing) == 2, "distinct payments must not be merged"
    assert total == pytest.approx(4900.0)


def test_records_disagreeing_on_a_shared_field_are_not_merged(conn, cfg, nas):
    """Two W-2s for one year that disagree on withholding are not one form.

    Merging them would hide a discrepancy the user probably wants to see.
    """
    amended = W2_2023.replace("13725.00", "14100.00")
    build(conn, cfg, nas, {"w2.txt": W2_2023, "w2_amended.txt": amended})

    _, contributing = FieldQuery(conn).aggregate("gross_salary", "sum")

    assert len(contributing) == 2, "a disagreement must not be silently collapsed"


def test_partial_extraction_still_merges_duplicates(conn, cfg, nas):
    """One copy yielding fewer fields is still the same form.

    A scan that misses a box must not be counted as a second W-2.
    """
    partial = "\n".join(line for line in W2_2023.splitlines()
                        if "Federal income tax" not in line)
    build(conn, cfg, nas, {"w2.txt": W2_2023, "scans/w2_scan.txt": partial})

    total, contributing = FieldQuery(conn).aggregate("gross_salary", "sum")

    assert len(contributing) == 1
    assert total == pytest.approx(91500.0)
