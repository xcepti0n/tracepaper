"""OCR for images and scanned PDFs (FR-2).

Screenshots and phone photos of receipts are ordinary in a personal archive.
Without OCR a file called IMG_4021.jpg is findable only by a name that says
nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracepaper.extract import ocr, text
from tracepaper.index.indexer import Indexer
from tracepaper.query.search import SearchEngine
from tracepaper.scan.scanner import Scanner

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
    from tracepaper.query.fields import FieldQuery

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


# ------------------------------------------------- vision-model fallback ----

class _Cfg:
    """Minimal stand-in: _vlm_configured only reads these three."""
    def __init__(self, enabled=True, endpoint="http://x:11434", model="gemma4:e4b"):
        self.llm_enabled = enabled
        self.llm_endpoint = endpoint
        self.vlm_model = model


def test_the_vision_model_only_runs_when_it_is_configured():
    """Same switch as photo captions: one control in Settings, not two."""
    assert ocr._vlm_configured(_Cfg())
    assert not ocr._vlm_configured(_Cfg(enabled=False))
    assert not ocr._vlm_configured(_Cfg(endpoint=""))
    assert not ocr._vlm_configured(_Cfg(model=""))


def test_the_vision_model_is_the_last_resort_not_the_first(tmp_path, monkeypatch):
    """It is orders of magnitude slower than tesseract, and a real OCR engine
    beats it on a clean scan. It must only see what the others could not read."""
    Image = pytest.importorskip("PIL.Image", reason="pillow not installed")
    path = tmp_path / "scan.png"
    Image.new("RGB", (40, 40), "white").save(path)

    called = []
    monkeypatch.setattr(ocr, "_vision_available", lambda: False)
    monkeypatch.setattr(ocr, "_tesseract_available", lambda: True)
    monkeypatch.setattr(ocr, "_ocr_tesseract", lambda p: ocr.OcrResult(
        text="a perfectly readable scan", backend="tesseract", confidence=0.9))
    monkeypatch.setattr(ocr, "_ocr_vlm",
                        lambda p, c: called.append(p) or ocr.OcrResult(
                            text="x", backend="vlm", confidence=0.9))

    result = ocr.ocr_image(path, cfg=_Cfg())
    assert result.backend == "tesseract"
    assert not called, "the vision model must not run when tesseract succeeded"


def test_the_vision_model_runs_when_tesseract_finds_nothing(tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image", reason="pillow not installed")
    path = tmp_path / "visa.jpg"
    Image.new("RGB", (40, 40), "white").save(path)

    monkeypatch.setattr(ocr, "_vision_available", lambda: False)
    monkeypatch.setattr(ocr, "_tesseract_available", lambda: True)
    monkeypatch.setattr(ocr, "_ocr_tesseract",
                        lambda p: ocr.OcrResult(text="", backend="tesseract"))
    monkeypatch.setattr(ocr, "_ocr_vlm", lambda p, c: ocr.OcrResult(
        text="H1B VISA UNITED STATES OF AMERICA", backend="vlm",
        confidence=0.35))

    result = ocr.ocr_image(path, cfg=_Cfg())
    assert result.backend == "vlm"
    assert "H1B" in result.text


def test_a_described_image_is_never_stored_as_its_text():
    """Asked to read an image with no text, a vision model narrates instead.
    That prose is fluent and entirely invented, and storing it would make the
    index claim a document says something it does not."""
    from tracepaper.extract import vision

    for narration in (
            "The image shows a passport lying on a wooden table.",
            "This appears to be a photograph of an identity document.",
            "I cannot read any text in this image.",
            "There is no legible text visible in the picture.",
            "NO_TEXT",
    ):
        assert vision._usable_transcription(narration) == "", narration


def test_a_real_transcription_survives_the_guard():
    from tracepaper.extract import vision

    real = ("INCOME TAX DEPARTMENT\nGOVT. OF INDIA\n"
            "Permanent Account Number\nABCDE1234F")
    assert vision._usable_transcription(real) == real


def test_the_guard_does_not_reject_text_that_merely_mentions_an_image():
    """Only the opening is checked: a transcription containing the phrase
    further in is still a transcription."""
    from tracepaper.extract import vision

    text = ("CLAIM FORM SECTION 4\nAttach a photograph. "
            "The image shows the damaged item as described above.")
    assert vision._usable_transcription(text) == text


def test_the_vision_model_never_claims_a_real_confidence(tmp_path, monkeypatch):
    """The model gives no calibrated score. Inventing a high one would let it
    clear the noise thresholds that exist to keep garbage out of the index."""
    from tracepaper.extract import vision

    monkeypatch.setattr(vision, "transcribe",
                        lambda p, **kw: "SOME REAL DOCUMENT TEXT HERE")
    result = ocr._ocr_vlm(tmp_path / "x.jpg", _Cfg())
    assert result.usable
    assert result.confidence < 0.5, "must not masquerade as a confident read"
    assert result.backend == "vlm", "the source of the text must be recorded"


def test_the_vision_fallback_is_budgeted_per_run(tmp_path, monkeypatch):
    """Each call is ~20 seconds. A photo import queued 51,031 images at once,
    which uncapped would occupy the model for weeks and starve captions, which
    share the same Ollama."""
    Image = pytest.importorskip("PIL.Image", reason="pillow not installed")
    path = tmp_path / "x.jpg"
    Image.new("RGB", (30, 30), "white").save(path)

    monkeypatch.setattr(ocr, "_vision_available", lambda: False)
    monkeypatch.setattr(ocr, "_tesseract_available", lambda: True)
    monkeypatch.setattr(ocr, "_ocr_tesseract",
                        lambda p: ocr.OcrResult(text="", backend="tesseract"))
    monkeypatch.setattr(ocr, "MAX_VLM_PER_RUN", 3)

    calls = []
    monkeypatch.setattr(ocr, "_ocr_vlm", lambda p, c: calls.append(1) or
                        ocr.OcrResult(text="", backend="vlm"))

    ocr.reset_vlm_budget()
    for _ in range(10):
        ocr.ocr_image(path, cfg=_Cfg())
    assert len(calls) == 3, "the budget must cap calls within one run"

    ocr.reset_vlm_budget()
    ocr.ocr_image(path, cfg=_Cfg())
    assert len(calls) == 4, "a new run must get a fresh budget"
