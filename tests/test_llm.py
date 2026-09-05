"""LLM gap-filling at ingest (D-003, D-010).

The validation layer matters more than the calls: a hallucinated field enters
the index as fact and outlives the model that invented it, so anything doubtful
is discarded rather than stored.
"""

from __future__ import annotations

import pytest

from datamanager.extract import records
from datamanager.extract.llm import LlmConfig, _validate


def test_disabled_by_default():
    assert LlmConfig().enabled is False


def test_valid_payload_becomes_a_record():
    record = _validate({
        "record_type": "quote",
        "fields": {"contractor": "Cascade Roofing", "quote_amount": "14800",
                   "warranty_years": "30"},
    }, "test-model")

    assert record is not None
    assert record.source == "llm"
    assert record.model_id == "test-model"
    assert record.confidence < 1.0, "model output must rank below templates"
    assert {f.key for f in record.fields} == {"contractor", "quote_amount",
                                              "warranty_years"}


def test_amounts_and_dates_are_typed():
    record = _validate({"record_type": "quote",
                        "fields": {"total": "14800", "signed_on": "2024-03-15"}},
                       "test-model")
    by_key = {f.key: f for f in record.fields}
    assert by_key["total"].value_num == 14800.0
    assert by_key["signed_on"].value_date == "2024-03-15"


def test_nested_values_are_rejected():
    """A nested object means the model restructured rather than extracted."""
    record = _validate({"record_type": "x",
                        "fields": {"good": "value",
                                   "bad": {"nested": "object"},
                                   "worse": ["a", "list"]}}, "m")
    assert {f.key for f in record.fields} == {"good"}


def test_summary_style_keys_are_rejected():
    """Those keys invite the model to summarise instead of extract."""
    record = _validate({"record_type": "x",
                        "fields": {"summary": "This document is about roofing",
                                   "contractor": "Cascade Roofing"}}, "m")
    assert {f.key for f in record.fields} == {"contractor"}


def test_malformed_payloads_return_none():
    assert _validate({}, "m") is None
    assert _validate({"fields": {}}, "m") is None
    assert _validate({"fields": "not a dict"}, "m") is None
    assert _validate("not a dict", "m") is None


def test_overlong_values_are_dropped():
    record = _validate({"record_type": "x",
                        "fields": {"prose": "word " * 200, "ok": "short"}}, "m")
    assert {f.key for f in record.fields} == {"ok"}


def test_invalid_keys_are_dropped():
    record = _validate({"record_type": "x",
                        "fields": {"a whole sentence as a key here": "v",
                                   "": "v", "valid_key": "v"}}, "m")
    assert {f.key for f in record.fields} == {"valid_key"}


def test_llm_records_rank_below_deterministic_layers():
    """Precedence: a template value must beat a model's guess for one key."""
    template = records.Record(
        record_type="tax_form_w2", source="template", confidence=0.95,
        fields=[records.Field(key="gross_salary", value_text="91500.00")])
    guessed = records.Record(
        record_type="document", source="llm", confidence=0.5,
        fields=[records.Field(key="gross_salary", value_text="90000"),
                records.Field(key="extra_fact", value_text="kept")])

    merged = records._merge([guessed, template])

    by_key = {f.key: f for record in merged for f in record.fields}
    assert by_key["gross_salary"].value_text == "91500.00"
    # A lower layer still contributes keys the higher one did not supply.
    assert "extra_fact" in by_key


def test_extraction_skips_documents_that_are_already_rich(conn, cfg, nas):
    """The expensive layer runs only where deterministic layers came up thin."""
    calls: list[str] = []

    class Spy:
        enabled = True

    def fake_extract(text, config):
        calls.append(text[:20])
        return []

    import datamanager.extract.llm as llm_module
    original = llm_module.extract
    llm_module.extract = fake_extract
    try:
        rich = ("Passport Details\nPassport Number: X1234567\n"
                "Nationality: India\nDate of Expiry: 2029-06-11\n")
        records.extract_records(rich, "passport.txt",
                                llm_config=Spy(), min_fields=3)
        assert not calls, "a document with enough fields must skip the model"

        records.extract_records("Just some prose with no labels at all.",
                                "note.txt", llm_config=Spy(), min_fields=3)
        assert calls, "thin extraction must fall through to the model"
    finally:
        llm_module.extract = original


def test_endpoint_failure_does_not_lose_other_records(conn, cfg, nas):
    """An unreachable endpoint must never cost a document its other layers."""
    class Unreachable:
        enabled = True

    import datamanager.extract.llm as llm_module
    original = llm_module.extract

    def exploding(text, config):
        raise ConnectionError("endpoint down")

    llm_module.extract = exploding
    try:
        found = records.extract_records(
            "Passport Number: X1234567\n", "passport.txt",
            llm_config=Unreachable(), min_fields=10)
        keys = {f.key for r in found for f in r.fields}
        assert "passport_number" in keys
    finally:
        llm_module.extract = original
