"""OCR for images and scanned PDFs (FR-2).

Screenshots, phone photos of receipts, and scanned documents carry no text
layer. Without OCR they are findable only by filename, which for a file called
`IMG_4021.jpg` means not findable at all.

Three backends, tried in order of what is actually available:

1. **macOS Vision** -- built into the OS, no install, very good quality.
   Reached through a small Swift/`osascript` shim rather than a Python binding
   so there is nothing to pip-install on the Mac.
2. **Tesseract** -- portable, needs `tesseract` on PATH plus `pytesseract`.
3. **None** -- the item stays `partial` with `needs_ocr` set, so pending work
   is visible rather than silently missing (NFR-4).

OCR runs on the ingest worker, never at query time.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Below this, OCR output is treated as noise rather than text. Scans of blank
# or near-blank pages otherwise inject garbage into the index.
MIN_CONFIDENCE = 0.30
MIN_CHARS = 8


@dataclass
class OcrResult:
    text: str
    backend: str
    confidence: float = 0.0

    @property
    def usable(self) -> bool:
        return (len(self.text.strip()) >= MIN_CHARS
                and self.confidence >= MIN_CONFIDENCE)


_VISION_SCRIPT = r'''
import sys, json
try:
    import Vision, Quartz
    from Foundation import NSURL
except Exception as exc:
    print(json.dumps({"error": f"pyobjc unavailable: {exc}"}))
    sys.exit(0)

url = NSURL.fileURLWithPath_(sys.argv[1])
src = Quartz.CGImageSourceCreateWithURL(url, None)
if src is None:
    print(json.dumps({"error": "cannot read image"}))
    sys.exit(0)
img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
if img is None:
    print(json.dumps({"error": "cannot decode image"}))
    sys.exit(0)

req = Vision.VNRecognizeTextRequest.alloc().init()
req.setRecognitionLevel_(1)          # accurate
req.setUsesLanguageCorrection_(True)
handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
ok, err = handler.performRequests_error_([req], None)
if not ok:
    print(json.dumps({"error": str(err)}))
    sys.exit(0)

lines, confs = [], []
for obs in (req.results() or []):
    cand = obs.topCandidates_(1)
    if cand and len(cand):
        lines.append(cand[0].string())
        confs.append(float(cand[0].confidence()))
print(json.dumps({
    "text": "\n".join(lines),
    "confidence": (sum(confs) / len(confs)) if confs else 0.0,
}))
'''


def available_backends() -> list[str]:
    """Which OCR backends this machine can actually use."""
    found: list[str] = []
    if _vision_available():
        found.append("vision")
    if _tesseract_available():
        found.append("tesseract")
    return found


def _vision_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


def _tesseract_available() -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract  # noqa: F401
        return True
    except Exception:
        return False


def ocr_image(path: Path) -> OcrResult:
    """OCR a single image file. Never raises."""
    if _vision_available():
        result = _ocr_vision(path)
        if result.usable:
            return result

    if _tesseract_available():
        result = _ocr_tesseract(path)
        if result.usable:
            return result

    return OcrResult(text="", backend="none", confidence=0.0)


def _ocr_vision(path: Path) -> OcrResult:
    """macOS Vision via a subprocess, so a crash cannot take the worker down."""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(_VISION_SCRIPT)
            script = fh.name
        proc = subprocess.run([sys.executable, script, str(path)],
                              capture_output=True, text=True, timeout=120)
        Path(script).unlink(missing_ok=True)

        if proc.returncode != 0 or not proc.stdout.strip():
            return OcrResult(text="", backend="vision", confidence=0.0)
        payload = json.loads(proc.stdout)
        if "error" in payload:
            log.debug("vision OCR unavailable for %s: %s", path, payload["error"])
            return OcrResult(text="", backend="vision", confidence=0.0)
        return OcrResult(text=payload.get("text", ""), backend="vision",
                         confidence=float(payload.get("confidence", 0.0)))
    except Exception as exc:
        log.debug("vision OCR failed for %s: %s", path, exc)
        return OcrResult(text="", backend="vision", confidence=0.0)


def _ocr_tesseract(path: Path) -> OcrResult:
    try:
        import pytesseract
        from PIL import Image

        with Image.open(path) as img:
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

        words, confs = [], []
        for word, conf in zip(data.get("text", []), data.get("conf", [])):
            try:
                score = float(conf)
            except (TypeError, ValueError):
                continue
            if word.strip() and score >= 0:
                words.append(word)
                confs.append(score / 100.0)

        return OcrResult(text=" ".join(words), backend="tesseract",
                         confidence=(sum(confs) / len(confs)) if confs else 0.0)
    except Exception as exc:
        log.debug("tesseract OCR failed for %s: %s", path, exc)
        return OcrResult(text="", backend="tesseract", confidence=0.0)


def ocr_pdf(path: Path, max_pages: int = 30) -> list[OcrResult]:
    """OCR a scanned PDF page by page, preserving page boundaries.

    Pages are kept separate so a citation can still name a page (FR-3).
    """
    pages = _render_pdf_pages(path, max_pages)
    if not pages:
        return []
    try:
        return [ocr_image(page) for page in pages]
    finally:
        for page in pages:
            page.unlink(missing_ok=True)
        if pages:
            parent = pages[0].parent
            try:
                parent.rmdir()
            except OSError:
                pass


def _render_pdf_pages(path: Path, max_pages: int) -> list[Path]:
    """Rasterise PDF pages to PNG. Uses whatever renderer is on the machine."""
    outdir = Path(tempfile.mkdtemp(prefix="dm_ocr_"))

    # pdftoppm (poppler) is the portable option and the fastest.
    if shutil.which("pdftoppm"):
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", "200", "-l", str(max_pages),
                 str(path), str(outdir / "page")],
                capture_output=True, timeout=300, check=True,
            )
            return sorted(outdir.glob("page*.png"))
        except Exception as exc:
            log.debug("pdftoppm failed for %s: %s", path, exc)

    # macOS ships sips, which handles single pages only -- enough for the
    # common case of a one-page scanned receipt.
    if sys.platform == "darwin" and shutil.which("sips"):
        try:
            out = outdir / "page-1.png"
            subprocess.run(["sips", "-s", "format", "png", str(path),
                            "--out", str(out)],
                           capture_output=True, timeout=120, check=True)
            if out.exists():
                return [out]
        except Exception as exc:
            log.debug("sips failed for %s: %s", path, exc)

    try:
        outdir.rmdir()
    except OSError:
        pass
    return []
