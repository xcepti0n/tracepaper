"""Photo tagging (FR-11, D-005).

Photos are indexed by derived tags; the image itself is never copied or stored
-- the NAS already holds it.

Tags come from independent sources that each write to the same table with their
own `source`, so they can be re-run separately:

- **EXIF** -- timestamp, GPS, camera. Exact, free, and never re-derived.
- **Geocoding** -- GPS to place names.
- **OCR** -- text in the image, which is what makes a screenshot findable.
- **Caption** -- a VLM's description, for the fuzzy "occasion" dimension.

Only the last is model-dependent, so a better model in 2028 re-runs one job
type and leaves every exact fact and every name you assigned untouched.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PhotoTags:
    tags: list[tuple[str, str, str, float]] = field(default_factory=list)
    taken_at: str | None = None

    def add(self, namespace: str, value: str, source: str,
            confidence: float = 1.0) -> None:
        value = str(value).strip()
        if value:
            self.tags.append((namespace, value, source, confidence))


def is_photo(path: Path) -> bool:
    from .text import IMAGE_SUFFIXES
    return path.suffix.lower() in IMAGE_SUFFIXES


def extract_tags(path: Path) -> PhotoTags:
    """Derive tags from a photo. Never raises."""
    result = PhotoTags()
    try:
        _exif_tags(path, result)
    except Exception as exc:
        log.debug("EXIF extraction failed for %s: %s", path, exc)
    try:
        _place_tags(result)
    except Exception as exc:
        log.debug("geocoding failed for %s: %s", path, exc)
    return result


def _place_tags(result: PhotoTags) -> None:
    """GPS to place names, offline (D-005).

    "photos from Goa in 2019" needs a place name, and a coordinate is not one.
    Uses a bundled city database rather than a web service: no network call, no
    per-photo cost, and no home location leaving the LAN (NFR-3).
    """
    coords = next((value for namespace, value, _, _ in result.tags
                   if namespace == "gps"), None)
    if not coords:
        return
    try:
        import reverse_geocoder
    except ImportError:
        return

    latitude, longitude = (float(part) for part in coords.split(","))
    match = reverse_geocoder.search([(latitude, longitude)], mode=1)
    if not match:
        return

    place = match[0]
    for field_name, namespace in (("name", "place"), ("admin1", "region"),
                                  ("cc", "country")):
        value = place.get(field_name)
        if value:
            # Derived from an exact coordinate, so confidence stays high, but
            # it is a lookup rather than a measurement -- hence not 1.0.
            result.add(namespace, str(value), "geocode", 0.9)


def _exif_tags(path: Path, result: PhotoTags) -> None:
    """EXIF is exact: a caption guessing "summer day" is strictly worse than a
    timestamp stating 2019-03-14."""
    try:
        from PIL import ExifTags, Image
    except ImportError:
        return

    with Image.open(path) as image:
        raw = image.getexif()
        if not raw:
            return
        tags = {ExifTags.TAGS.get(k, k): v for k, v in raw.items()}

        taken = tags.get("DateTimeOriginal") or tags.get("DateTime")
        if taken:
            parsed = _parse_exif_datetime(str(taken))
            if parsed:
                result.taken_at = parsed
                result.add("year", parsed[:4], "exif")
                result.add("month", parsed[:7], "exif")

        for key, namespace in (("Make", "camera_make"), ("Model", "camera")):
            if tags.get(key):
                result.add(namespace, str(tags[key]).strip(), "exif")

        gps = raw.get_ifd(0x8825) if hasattr(raw, "get_ifd") else None
        if gps:
            coords = _gps_coordinates(gps)
            if coords:
                latitude, longitude = coords
                # Stored at reduced precision: a home address is not something
                # to scatter through a tag table.
                result.add("gps", f"{latitude:.4f},{longitude:.4f}", "exif")


def _parse_exif_datetime(value: str) -> str | None:
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _gps_coordinates(gps: dict) -> tuple[float, float] | None:
    def to_degrees(values) -> float | None:
        try:
            degrees, minutes, seconds = (float(v) for v in values)
            return degrees + minutes / 60 + seconds / 3600
        except (TypeError, ValueError):
            return None

    try:
        latitude = to_degrees(gps.get(2))
        longitude = to_degrees(gps.get(4))
        if latitude is None or longitude is None:
            return None
        if str(gps.get(1, "N")).upper().startswith("S"):
            latitude = -latitude
        if str(gps.get(3, "E")).upper().startswith("W"):
            longitude = -longitude
        return latitude, longitude
    except Exception:
        return None


def store_tags(conn: sqlite3.Connection, item_id: int, tags: PhotoTags) -> int:
    """Write derived tags, replacing only machine-derived ones.

    Human-assigned tags are never touched: a name you gave a photo outranks
    anything a model produces later (FR-10).
    """
    conn.execute("DELETE FROM tags WHERE item_id = ? AND source != 'human'",
                 (item_id,))

    written = 0
    for namespace, value, source, confidence in tags.tags:
        conn.execute(
            "INSERT INTO tags (item_id, namespace, value, source, confidence) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (item_id, namespace, value, source, confidence),
        )
        written += 1

    if tags.taken_at:
        conn.execute("UPDATE items SET created_at = ? WHERE id = ?",
                     (tags.taken_at, item_id))
    return written


def search_by_tags(conn: sqlite3.Connection, *, namespace: str | None = None,
                   value: str | None = None,
                   limit: int = 50) -> list[sqlite3.Row]:
    sql = ["SELECT DISTINCT i.id, i.title, i.uri, i.created_at",
           "FROM items i JOIN tags t ON t.item_id = i.id",
           "WHERE i.deleted_at IS NULL"]
    params: list[object] = []
    if namespace:
        sql.append("AND t.namespace = ?")
        params.append(namespace)
    if value:
        sql.append("AND t.value LIKE ?")
        params.append(f"%{value}%")
    sql.append("ORDER BY i.created_at DESC, i.id LIMIT ?")
    params.append(limit)
    return conn.execute(" ".join(sql), params).fetchall()


def name_cluster(conn: sqlite3.Connection, cluster_id: int, name: str) -> int:
    """Name a face cluster. Applies to every photo in it, past and future.

    This is human-authored data: it survives any re-run of the photo pipeline
    and is part of the backed-up layer (NFR-6).
    """
    from ..db import transaction

    with transaction(conn) as c:
        c.execute("UPDATE face_clusters SET name = ? WHERE cluster_id = ?",
                  (name, cluster_id))
        rows = c.execute(
            "SELECT DISTINCT item_id FROM faces WHERE cluster_id = ?",
            (cluster_id,)).fetchall()
        for row in rows:
            c.execute(
                "INSERT INTO tags (item_id, namespace, value, source, confidence) "
                "VALUES (?, 'person', ?, 'human', 1.0) ON CONFLICT DO NOTHING",
                (row["item_id"], name),
            )
    return len(rows)
