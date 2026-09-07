"""Text extraction and passage splitting (FR-2, FR-7)."""

from __future__ import annotations

from pathlib import Path

from tracepaper.extract import passages, text


def test_plaintext(tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("hello world")
    result = text.extract(path)
    assert result.text == "hello world"
    assert result.status == "complete"


def test_markdown_is_text(tmp_path: Path):
    path = tmp_path / "a.md"
    path.write_text("# Heading\n\nBody text.")
    result = text.extract(path)
    assert "Heading" in result.text
    assert result.status == "complete"


def test_csv_repeats_header_on_each_row(tmp_path: Path):
    """A row divorced from its header is unsearchable once split into passages."""
    path = tmp_path / "spend.csv"
    path.write_text("date,merchant,amount\n2023-04-01,Costco,142.50\n")
    result = text.extract(path)
    assert "merchant: Costco" in result.text
    assert "amount: 142.50" in result.text


def test_tsv_delimiter(tmp_path: Path):
    path = tmp_path / "a.tsv"
    path.write_text("name\tvalue\nfoo\tbar\n")
    result = text.extract(path)
    assert "name: foo" in result.text


def test_eml_includes_headers_and_body(tmp_path: Path):
    path = tmp_path / "flight.eml"
    path.write_text(
        "From: noreply@alaskaair.com\n"
        "To: me@example.com\n"
        "Subject: Your itinerary\n"
        "Date: Mon, 3 Apr 2023 10:00:00 -0700\n"
        "\n"
        "Confirmation ABC123, Seattle to Portland.\n"
    )
    result = text.extract(path)
    assert "alaskaair.com" in result.text
    assert "Your itinerary" in result.text
    assert "ABC123" in result.text


def test_unknown_binary_is_partial_not_failed(tmp_path: Path):
    """FR-2: never reject a file."""
    path = tmp_path / "a.bin"
    path.write_bytes(b"\x00\x01\x02\x03binary")
    result = text.extract(path)
    assert result.status == "partial"
    assert "filename" in result.note


def test_unknown_text_format_is_indexed(tmp_path: Path):
    path = tmp_path / "notes.xyz"
    path.write_text("plain readable content in an unknown extension")
    result = text.extract(path)
    assert "readable content" in result.text
    assert result.status == "partial"


def test_image_defers_to_photo_pipeline(tmp_path: Path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0stub")
    result = text.extract(path)
    assert result.status == "partial"
    assert result.needs_ocr


def test_missing_file_fails_gracefully(tmp_path: Path):
    result = text.extract(tmp_path / "nope.txt")
    assert result.status == "failed"


# ------------------------------------------------------------- passages

def test_passages_split_on_blank_lines():
    body = "First block here.\n\nSecond block here.\n\nThird block here."
    parts = passages.split(body, target_chars=25)
    assert len(parts) >= 2
    assert all(p.text.strip() for p in parts)


def test_passage_offsets_map_back_to_source():
    body = "alpha block\n\nbeta block\n\ngamma block"
    for part in passages.split(body, target_chars=15):
        excerpt = body[part.char_start:part.char_end]
        assert part.text.strip() in excerpt, "offsets must locate the passage"


def test_passages_carry_page_numbers():
    pages = ["Page one content.", "Page two content.", "Page three content."]
    parts = passages.split("\n\n".join(pages), pages=pages, target_chars=200)
    assert {p.page for p in parts} == {1, 2, 3}


def test_passages_never_span_pages():
    """A citation pointing at two pages is not a citation."""
    pages = ["short one", "short two", "short three"]
    parts = passages.split("\n\n".join(pages), pages=pages, target_chars=5000)
    assert len(parts) == 3
    assert [p.page for p in parts] == [1, 2, 3]


def test_ordinals_are_sequential():
    pages = ["a\n\nb", "c\n\nd"]
    parts = passages.split("\n\n".join(pages), pages=pages, target_chars=3)
    assert [p.ordinal for p in parts] == list(range(len(parts)))


def test_long_block_is_split_with_overlap():
    body = ". ".join(f"sentence number {i}" for i in range(200))
    parts = passages.split(body, target_chars=300, overlap_chars=50)
    assert len(parts) > 1
    assert all(len(p.text) < 700 for p in parts)


def test_empty_text_yields_no_passages():
    assert passages.split("") == []
    assert passages.split("   \n\n  ") == []


def test_headings_start_new_passages():
    body = "# Salary\n\nSome text.\n\n# Deductions\n\nOther text."
    parts = passages.split(body, target_chars=20)
    joined = [p.text for p in parts]
    assert any("Salary" in t for t in joined)
    assert any("Deductions" in t for t in joined)


def test_single_page_source_still_carries_page_number():
    """A Tier 1 answer is a value plus its citation; 'page 1' is part of that."""
    parts = passages.split("only page content here", pages=["only page content here"])
    assert parts
    assert all(p.page == 1 for p in parts)


def test_plain_text_has_no_page_number():
    """A .txt file has no pages; inventing one would be a false citation."""
    parts = passages.split("some plain text\n\nanother block")
    assert parts
    assert all(p.page is None for p in parts)
