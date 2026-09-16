"""Object and scene tagging for photos (FR-11, D-005).

Answers "photos of the dog", "beach pictures", "that whiteboard photo" -- the
searches EXIF and OCR cannot serve, because nothing in the file says what is
in the picture.

Two backends, in order:

1. **macOS Vision** -- `VNClassifyImageRequest` gives ~1300 object and scene
   labels, plus animal detection and a people count. Built into the OS, no
   download, fast enough to run over a whole library.
2. **A vision LLM** (Ollama) -- a caption in prose for anything Vision was
   unsure about. Slower, so it is reserved for that gap.

This is the one photo layer that depends on a model, which is why it writes
tags with its own `source` and re-runs independently. A better model in 2028
re-runs this job type alone; every EXIF fact, place name and person you named
stays exactly as it is.
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Vision returns 1300 labels for every image, nearly all of them noise. Only
# labels the model is actually confident about are worth storing.
MIN_CONFIDENCE = 0.15
MAX_LABELS = 12

# Labels too generic to help anyone search. "outdoor" matches half a library.
_TOO_GENERIC = {
    "material", "structure", "object", "background", "surface", "texture",
    "pattern", "abstract", "art", "design", "color", "light", "shape",
    "celestial_body", "natural_object", "man_made", "indoor", "outdoor",
    "scene", "environment", "landscape_scene", "no_person",
}


@dataclass
class VisionTags:
    labels: list[tuple[str, float]] = field(default_factory=list)
    animals: list[str] = field(default_factory=list)
    people_count: int = 0
    caption: str = ""
    backend: str = "none"

    @property
    def is_empty(self) -> bool:
        return not (self.labels or self.animals or self.people_count
                    or self.caption)


_CLASSIFY_SCRIPT = r'''
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

handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, {})
out = {"labels": [], "animals": [], "people": 0}

classify = Vision.VNClassifyImageRequest.alloc().init()
animals = Vision.VNRecognizeAnimalsRequest.alloc().init()
humans = Vision.VNDetectHumanRectanglesRequest.alloc().init()

ok, err = handler.performRequests_error_([classify, animals, humans], None)
if not ok:
    print(json.dumps({"error": str(err)}))
    sys.exit(0)

for obs in (classify.results() or []):
    confidence = float(obs.confidence())
    if confidence >= 0.05:
        out["labels"].append([str(obs.identifier()), confidence])

for obs in (animals.results() or []):
    for label in (obs.labels() or []):
        out["animals"].append(str(label.identifier()))

out["people"] = len(humans.results() or [])
print(json.dumps(out))
'''


def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


def classify(path: Path, *, timeout: int = 60) -> VisionTags:
    """Tag a photo's contents. Never raises."""
    result = VisionTags()
    if not available():
        return result

    try:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(_CLASSIFY_SCRIPT)
            script = fh.name
        # A subprocess, so a crash in the vision framework cannot take the
        # ingest worker down mid-library.
        proc = subprocess.run([sys.executable, script, str(path)],
                              capture_output=True, text=True, timeout=timeout)
        Path(script).unlink(missing_ok=True)

        if proc.returncode != 0 or not proc.stdout.strip():
            return result
        payload = json.loads(proc.stdout)
        if "error" in payload:
            log.debug("vision classify unavailable for %s: %s", path,
                      payload["error"])
            return result
    except Exception as exc:
        log.debug("vision classify failed for %s: %s", path, exc)
        return result

    result.backend = "vision"
    result.labels = _useful_labels(payload.get("labels", []))
    result.animals = sorted({a.lower() for a in payload.get("animals", [])})
    result.people_count = int(payload.get("people", 0))
    return result


def _useful_labels(raw: list) -> list[tuple[str, float]]:
    """Keep only labels specific and confident enough to be worth searching."""
    kept: list[tuple[str, float]] = []
    for entry in raw:
        try:
            identifier, confidence = str(entry[0]).lower(), float(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if confidence < MIN_CONFIDENCE or identifier in _TOO_GENERIC:
            continue
        kept.append((identifier, confidence))

    kept.sort(key=lambda pair: (-pair[1], pair[0]))
    return kept[:MAX_LABELS]


# Vision models see a few hundred pixels square; a 12MP phone photo is ~50x
# more data than the model can use. Sending the original costs upload time,
# base64 bloat and encode time for nothing.
CAPTION_MAX_PIXELS = 1024


def _downscaled(path: Path, max_pixels: int = CAPTION_MAX_PIXELS) -> bytes:
    """The image, shrunk to something a vision model can actually use.

    Falls back to the original bytes when Pillow is missing or the file is not
    a readable image -- a caption from a slow upload beats no caption.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            if max(img.size) <= max_pixels:
                return path.read_bytes()
            img = img.convert("RGB")
            img.thumbnail((max_pixels, max_pixels), Image.LANCZOS)
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()
    except Exception as exc:
        log.debug("could not downscale %s: %s", path, exc)
        return path.read_bytes()


# Some models accept an image, then answer as if none arrived. The MLX build of
# gemma4:e4b does this on every photo, and gemma4:26b-mlx is worse: it invents
# a confident caption ("a group of people wearing suits") for an image it never
# saw. A wrong caption is stored as searchable text and is far more damaging
# than no caption, so both shapes are rejected here rather than trusted.
_BLIND_MARKERS = (
    "no image was provided",
    "image was not provided",
    "cannot describe the image",
    "can't describe the image",
    "unable to describe",
    "i need an image",
    "i need the image",
    "please provide the image",
    "as an ai",
    "i cannot see",
    "i can't see",
    "there is no image",
    "no image was attached",
    "didn't receive an image",
    "did not receive an image",
)

# The shortest genuine description of a photo still runs to a few words. A
# terse refusal that dodges the markers above is caught by length.
MIN_CAPTION_CHARS = 15


def _usable_caption(text: str) -> str:
    """The caption, or "" when the model plainly did not see the image.

    Em dashes are stripped because captions are rendered in the UI, and a
    model's punctuation choices should not leak into the interface.
    """
    cleaned = text.strip()
    if len(cleaned) < MIN_CAPTION_CHARS:
        return ""
    lowered = cleaned.lower()
    if any(marker in lowered for marker in _BLIND_MARKERS):
        log.warning("model returned a no-image reply, discarding: %r",
                    cleaned[:120])
        return ""
    for dash in ("\u2014", "\u2013"):
        cleaned = cleaned.replace(dash, ", ")
    return re.sub(r"\s+", " ", cleaned).strip()


def caption(path: Path, *, endpoint: str, model: str,
            timeout: int = 180) -> str:
    """A prose caption from a vision LLM. Returns "" on any failure.

    Used only where Vision's labels were thin, since this is orders of
    magnitude slower.
    """
    import base64
    import urllib.request

    try:
        encoded = base64.b64encode(_downscaled(path)).decode()
        body = json.dumps({
            "model": model,
            "prompt": ("Describe this image in one short sentence. State only "
                       "what is visibly present. Do not speculate about the "
                       "occasion, the people, or the location."),
            "images": [encoded],
            "stream": False,
            "options": {"temperature": 0.0, "seed": 42},
        }).encode()

        request = urllib.request.Request(
            f"{endpoint}/api/generate", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read())
        return _usable_caption(str(reply.get("response", "")))[:300]
    except Exception as exc:
        log.debug("caption failed for %s: %s", path, exc)
        return ""


def to_tags(result: VisionTags) -> list[tuple[str, str, str, float]]:
    """Convert to (namespace, value, source, confidence) rows."""
    rows: list[tuple[str, str, str, float]] = []

    for identifier, confidence in result.labels:
        rows.append(("object", identifier, result.backend or "vision", confidence))
    for animal in result.animals:
        # Animal detection is a dedicated, reliable model -- higher confidence
        # than a general scene label.
        rows.append(("animal", animal, result.backend or "vision", 0.9))
    if result.people_count:
        rows.append(("people", str(result.people_count),
                     result.backend or "vision", 0.9))
        rows.append(("has", "people", result.backend or "vision", 0.9))
    if result.caption:
        rows.append(("caption", result.caption, "vlm", 0.5))
    return rows


# ---------------------------------------------------------------- VLM OCR ---
# Tesseract reads clean scans well and photographed documents badly: a PAN
# card at an angle, a visa stamp in a passport, a voter ID under glare. A
# vision model handles those, so it runs only where Tesseract found nothing,
# never instead of it. It is far slower, which is exactly why it is last.
#
# This is ingest, never the query path. Search itself stays deterministic.

_OCR_PROMPT = (
    "Transcribe all text visible in this image, exactly as it appears. "
    "Preserve line breaks. Do not translate, summarise, correct or explain "
    "anything. If there is no legible text, reply with exactly: NO_TEXT"
)

# A model asked to transcribe an image with no text tends to narrate instead
# ("a photograph of a document on a table"). That prose would be indexed as if
# it were the document's own text, so it is rejected.
_NARRATION_MARKERS = (
    "the image shows", "this image shows", "the image depicts",
    "this appears to be", "the photo shows", "there is no legible",
    "no text is visible", "i cannot", "i'm unable", "unable to read",
    "no_text",
)

MIN_OCR_CHARS = 12


def _usable_transcription(text: str) -> str:
    """Keep a transcription only when it looks like transcribed text.

    The failure this guards against is not a wrong character here and there,
    it is the model describing the picture instead of reading it. That prose
    is fluent, confident and completely fabricated as far as the index is
    concerned, and it would be stored as the document's searchable text.
    """
    cleaned = text.strip()
    if len(cleaned) < MIN_OCR_CHARS:
        return ""

    lowered = cleaned.lower()
    for marker in _BLIND_MARKERS:
        if marker in lowered:
            log.warning("vlm ocr: model reported no image, discarding")
            return ""
    # Only a narration that OPENS the reply counts. A real form can easily
    # contain "the image shows" partway through ("Attach a photograph. The
    # image shows the damaged item"), and rejecting that would throw away a
    # genuine transcription. A model that is describing rather than reading
    # starts that way from the first word.
    for marker in _NARRATION_MARKERS:
        if lowered.startswith(marker):
            log.info("vlm ocr: model described the image instead of reading "
                     "it, discarding: %r", cleaned[:100])
            return ""
    return cleaned


def transcribe(path: Path, *, endpoint: str, model: str,
               timeout: int = 180) -> str:
    """Read the text out of an image with a vision model. "" on any failure.

    Deliberately separate from caption(): that one asks for a description and
    is stored as a caption, this one asks for a transcription and is stored as
    the document's text. Mixing them would put invented prose in the index.
    """
    import base64
    import urllib.request

    try:
        encoded = base64.b64encode(_downscaled(path)).decode()
        body = json.dumps({
            "model": model,
            "prompt": _OCR_PROMPT,
            "images": [encoded],
            "stream": False,
            # Zero temperature: this is a reading task, and any sampling
            # freedom here shows up as invented characters.
            "options": {"temperature": 0.0, "seed": 42},
        }).encode()
        request = urllib.request.Request(
            f"{endpoint}/api/generate", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read())
        return _usable_transcription(str(reply.get("response", "")))
    except Exception as exc:
        log.debug("vlm ocr failed for %s: %s", path, exc)
        return ""
