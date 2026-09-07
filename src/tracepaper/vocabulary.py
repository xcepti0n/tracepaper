"""Key canonicalization (FR-4).

Open vocabulary means extractors invent their own keys, so the same fact
arrives under several names: `gross_salary`, `wages_tips_other_compensation`,
`box_1_wages`. Without canonicalization the vocabulary drifts and a query for
one name misses documents that used another.

Canonicalization is deterministic and inspectable -- no model, no embeddings.
Aliases are proposed by rule, the user can pin any mapping, and a pinned
mapping is never overridden.
"""

from __future__ import annotations

import re
import sqlite3

from .db import transaction

# Noise that appears in extracted labels but carries no meaning. Form box
# numbers are the common case: "box_1_wages" and "wages" are one key.
_NOISE_PREFIX = re.compile(r"^(?:box|line|item|field|no|nr)_?\d+_?")
_NOISE_WORDS = {"the", "a", "an", "of", "for", "your", "total", "amount"}

# Facts that recur across document types under many names. Mapping these by
# hand is worth it: they are what people actually search for.
SEED_ALIASES: dict[str, tuple[str, ...]] = {
    "gross_salary": (
        "wages_tips_other_compensation", "wages_tips", "gross_pay", "gross_wages",
        "gross_income", "wages", "box_1_wages_tips_other_compensation",
        "total_wages", "salary",
    ),
    "federal_tax_withheld": (
        "federal_income_tax_withheld", "box_2_federal_income_tax_withheld",
        "fed_tax_withheld", "federal_withholding", "federal_tax",
    ),
    "social_security_wages": (
        "box_3_social_security_wages", "ss_wages", "socsec_wages",
    ),
    "medicare_wages": ("box_5_medicare_wages", "medicare_wages_and_tips"),
    "expiry_date": (
        "date_of_expiry", "expiration_date", "date_of_expiration", "expires",
        "expires_on", "valid_until", "valid_thru", "good_through",
    ),
    "issue_date": (
        "date_of_issue", "issued", "issued_on", "date_issued", "issued_date",
    ),
    "amount": (
        "total_due", "amount_due", "total_amount", "amount_paid", "total_paid",
        "grand_total", "balance_due", "total", "charge", "price",
    ),
    "merchant": ("vendor", "payee", "seller", "store", "retailer", "business"),
    "account_number": ("account_no", "acct_number", "acct_no", "account"),
    "invoice_number": ("invoice_no", "invoice_id", "bill_number"),
    "document_date": ("date_of_service", "service_date", "transaction_date",
                      "statement_date", "date_of_purchase", "purchase_date"),
    "employer": ("employer_name", "company", "company_name", "organisation",
                 "organization"),
    "confirmation_number": ("confirmation_code", "confirmation", "booking_reference",
                            "booking_code", "reservation_number", "pnr", "record_locator"),
}


def _build_seed_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for canonical, aliases in SEED_ALIASES.items():
        out[canonical] = canonical
        for alias in aliases:
            out[alias] = canonical
    return out


SEED_MAP = _build_seed_map()


def canonical_form(key: str) -> str:
    """Rule-based canonical form, used when no explicit mapping exists.

    Strips form box numbers and filler words so `box_2_federal_income_tax`
    and `federal_income_tax` converge.
    """
    stripped = _NOISE_PREFIX.sub("", key)
    parts = [p for p in stripped.split("_") if p and p not in _NOISE_WORDS]
    return "_".join(parts) if parts else key


def resolve(conn: sqlite3.Connection, key: str) -> str:
    """Canonical key for a raw key, honouring stored and pinned mappings."""
    row = conn.execute(
        "SELECT canonical_key FROM key_vocabulary WHERE key = ?", (key,)
    ).fetchone()
    if row:
        return row["canonical_key"]

    if key in SEED_MAP:
        return SEED_MAP[key]

    reduced = canonical_form(key)
    return SEED_MAP.get(reduced, reduced)


def register(conn: sqlite3.Connection, key: str) -> str:
    """Record a key sighting and return its canonical form.

    A user-pinned mapping is never overwritten (FR-10 applies to vocabulary
    too: a decision the user made by hand outranks any rule).
    """
    row = conn.execute(
        "SELECT canonical_key, pinned_by_user FROM key_vocabulary WHERE key = ?",
        (key,),
    ).fetchone()

    if row is not None:
        conn.execute(
            "UPDATE key_vocabulary SET occurrences = occurrences + 1 WHERE key = ?",
            (key,),
        )
        return row["canonical_key"]

    canonical = SEED_MAP.get(key) or SEED_MAP.get(canonical_form(key)) \
        or canonical_form(key)
    conn.execute(
        "INSERT INTO key_vocabulary (key, canonical_key, occurrences, pinned_by_user) "
        "VALUES (?, ?, 1, 0)",
        (key, canonical),
    )
    return canonical


def pin(conn: sqlite3.Connection, key: str, canonical: str) -> None:
    """Force a mapping by hand. Survives every future rule change."""
    with transaction(conn) as c:
        c.execute(
            "INSERT INTO key_vocabulary (key, canonical_key, occurrences, pinned_by_user) "
            "VALUES (?, ?, 0, 1) ON CONFLICT(key) DO UPDATE SET "
            "canonical_key = excluded.canonical_key, pinned_by_user = 1",
            (key, canonical),
        )
        # Existing rows must move with the mapping, or old data stays
        # unreachable under the new name.
        c.execute(
            "UPDATE record_fields SET key = ? WHERE key = ?", (canonical, key)
        )


def merge(conn: sqlite3.Connection, source_key: str, target_key: str) -> int:
    """Merge one key into another, rewriting stored fields. Returns rows moved."""
    with transaction(conn) as c:
        cur = c.execute(
            "UPDATE record_fields SET key = ? WHERE key = ?", (target_key, source_key)
        )
        moved = cur.rowcount
        c.execute(
            "INSERT INTO key_vocabulary (key, canonical_key, occurrences, pinned_by_user) "
            "VALUES (?, ?, 0, 1) ON CONFLICT(key) DO UPDATE SET "
            "canonical_key = excluded.canonical_key, pinned_by_user = 1",
            (source_key, target_key),
        )
    return moved


def suggestions(conn: sqlite3.Connection, limit: int = 50) -> list[tuple[str, str, int]]:
    """Keys whose canonical form differs from themselves -- merge candidates."""
    rows = conn.execute(
        "SELECT key, canonical_key, occurrences FROM key_vocabulary "
        "WHERE key != canonical_key AND pinned_by_user = 0 "
        "ORDER BY occurrences DESC, key LIMIT ?",
        (limit,),
    ).fetchall()
    return [(r["key"], r["canonical_key"], int(r["occurrences"])) for r in rows]
