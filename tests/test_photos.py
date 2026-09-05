"""Photo tagging (FR-11, D-005)."""

from __future__ import annotations

from pathlib import Path

import pytest

from datamanager.extract import photos
from datamanager.index.indexer import Indexer
from datamanager.scan.scanner import Scanner


def make_photo(path: Path, *, taken: str | None = None) -> Path:
    Image = pytest.importorskip("PIL.Image", reason="pillow not installed")
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (80, 60), "skyblue")
    if taken:
        exif = img.getexif()
        exif[306] = taken                  # DateTime
        exif[271] = "TestCamera"           # Make
        img.save(path, exif=exif)
    else:
        img.save(path)
    return path


def test_photo_detection():
    assert photos.is_photo(Path("a.jpg"))
    assert photos.is_photo(Path("a.HEIC"))
    assert not photos.is_photo(Path("a.pdf"))


def test_exif_date_becomes_year_and_month_tags(tmp_path: Path):
    """EXIF is exact -- a caption guessing "summer" is strictly worse."""
    path = make_photo(tmp_path / "trip.jpg", taken="2019:03:14 10:22:00")

    tags = photos.extract_tags(path)

    assert tags.taken_at == "2019-03-14"
    namespaces = {namespace: value for namespace, value, _, _ in tags.tags}
    assert namespaces["year"] == "2019"
    assert namespaces["month"] == "2019-03"


def test_photo_without_exif_is_not_an_error(tmp_path: Path):
    tags = photos.extract_tags(make_photo(tmp_path / "plain.jpg"))
    assert tags.taken_at is None


def test_photo_is_indexed_as_a_photo(conn, cfg, nas):
    make_photo(nas / "holiday.jpg", taken="2019:03:14 10:22:00")

    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    row = conn.execute("SELECT kind FROM items").fetchone()
    assert row["kind"] == "photo"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tags WHERE namespace = 'year'"
    ).fetchone()["n"] == 1


def test_image_bytes_are_never_stored(conn, cfg, nas):
    """FR-11: the NAS already holds the photo."""
    make_photo(nas / "big.jpg", taken="2020:01:01 00:00:00")

    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    row = conn.execute(
        "SELECT text FROM item_versions WHERE valid_to IS NULL").fetchone()
    text = (row["text"] or "") if row else ""
    assert len(text) < 500, "a photo must not store its pixels as text"


def test_searchable_by_year(conn, cfg, nas):
    make_photo(nas / "a.jpg", taken="2019:07:04 12:00:00")
    make_photo(nas / "b.jpg", taken="2021:07:04 12:00:00")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    found = photos.search_by_tags(conn, namespace="year", value="2019")

    assert len(found) == 1
    assert found[0]["title"] == "a.jpg"


def test_human_tags_survive_reindexing(conn, cfg, nas):
    """A name you assigned outranks anything a model derives later (FR-10)."""
    make_photo(nas / "family.jpg", taken="2019:03:14 10:22:00")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    item_id = int(conn.execute("SELECT id FROM items").fetchone()["id"])

    conn.execute(
        "INSERT INTO tags (item_id, namespace, value, source, confidence) "
        "VALUES (?, 'person', 'Mum', 'human', 1.0)", (item_id,))

    Indexer(conn, cfg).reindex_item(item_id)

    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tags WHERE source = 'human'"
    ).fetchone()["n"] == 1, "a hand-assigned tag must survive re-extraction"


def test_naming_a_cluster_tags_every_photo_in_it(conn, cfg, nas):
    """Name a face once; it applies to every photo matched to that cluster."""
    make_photo(nas / "a.jpg")
    make_photo(nas / "b.jpg")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    conn.execute("INSERT INTO face_clusters (cluster_id, name) VALUES (1, NULL)")
    for row in conn.execute("SELECT id FROM items").fetchall():
        conn.execute("INSERT INTO faces (item_id, cluster_id) VALUES (?, 1)",
                     (row["id"],))

    tagged = photos.name_cluster(conn, 1, "Priya")

    assert tagged == 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tags WHERE namespace='person' AND value='Priya'"
    ).fetchone()["n"] == 2
