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


# ------------------------------------------------------------- geocoding

def make_geo_photo(path: Path, date: str, lat_dms, lon_dms,
                   ns: str = "N", ew: str = "E") -> Path:
    pytest.importorskip("PIL.Image", reason="pillow not installed")
    from fractions import Fraction

    from PIL import Image

    img = Image.new("RGB", (80, 60), "skyblue")
    exif = img.getexif()
    exif[306] = date
    ifd = exif.get_ifd(0x8825)
    ifd[1] = ns
    ifd[2] = tuple(Fraction(x) for x in lat_dms)
    ifd[3] = ew
    ifd[4] = tuple(Fraction(x) for x in lon_dms)
    img.save(path, exif=exif)
    return path


requires_geocoder = pytest.mark.skipif(
    __import__("importlib").util.find_spec("reverse_geocoder") is None,
    reason="reverse_geocoder not installed")


@requires_geocoder
def test_gps_becomes_a_place_name(tmp_path: Path):
    """"photos from Goa" needs a place name; a coordinate is not one."""
    from fractions import Fraction

    path = make_geo_photo(tmp_path / "goa.jpg", "2019:12:25 14:30:00",
                          (15, 17, Fraction(5757, 100)),
                          (74, 7, Fraction(2640, 100)))

    tags = photos.extract_tags(path)
    by_namespace = {ns: value for ns, value, _, _ in tags.tags}

    assert by_namespace["region"] == "Goa"
    assert by_namespace["country"] == "IN"
    assert by_namespace["year"] == "2019"


@requires_geocoder
def test_geocoding_is_offline(tmp_path: Path, monkeypatch):
    """No network call, no API key, no home location leaving the LAN."""
    import socket
    from fractions import Fraction

    def blocked(*args, **kwargs):
        raise AssertionError("geocoding must not touch the network")

    monkeypatch.setattr(socket, "create_connection", blocked)
    path = make_geo_photo(tmp_path / "goa.jpg", "2019:12:25 14:30:00",
                          (15, 17, Fraction(5757, 100)),
                          (74, 7, Fraction(2640, 100)))

    tags = photos.extract_tags(path)

    assert any(ns == "region" for ns, _, _, _ in tags.tags)


@requires_geocoder
def test_place_and_year_narrow_together(conn, cfg, nas):
    """"photos from Goa in 2019" must not return the union of Goa and 2019."""
    from fractions import Fraction

    from datamanager.query.unified import UnifiedSearch

    make_geo_photo(nas / "goa_2019.jpg", "2019:12:25 14:30:00",
                   (15, 17, Fraction(5757, 100)), (74, 7, Fraction(2640, 100)))
    make_geo_photo(nas / "seattle_2021.jpg", "2021:06:10 09:00:00",
                   (47, 36, Fraction(2100, 100)),
                   (122, 19, Fraction(5900, 100)), ew="W")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    search = UnifiedSearch(conn)

    assert len(search.query("photos from Goa in 2019", semantic=False).photos) == 1
    assert len(search.query("Goa", semantic=False).photos) == 1
    assert len(search.query("photos 2021", semantic=False).photos) == 1
    assert search.query("Goa 2021", semantic=False).photos == [], \
        "filters must narrow together, not union"
