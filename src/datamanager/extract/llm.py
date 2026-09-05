"""LLM extraction for free prose (D-003, D-010, FR-4).

The bottom layer of the extractor stack. Templates and patterns handle
documents with labelled values; this handles the rest -- a contractor's quote
in an email, a letter, meeting notes -- where the facts are in prose and no
`Label: value` pattern exists.

Three properties keep this from becoming the staleness problem the project
exists to avoid:

1. **Ingest only.** Never called at query time. Stopping the endpoint changes
   no search result.
2. **Extraction, not judgment.** The prompt asks only "what facts are stated
   here", never for ranking, interpretation, or an answer. A small or old
   model does this adequately, so a better model improves recall at the margin
   rather than being required.
3. **Schema-validated output.** Anything that fails validation is discarded,
   never stored. A hallucinated field is worse than a missing one.

The endpoint is any OpenAI-compatible or Ollama HTTP server, so it can be
swapped for another model or host without touching pipeline code.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .records import Field, Record, normalize_key, parse_amount, parse_date

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:e4b-mlx"
DEFAULT_TIMEOUT = 120

# Prose is sent whole so facts stay bound (FR-3), but a very long document is
# truncated: models degrade on long inputs, and the head of a document is where
# its identifying facts almost always are.
MAX_CHARS = 12_000

PROMPT = """Extract factual key-value pairs from the document below.

Rules:
- Return ONLY a JSON object: {"record_type": "...", "fields": {"key": "value"}}
- record_type is a short lowercase label for what this document is
  (for example: quote, invoice, letter, lease, statement, notes).
- Keys are lowercase_with_underscores, describing the fact.
- Values are exactly as written in the document. Do not calculate, convert,
  summarise, or infer anything.
- Include only facts explicitly stated in the text.
- If the document states no clear facts, return {"record_type": "document",
  "fields": {}}.

Document:
---
{text}
---

JSON:"""


@dataclass
class LlmConfig:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    timeout: int = DEFAULT_TIMEOUT
    enabled: bool = False

    @property
    def model_id(self) -> str:
        return f"{self.model}@{self.endpoint}"


def available(config: LlmConfig) -> bool:
    """Whether the endpoint is reachable. Never raises."""
    if not config.enabled:
        return False
    try:
        request = urllib.request.Request(f"{config.endpoint}/api/tags")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def extract(text: str, config: LlmConfig) -> list[Record]:
    """Extract records from prose. Returns [] on any failure.

    An unreachable endpoint or a malformed reply must never cost the document
    its other layers, so every failure is logged and swallowed here.
    """
    if not config.enabled or not text.strip():
        return []

    payload = _call(text[:MAX_CHARS], config)
    if payload is None:
        return []

    record = _validate(payload, config.model_id)
    return [record] if record else []


def _call(text: str, config: LlmConfig) -> dict | None:
    body = json.dumps({
        "model": config.model,
        "prompt": PROMPT.replace("{text}", text),
        "stream": False,
        "format": "json",          # Ollama constrains output to valid JSON
        "options": {
            # Deterministic decoding: the same document must extract the same
            # way twice, or re-ingest silently changes stored data.
            "temperature": 0.0,
            "seed": 42,
        },
    }).encode()

    try:
        request = urllib.request.Request(
            f"{config.endpoint}/api/generate", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            reply = json.loads(response.read())
    except urllib.error.URLError as exc:
        log.warning("LLM endpoint unreachable (%s): %s", config.endpoint, exc)
        return None
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return None

    raw = reply.get("response", "")
    return _parse_json(raw)


def _parse_json(raw: str) -> dict | None:
    """Parse the model's reply, tolerating prose wrapped around the JSON."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        log.debug("no JSON object in LLM reply: %.200s", raw)
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        log.debug("malformed JSON in LLM reply: %.200s", raw)
        return None


# A key must look like a key, not a sentence the model decided to emit.
# Word count is the discriminator: real field names are one to four words
# ("gross_salary", "date_of_expiry"), while a summary sentence is many.
_VALID_KEY = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
_MAX_KEY_WORDS = 5


def _is_key_shaped(key: str) -> bool:
    return bool(_VALID_KEY.match(key)) and key.count("_") < _MAX_KEY_WORDS

# Keys that would collide with layers that know better, or that invite the
# model to summarise rather than extract.
_REJECTED_KEYS = {"summary", "description_of_document", "notes", "content",
                  "text", "document", "analysis", "conclusion", "opinion"}


def _validate(payload: dict, model_id: str) -> Record | None:
    """Turn a model reply into a Record, discarding anything unsound.

    Conservative on purpose: a hallucinated field enters the index as fact and
    outlives the model that invented it, so anything doubtful is dropped.
    """
    if not isinstance(payload, dict):
        return None

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        return None

    record_type = payload.get("record_type")
    if not isinstance(record_type, str) or not _is_key_shaped(
            normalize_key(record_type) or ""):
        record_type = "document"
    else:
        record_type = normalize_key(record_type)

    fields: list[Field] = []
    for raw_key, raw_value in raw_fields.items():
        if not isinstance(raw_key, str):
            continue
        key = normalize_key(raw_key)
        if not _is_key_shaped(key) or key in _REJECTED_KEYS:
            continue

        # Scalars only. A nested object or list means the model restructured
        # the document rather than extracting facts from it.
        if isinstance(raw_value, (dict, list)):
            continue
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if not value or len(value) > 200:
            continue

        field = Field(key=key, value_text=value, confidence=0.5)

        iso = parse_date(value)
        if iso:
            field.value_date = iso
        else:
            from .records import _is_identifier
            if not _is_identifier(key, value):
                number, unit = parse_amount(value)
                if number is not None and len(
                        re.sub(r"[\d,.\s$€£₹]", "", value)) <= 3:
                    field.value_num = number
                    field.unit = unit
        fields.append(field)

    if not fields:
        return None

    return Record(record_type=record_type, fields=fields, source="llm",
                  confidence=0.5, model_id=model_id)
