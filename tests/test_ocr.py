"""OCR for images and scanned PDFs (FR-2).

Screenshots and phone photos of receipts are ordinary in a personal archive.
Without OCR a file called IMG_4021.jpg is findable only by a name that says
nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from datamanager.extract import ocr, text
from datamanager.index.indexer import Indexer
from datamanager.query.search import SearchEngine
from datamanager.scan.scanner import Scanner

RECEIPT_LINES = [
    "HOME DEPOT STORE 4521",
    "Date: 2024-05-02",
    "Order Number: W52188301",
    "Subtotal 213.97",
    "Total 235.80",
]


def make_receipt_image(path: Path) -> Path:
    """Render a receipt-like image, the way a screenshot would look."""
    Image = pytest.importorskip("PIL.Image", reason="pillow not installed")
    from PIL import ImageDraw, ImageFont

    img = Image.new("RGB", (760, 300), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial.ttf", 26)
    except Exception:
        font = ImageFont.load_default()
    for i, line in enumerate(RECEIPT_LINES):
        draw.text((25, 20 + i * 50), line, fill="black", font=font)
    img.save(path)
    return path


requires_ocr = pytest.mark.skipif(
    not ocr.available_backends(), reason="no OCR backend on this machine")


def test_backends_are_reported():
    """Callers must be able to tell whether OCR is possible at all."""
    assert isinstance(ocr.available_backends(), list)


@requires_ocr
def test_screenshot_is_ocred(tmp_path: Path):
    path = make_receipt_image(tmp_path / "receipt.png")

    result = ocr.ocr_image(path)

    assert result.usable
    assert "HOME DEPOT" in result.text.upper()
    assert "235.80" in result.text


@requires_ocr
def test_screenshot_becomes_searchable_end_to_end(conn, cfg, nas):
    """The whole chain: OCR to text to passages to search."""
    make_receipt_image(nas / "IMG_4021.png")

    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    hits = SearchEngine(conn).search("home depot").hits
    assert len(hits) == 1
    assert hits[0].title == "IMG_4021.png"


@requires_ocr
def test_ocr_text_yields_records(conn, cfg, nas):
    """Fields extract from OCR output like any other text."""
    from datamanager.query.fields import FieldQuery

    make_receipt_image(nas / "receipt.png")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    assert FieldQuery(conn).get("order_number").best is not None


def test_unreadable_image_is_partial_not_failed(conn, cfg, nas):
    """A blank or unreadable image must keep its place in the index."""
    Image = pytest.importorskip("PIL.Image", reason="pillow not installed")
    Image.new("RGB", (60, 60), "white").save(nas / "blank.png")

    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    row = conn.execute("SELECT extraction_status FROM items").fetchone()
    assert row["extraction_status"] == "partial", "pending OCR must stay visible"
    assert len(SearchEngine(conn).search("blank").hits) == 1, "findable by name"


def test_missing_backend_degrades_cleanly(tmp_path: Path, monkeypatch):
    """With no OCR installed, an image is partial -- never an error."""
    Image = pytest.importorskip("PIL.Image", reason="pillow not installed")
    path = tmp_path / "photo.png"
    Image.new("RGB", (40, 40), "white").save(path)

    monkeypatch.setattr(ocr, "available_backends", lambda: [])
    monkeypatch.setattr(ocr, "ocr_image",
                        lambda p: ocr.OcrResult(text="", backend="none"))

    result = text.extract(path)

    assert result.status == "partial"
    assert result.needs_ocr
