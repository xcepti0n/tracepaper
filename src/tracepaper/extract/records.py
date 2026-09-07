"""Whole-document record extraction (FR-3, FR-4, D-003).

A *record* is a group of facts that belong together, bound while the entire
document is in view. This is the point of the whole design: on a W-2 the tax
year sits in a header box and gross salary in Box 1, far apart. Extracting them
independently and pairing them later is exactly the failure mode that makes
plain search unreliable -- so they are bound here, once, and stored resolved.

Extractors run in priority order and all emit the same shape. Their outputs are
merged by source precedence:

    human > template > pattern > llm

`human` corrections are never overwritten (FR-10). The LLM layer (M6) plugs in
at the bottom and can be removed entirely without breaking anything above it.

Keys are open vocabulary (FR-4): extractors emit whatever a document actually
contains. There is no enumerated schema to maintain.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

log = logging.getLogger(__name__)

# Source precedence. Lower sorts first, and wins.
SOURCE_RANK = {"human": 0, "template": 1, "pattern": 2, "llm": 3}

EXTRACTOR_VERSION = "1"


@dataclass
class Field:
    key: str
    value_text: str
    value_num: float | None = None
    value_date: str | None = None
    unit: str | None = None
    confidence: float = 1.0
    char_start: int | None = None
    char_end: int | None = None


@dataclass
class Record:
    record_type: str
    fields: list[Field] = field(default_factory=list)
    source: str = "pattern"
    confidence: float = 1.0
    page: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    model_id: str | None = None

    def get(self, key: str) -> Field | None:
        for f in self.fields:
            if f.key == key:
                return f
        return None

    def keys(self) -> set[str]:
        return {f.key for f in self.fields}


# --------------------------------------------------------------- value parsing

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Amounts: optional currency symbol, thousands separators, optional decimals.
_AMOUNT = re.compile(r"(?:(?P<cur>[$€£₹])\s*)?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{1,2}|\d+)")

_CURRENCY_UNITS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}


def parse_amount(raw: str) -> tuple[float | None, str | None]:
    """Parse a currency amount. Returns (value, unit)."""
    match = _AMOUNT.search(raw)
    if not match:
        return None, None
    try:
        value = float(match.group("num").replace(",", ""))
    except ValueError:
        return None, None
    symbol = match.group("cur")
    unit = _CURRENCY_UNITS.get(symbol) if symbol else None
    if unit is None:
        upper = raw.upper()
        for code in ("USD", "EUR", "GBP", "INR", "CAD", "AUD"):
            if code in upper:
                unit = code
                break
    return value, unit


def parse_date(raw: str) -> str | None:
    """Parse a date to ISO-8601. Returns None when genuinely ambiguous.

    Deliberately conservative: a wrong date silently poisons a Tier 1 answer,
    so anything unclear is left for a later, better-informed extractor.
    """
    raw = raw.strip()

    # ISO first -- unambiguous.
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", raw)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # "12 June 2019" / "June 12, 2019"
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", raw)
    if m and m.group(2)[:3].lower() in _MONTHS:
        return _safe_date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()],
                          int(m.group(1)))

    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", raw)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return _safe_date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()],
                          int(m.group(2)))

    # Numeric with separators. Day/month order is locale-dependent, so only
    # accept it when one component is unambiguously > 12.
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b", raw)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if a > 12 >= b:
            return _safe_date(year, b, a)      # DD/MM/YYYY
        if b > 12 >= a:
            return _safe_date(year, a, b)      # MM/DD/YYYY
        return None                             # ambiguous: refuse to guess
    return None


def _safe_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_year(raw: str) -> int | None:
    m = re.search(r"\b(19\d{2}|20\d{2})\b", raw)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ extractors

class Extractor:
    """Base class. Subclasses return records or an empty list."""

    name = "extractor"
    source = "pattern"

    def matches(self, text: str, title: str) -> bool:
        raise NotImplementedError

    def extract(self, text: str, title: str) -> list[Record]:
        raise NotImplementedError


def _find(text: str, pattern: str, flags: int = re.IGNORECASE) -> re.Match | None:
    return re.search(pattern, text, flags)


def _labelled(text: str, label_pattern: str, value_pattern: str = r"([^\n]{1,80})",
              flags: int = re.IGNORECASE) -> re.Match | None:
    """Match `Label: value` where the value is on the same line.

    The label pattern must use non-capturing groups: the value is group(1), and
    a capturing alternation in the label would shift it to None.
    """
    match = re.search(rf"{label_pattern}\s*[:\-]?\s*{value_pattern}", text, flags)
    if match is None or match.group(1) is None:
        return None
    return match


class W2Extractor(Extractor):
    """US Form W-2 (template layer).

    The canonical case for whole-document binding: the tax year and Box 1 are
    far apart, and pairing them after the fact is what goes wrong.
    """

    name = "w2"
    source = "template"

    def matches(self, text: str, title: str) -> bool:
        blob = f"{title}\n{text}"
        return bool(
            re.search(r"\bW-?2\b", blob, re.IGNORECASE)
            and re.search(r"wage and tax statement|wages,\s*tips", blob, re.IGNORECASE)
        )

    def extract(self, text: str, title: str) -> list[Record]:
        fields: list[Field] = []

        year = None
        m = _labelled(text, r"tax\s*year", r"(\d{4})")
        if m:
            year = int(m.group(1))
        else:
            # W-2 forms often print the bare year in the header.
            m = re.search(r"(?:^|\n)[^\n]*\b(20\d{2})\b[^\n]*\n", text[:400])
            if m:
                year = int(m.group(1))
        if year:
            fields.append(Field(key="tax_year", value_text=str(year),
                                value_num=float(year),
                                char_start=m.start() if m else None,
                                char_end=m.end() if m else None))

        m = _labelled(text, r"(?:employer(?:'s)?\s*name|c\s+employer\s*name)")
        if m:
            employer = m.group(1).strip()
            if employer:
                fields.append(Field(key="employer", value_text=employer,
                                    char_start=m.start(1), char_end=m.end(1)))

        m = _find(text, r"employer\s*(?:EIN|ID)[^\d]{0,20}(\d{2}-?\d{7})")
        if m:
            fields.append(Field(key="employer_ein", value_text=m.group(1),
                                char_start=m.start(1), char_end=m.end(1)))

        # Numbered boxes. The label text varies between issuers, so both the
        # box number and a keyword are accepted.
        # Box labels vary between issuers, so each alternative is non-capturing
        # and the amount is the single capture group.
        boxes = [
            ("gross_salary",
             r"(?:\b1\s+wages,?\s*tips|wages,?\s*tips,?\s*other\s*compensation)"),
            ("federal_tax_withheld",
             r"(?:\b2\s+federal|federal\s*income\s*tax\s*withheld)"),
            ("social_security_wages",
             r"(?:\b3\s+social\s*security\s*wages|social\s*security\s*wages)"),
            ("medicare_wages",
             r"(?:\b5\s+medicare|medicare\s*wages)"),
        ]
        for key, label in boxes:
            m = re.search(rf"{label}[^\n\d]*([\d,]+\.\d{{2}}|[\d,]{{3,}})", text,
                          re.IGNORECASE)
            if not m or m.group(1) is None:
                continue
            value, unit = parse_amount(m.group(1))
            if value is not None:
                fields.append(Field(key=key, value_text=m.group(1).strip(),
                                    value_num=value, unit=unit or "USD",
                                    char_start=m.start(1), char_end=m.end(1)))

        if not fields:
            return []
        return [Record(record_type="tax_form_w2", fields=fields,
                       source=self.source, confidence=0.95)]


class PassportExtractor(Extractor):
    name = "passport"
    source = "template"

    def matches(self, text: str, title: str) -> bool:
        blob = f"{title}\n{text}"
        return bool(re.search(r"\bpassport\b", blob, re.IGNORECASE))

    def extract(self, text: str, title: str) -> list[Record]:
        fields: list[Field] = []

        m = _labelled(text, r"passport\s*(?:no\.?|number|#)", r"([A-Z0-9]{6,12})")
        if m and m.group(1):
            fields.append(Field(key="passport_number", value_text=m.group(1).strip(),
                                char_start=m.start(1), char_end=m.end(1)))

        for key, label in (("expiry_date", r"(?:date\s*of\s*)?expir(?:y|ation)(?:\s*date)?"),
                           ("issue_date", r"(?:date\s*of\s*)?issue(?:d)?(?:\s*date)?")):
            m = _labelled(text, label)
            if m and m.group(1):
                iso = parse_date(m.group(1))
                if iso:
                    fields.append(Field(key=key, value_text=m.group(1).strip(),
                                        value_date=iso,
                                        char_start=m.start(1), char_end=m.end(1)))

        m = _labelled(text, r"(?:nationality|citizenship)", r"([A-Za-z ]{2,40})")
        if m:
            fields.append(Field(key="nationality", value_text=m.group(1).strip(),
                                char_start=m.start(1), char_end=m.end(1)))

        if not fields:
            return []
        return [Record(record_type="passport", fields=fields,
                       source=self.source, confidence=0.9)]


class FlightExtractor(Extractor):
    """Flight confirmations (template layer).

    Airlines vary wildly in layout, so this matches on the facts a booking
    always carries -- a confirmation code, a flight number, a route -- rather
    than on any one carrier's format.
    """

    name = "flight"
    source = "template"

    _AIRLINES = {
        "alaska": "Alaska Airlines", "alaskaair": "Alaska Airlines",
        "united": "United Airlines", "delta": "Delta Air Lines",
        "american": "American Airlines", "southwest": "Southwest Airlines",
        "jetblue": "JetBlue", "lufthansa": "Lufthansa", "emirates": "Emirates",
        "british airways": "British Airways", "air canada": "Air Canada",
        "air india": "Air India", "indigo": "IndiGo", "klm": "KLM",
        "qatar": "Qatar Airways", "singapore airlines": "Singapore Airlines",
    }

    def matches(self, text: str, title: str) -> bool:
        blob = f"{title}\n{text}".lower()
        signals = (
            "confirmation" in blob or "itinerary" in blob or "boarding pass" in blob,
            bool(re.search(r"\bflight\b", blob)),
            bool(re.search(r"\b[A-Z]{3}\b\s*(?:to|-|→)\s*\b[A-Z]{3}\b", text)),
        )
        return sum(bool(s) for s in signals) >= 2

    def extract(self, text: str, title: str) -> list[Record]:
        fields: list[Field] = []
        blob = f"{title}\n{text}"

        for needle, canonical in self._AIRLINES.items():
            if needle in blob.lower():
                fields.append(Field(key="airline", value_text=canonical))
                break

        m = re.search(r"(?:confirmation|booking|reservation)\s*"
                      r"(?:code|number|no\.?|#|ref(?:erence)?)?\s*[:\-]?\s*"
                      r"\b([A-Z0-9]{5,8})\b", text, re.IGNORECASE)
        if m:
            fields.append(Field(key="confirmation_number", value_text=m.group(1),
                                char_start=m.start(1), char_end=m.end(1)))

        m = re.search(r"\bflight\s*(?:number|no\.?|#)?\s*[:\-]?\s*"
                      r"\b([A-Z]{2}\s?\d{1,4})\b", text, re.IGNORECASE)
        if m:
            fields.append(Field(key="flight_number",
                                value_text=m.group(1).replace(" ", ""),
                                char_start=m.start(1), char_end=m.end(1)))

        m = re.search(r"\b([A-Z]{3})\b\s*(?:to|-|→|–)\s*\b([A-Z]{3})\b", text)
        if m:
            fields.append(Field(key="origin", value_text=m.group(1)))
            fields.append(Field(key="destination", value_text=m.group(2)))
            fields.append(Field(key="route",
                                value_text=f"{m.group(1)}-{m.group(2)}"))

        for key, label in (("departure_date", r"depart(?:ure|s|ing)?"),
                           ("return_date", r"return(?:ing)?")):
            m = _labelled(text, rf"(?:{label})(?:\s*date)?")
            if m and m.group(1):
                iso = parse_date(m.group(1))
                if iso:
                    fields.append(Field(key=key, value_text=m.group(1).strip(),
                                        value_date=iso,
                                        char_start=m.start(1), char_end=m.end(1)))

        # A booking with no date and no code is not a booking.
        keys = {f.key for f in fields}
        if not (keys & {"confirmation_number", "flight_number"}):
            return []
        return [Record(record_type="flight_booking", fields=fields,
                       source=self.source, confidence=0.85)]


class ReceiptExtractor(Extractor):
    """Purchase receipts and invoices (template layer)."""

    name = "receipt"
    source = "template"

    def matches(self, text: str, title: str) -> bool:
        blob = f"{title}\n{text}".lower()
        has_total = bool(re.search(r"\b(?:total|amount due|balance due|grand total)\b",
                                   blob))
        has_marker = bool(re.search(r"\b(?:receipt|invoice|order|purchase|"
                                    r"transaction|subtotal|tax)\b", blob))
        return has_total and has_marker

    def extract(self, text: str, title: str) -> list[Record]:
        fields: list[Field] = []

        # Prefer the most specific total label present.
        for key, label in (("amount", r"(?:grand\s*total|total\s*due|balance\s*due)"),
                           ("amount", r"\btotal\b"),
                           ("subtotal", r"\bsub[\s-]?total\b"),
                           ("tax", r"\b(?:tax|vat|gst)\b")):
            if any(f.key == key for f in fields):
                continue
            m = re.search(rf"{label}\s*[:\-]?\s*([$€£₹]?\s*[\d,]+\.?\d{{0,2}})",
                          text, re.IGNORECASE)
            if m:
                value, unit = parse_amount(m.group(1))
                if value is not None:
                    fields.append(Field(key=key, value_text=m.group(1).strip(),
                                        value_num=value, unit=unit,
                                        char_start=m.start(1), char_end=m.end(1)))

        for key, label in (("invoice_number",
                            r"(?:invoice|receipt)\s*(?:number|no\.?|#|id)"),
                           ("order_number", r"order\s*(?:number|no\.?|#|id)")):
            m = _labelled(text, label, r"([A-Za-z0-9][A-Za-z0-9\-]{3,24})")
            if m and m.group(1):
                fields.append(Field(key=key, value_text=m.group(1).strip(),
                                    char_start=m.start(1), char_end=m.end(1)))

        m = _labelled(text, r"(?:date|date\s*of\s*(?:purchase|service|issue))")
        if m and m.group(1):
            iso = parse_date(m.group(1))
            if iso:
                fields.append(Field(key="document_date", value_text=m.group(1).strip(),
                                    value_date=iso,
                                    char_start=m.start(1), char_end=m.end(1)))

        m = _labelled(text, r"(?:merchant|vendor|store|sold\s*by|payee)")
        if m and m.group(1):
            fields.append(Field(key="merchant", value_text=m.group(1).strip(),
                                char_start=m.start(1), char_end=m.end(1)))
        else:
            # Receipts usually lead with the merchant name on the first line.
            first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            if first and len(first) <= 60 and not re.search(r"\d{3,}", first):
                fields.append(Field(key="merchant", value_text=first,
                                    confidence=0.5))

        if not any(f.key == "amount" for f in fields):
            return []
        return [Record(record_type="receipt", fields=fields,
                       source=self.source, confidence=0.8)]


class GenericLabelExtractor(Extractor):
    """Open-vocabulary fallback (FR-4).

    Harvests `Label: value` pairs from any document, whatever its type. This is
    what lets an unrecognised format still yield structured facts with no new
    code -- the property that keeps the system from needing updates as new
    document types appear.
    """

    name = "generic_labels"
    source = "pattern"

    # A label is a short run of words before a colon, at the start of a line.
    _PAIR = re.compile(
        r"^[ \t]*(?P<label>[A-Za-z][A-Za-z0-9 /'&().-]{2,40}?)\s*:[ \t]*(?P<value>\S[^\n]{0,120})$",
        re.MULTILINE,
    )

    # Labels that carry no fact, or that belong to other layers.
    _SKIP = {"note", "notes", "subject", "from", "to", "cc", "bcc", "re",
             "http", "https", "www", "date", "sent", "received"}

    def matches(self, text: str, title: str) -> bool:
        return bool(text.strip())

    def extract(self, text: str, title: str) -> list[Record]:
        fields: list[Field] = []
        seen: set[str] = set()

        for match in self._PAIR.finditer(text):
            label = match.group("label").strip()
            value = match.group("value").strip()
            key = normalize_key(label)

            if not key or key in seen or key in self._SKIP or len(value) < 1:
                continue
            # A value that is itself a label line is a formatting artefact.
            if value.endswith(":"):
                continue
            seen.add(key)

            item = Field(key=key, value_text=value, confidence=0.6,
                         char_start=match.start("value"), char_end=match.end("value"))

            iso = parse_date(value)
            if iso:
                item.value_date = iso
            elif not _is_identifier(key, value):
                num, unit = parse_amount(value)
                # Only treat it as a number when the value is essentially just
                # that number -- "Seattle, WA 98101" is not an amount.
                if num is not None and len(re.sub(r"[\d,.\s$€£₹]", "", value)) <= 3:
                    item.value_num = num
                    item.unit = unit
            fields.append(item)

        if not fields:
            return []
        return [Record(record_type="document", fields=fields,
                       source=self.source, confidence=0.6)]


# Keys whose values are identifiers, not quantities. Typing them as numbers
# would let them into SUM() and range filters, where they mean nothing.
_IDENTIFIER_KEY = re.compile(
    r"(?:^|_)(?:id|no|num|number|code|ref|reference|serial|account|acct|"
    r"chip|microchip|policy|invoice|receipt|order|tracking|confirmation|"
    r"licence|license|passport|vin|imei|iban|swift|routing|zip|postcode|"
    r"phone|tel|mobile|fax|ssn|ein|tin|pan|aadhaar)(?:$|_)"
)


def _is_identifier(key: str, value: str) -> bool:
    """True when a value is an identifier rather than a measurable quantity."""
    if _IDENTIFIER_KEY.search(key):
        return True
    # Long unbroken digit runs are identifiers; real amounts carry separators
    # or decimals well before this length.
    digits = re.sub(r"\D", "", value)
    return len(digits) >= 11 and "." not in value


def normalize_key(label: str) -> str:
    """Fold a human label to a vocabulary key: lowercase, underscored."""
    key = label.strip().lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    key = re.sub(r"_+", "_", key).strip("_")
    return key


# The registry. Order is documentation: templates before patterns, and the
# open-vocabulary fallback last.
EXTRACTORS: list[Extractor] = [
    W2Extractor(),
    PassportExtractor(),
    FlightExtractor(),
    ReceiptExtractor(),
    GenericLabelExtractor(),
]


def extract_records(text: str, title: str = "", *,
                    llm_config=None, min_fields: int = 3) -> list[Record]:
    """Run every matching extractor over the whole document.

    Records from higher-precedence sources win per key; lower-precedence
    extractors only fill gaps. Nothing is discarded silently -- a lower-ranked
    record keeps any key its betters did not supply.

    The LLM layer (M6) runs last and only when the deterministic layers came up
    thin, so the expensive path is reserved for prose that needs it.
    """
    if not text.strip():
        return []

    produced: list[Record] = []
    for extractor in EXTRACTORS:
        try:
            if extractor.matches(text, title):
                produced.extend(extractor.extract(text, title))
        except Exception:
            # One bad extractor must not cost the document its other records --
            # but it must never fail silently either, or a document quietly
            # yields nothing and nobody finds out.
            log.exception("extractor %r failed on %r", extractor.name, title)
            continue

    merged = _merge(produced)

    # Free prose yields little or nothing above. Ask the model only then.
    harvested = sum(len(r.fields) for r in merged)
    if llm_config is not None and harvested < min_fields:
        try:
            from . import llm
            merged = _merge(produced + llm.extract(text, llm_config))
        except Exception:
            log.exception("LLM extraction failed on %r", title)

    return merged


def _merge(records: list[Record]) -> list[Record]:
    """Drop fields already supplied by a higher-precedence record.

    Precedence is per key, not per record: a template record claiming
    `gross_salary` does not suppress a pattern record's unrelated keys.
    """
    ordered = sorted(records, key=lambda r: (SOURCE_RANK.get(r.source, 99),
                                             -r.confidence))
    claimed: set[str] = set()
    out: list[Record] = []

    for record in ordered:
        kept = [f for f in record.fields if f.key not in claimed]
        if not kept:
            continue
        claimed.update(f.key for f in kept)
        out.append(Record(
            record_type=record.record_type, fields=kept, source=record.source,
            confidence=record.confidence, page=record.page,
            char_start=record.char_start, char_end=record.char_end,
            model_id=record.model_id,
        ))
    return out
