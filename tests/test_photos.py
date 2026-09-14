"""Photo tagging (FR-11, D-005)."""

from __future__ import annotations

import io

from pathlib import Path

import pytest

from tracepaper.extract import photos
from tracepaper.index.indexer import Indexer
from tracepaper.scan.scanner import Scanner


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
    assert by_namespace["year"] == "2019"
    # Both the country code and the name are stored: nobody searches for "IN",
    # and the code stays so an index built before the names existed still
    # matches. A dict keeps only the last, so check the tag list.
    countries = {value for ns, value, _, _ in tags.tags if ns == "country"}
    assert countries == {"IN", "India"}
    # admin2 is how people name a place when the city is unfamiliar.
    assert by_namespace["district"] == "South Goa"


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

    from tracepaper.query.unified import UnifiedSearch

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


def _photo_client(cfg, nas, name="photo.jpg", size=(1200, 1800)):
    """A client whose config actually has `nas` as a root, plus one photo row."""
    from dataclasses import replace

    from fastapi.testclient import TestClient
    from PIL import Image

    from tracepaper.api import create_app, open_connection

    path = nas / name
    Image.new("RGB", size, (90, 140, 200)).save(path)

    client = TestClient(create_app(replace(cfg, roots=[str(nas)])))
    conn = open_connection()
    try:
        conn.execute(
            "INSERT INTO items (kind, uri, title, mime, extraction_status) "
            "VALUES ('photo', ?, ?, 'image/jpeg', 'complete')", (str(path), name))
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return client, item_id, path


def test_thumbnail_is_a_small_jpeg_not_the_original(cfg, nas):
    """Serving originals would push tens of megabytes per results row, and HEIC
    -- most of a phone library -- does not render in a browser at all, so the
    grid would show broken images for exactly the photos most likely to match."""
    import io

    from PIL import Image

    client, item_id, path = _photo_client(cfg, nas)
    response = client.get(f"/thumb/{item_id}?size=320")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert len(response.content) < path.stat().st_size, "must be smaller"

    thumb = Image.open(io.BytesIO(response.content))
    assert max(thumb.size) <= 320
    assert thumb.size[0] < thumb.size[1], "aspect ratio must be preserved"


def test_thumbnail_size_is_clamped(cfg, nas):
    """`size` comes from the query string, so it must not become a way to ask
    the server to render something enormous."""
    import io

    from PIL import Image

    client, item_id, _ = _photo_client(cfg, nas)
    thumb = Image.open(io.BytesIO(client.get(f"/thumb/{item_id}?size=99999").content))
    assert max(thumb.size) <= 1024


def test_thumbnail_refuses_a_path_outside_the_roots(cfg, nas):
    """Same guarantee as /file: a stored uri is not a capability to read the
    filesystem."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from tracepaper.api import create_app, open_connection

    client = TestClient(create_app(replace(cfg, roots=[str(nas)])))
    conn = open_connection()
    try:
        conn.execute("INSERT INTO items (kind, uri, title, extraction_status) "
                     "VALUES ('photo', '/etc/passwd', 'x', 'complete')")
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    assert client.get(f"/thumb/{item_id}").status_code == 403


def test_an_unrenderable_file_does_not_500_the_page(cfg, nas):
    """A thumbnail is a convenience. A file that is not really an image must
    fail its own request, not break the results grid."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from tracepaper.api import create_app, open_connection

    path = nas / "not-really.jpg"
    path.write_text("this is not an image")

    client = TestClient(create_app(replace(cfg, roots=[str(nas)])))
    conn = open_connection()
    try:
        conn.execute("INSERT INTO items (kind, uri, title, mime, "
                     "extraction_status) VALUES ('photo', ?, 'x', "
                     "'image/jpeg', 'complete')", (str(path),))
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    response = client.get(f"/thumb/{item_id}")
    assert response.status_code == 422
    assert "preview" in response.json()["detail"]


def test_a_photo_is_found_by_its_written_description(conn, cfg, nas):
    """Captions are the only text most photos will ever have.

    Tags are single words, so "sandy" is unreachable by tag even when the
    photo is plainly of a sandy beach. The caption knows, and until now it
    was written to the index and never read back.
    """
    from tracepaper.db import transaction
    from tracepaper.query.unified import UnifiedSearch

    with transaction(conn) as c:
        c.execute("INSERT INTO items (id, kind, uri, title, extraction_status) "
                  "VALUES (1, 'photo', ?, 'IMG_4821.jpg', 'complete')",
                  (str(nas / "IMG_4821.jpg"),))
        c.execute("INSERT INTO tags (item_id, namespace, value, source, "
                  "confidence) VALUES (1, 'caption', "
                  "'a dog running on a sandy beach', 'vlm', 0.5)")

    result = UnifiedSearch(conn).query("sandy beach", semantic=False)

    assert result.photos, "the caption should have been searched"
    assert result.photos[0]["item_id"] == 1
    assert "description" in result.photo_filters


def test_every_caption_word_must_match(conn, cfg, nas):
    """"dog beach" must not return every photo that merely has a dog."""
    from tracepaper.db import transaction
    from tracepaper.query.unified import UnifiedSearch

    with transaction(conn) as c:
        for item_id, caption in (
                (1, "a dog running on a sandy beach"),
                (2, "a dog asleep on the sofa")):
            c.execute("INSERT INTO items (id, kind, uri, title, "
                      "extraction_status) VALUES (?, 'photo', ?, ?, 'complete')",
                      (item_id, str(nas / f"{item_id}.jpg"), f"{item_id}.jpg"))
            c.execute("INSERT INTO tags (item_id, namespace, value, source, "
                      "confidence) VALUES (?, 'caption', ?, 'vlm', 0.5)",
                      (item_id, caption))

    found = UnifiedSearch(conn).query("dog beach", semantic=False).photos

    assert [p["item_id"] for p in found] == [1]


def test_the_medium_word_is_not_searched_for(conn, cfg, nas):
    """"photos of a dog" asks for photos of a dog, not photos of "photo"."""
    from tracepaper.db import transaction
    from tracepaper.query.unified import UnifiedSearch

    with transaction(conn) as c:
        c.execute("INSERT INTO items (id, kind, uri, title, extraction_status) "
                  "VALUES (1, 'photo', ?, '1.jpg', 'complete')",
                  (str(nas / "1.jpg"),))
        c.execute("INSERT INTO tags (item_id, namespace, value, source, "
                  "confidence) VALUES (1, 'caption', 'a dog', 'vlm', 0.5)")

    assert UnifiedSearch(conn).query("photos of a dog", semantic=False).photos


def test_captions_downscale_large_images(tmp_path):
    """A 12MP upload per photo is why captioning was too slow to enable."""
    Image = pytest.importorskip("PIL.Image")
    from tracepaper.extract.vision import CAPTION_MAX_PIXELS, _downscaled

    big = tmp_path / "big.jpg"
    Image.new("RGB", (4000, 3000), "blue").save(big)

    shrunk = _downscaled(big)

    assert len(shrunk) < big.stat().st_size
    with Image.open(io.BytesIO(shrunk)) as img:
        assert max(img.size) <= CAPTION_MAX_PIXELS


# A model that answers without seeing the image is the one failure that makes
# the index worse instead of merely incomplete. Measured on this machine:
# gemma4:e4b-mlx replies "no image was provided" to every photo, and
# gemma4:26b-mlx invents a confident caption for an image it never saw.
@pytest.mark.parametrize("reply", [
    "I cannot describe the image because no image was provided.",
    "Please provide the image you would like me to describe.",
    "I need an image to describe it.",
    "I need the image to provide a description.",
    "There is no image attached to describe.",
    "As an AI, I am unable to describe images.",
])
def test_no_image_replies_are_never_stored_as_captions(reply):
    from tracepaper.extract.vision import _usable_caption

    assert _usable_caption(reply) == ""


def test_a_real_caption_survives_the_guard():
    """The guard must not be so eager that it drops genuine descriptions."""
    from tracepaper.extract.vision import _usable_caption

    for good in (
        "A red house with a brown roof stands on a tan field under a sun.",
        "Three stacked colored bars beneath a yellow circle.",
        # "not provided" in a caption about a form must not trip the guard,
        # which is why the marker is the image-specific phrasing.
        "A tax form where the date is not provided.",
        "A dog on a beach.",
    ):
        assert _usable_caption(good) == " ".join(good.split())


def test_captions_carry_no_em_dashes_into_the_ui():
    """gemma4:e4b writes em dashes, and captions are rendered on the page."""
    from tracepaper.extract.vision import _usable_caption

    out = _usable_caption(
        "A white field containing three stripes—red, green and blue—"
        "and a yellow circle.")

    assert "—" not in out and "–" not in out
    assert "stripes, red" in out


def test_a_terse_refusal_is_rejected_on_length():
    from tracepaper.extract.vision import _usable_caption

    assert _usable_caption("No.") == ""
    assert _usable_caption("") == ""


def test_photos_are_findable_by_a_two_word_place(conn, cfg, nas):
    """Plenty of real place names are two words, and matching one token at a
    time could never find them: a photo tagged New Delhi was unreachable by
    that name, and so was anything in the United States."""
    from tracepaper.query.unified import UnifiedSearch

    with conn:
        c = conn.execute(
            "INSERT INTO items (kind, uri, title, extraction_status) "
            "VALUES ('photo', ?, 'trip.jpg', 'complete')",
            (str(nas / "trip.jpg"),))
        item_id = c.lastrowid
        for namespace, value in (("place", "New Delhi"),
                                 ("region", "NCT"),
                                 ("country", "India")):
            conn.execute(
                "INSERT INTO tags (item_id, namespace, value, source, "
                "confidence) VALUES (?, ?, ?, 'geocode', 0.9)",
                (item_id, namespace, value))

    search = UnifiedSearch(conn)
    assert search.query("photos from New Delhi", semantic=False).photos, (
        "a two-word place name must match")
    # The single-word forms still work, and so does the country name.
    assert search.query("India photos", semantic=False).photos


def test_country_names_are_searchable_not_just_codes():
    """The geocoder returns "IN". Nobody types that."""
    from tracepaper.extract.photos import COUNTRY_NAMES

    assert COUNTRY_NAMES["IN"] == "India"
    assert COUNTRY_NAMES["US"] == "United States"
    # Codes map to a single name, never a list, or the tag would be ambiguous.
    assert all(isinstance(v, str) for v in COUNTRY_NAMES.values())


def test_photo_results_are_ordered_by_relevance_not_date(conn, cfg, nas):
    """SQL ordered only by created_at and truncated, so the newest photos came
    back whatever was typed and the same handful appeared for every query. A
    caption match must outrank a photo that merely shares a tag."""
    from tracepaper.query.unified import UnifiedSearch

    with conn:
        for index, (name, caption) in enumerate((
                ("old_match.jpg", "a dog running on a beach"),
                ("new_nomatch.jpg", None))):
            c = conn.execute(
                "INSERT INTO items (kind, uri, title, created_at, "
                "extraction_status) VALUES ('photo', ?, ?, ?, 'complete')",
                (str(nas / name), name, f"20{20 + index}-01-01"))
            item_id = c.lastrowid
            conn.execute(
                "INSERT INTO tags (item_id, namespace, value, source, "
                "confidence) VALUES (?, 'object', 'dog', 'vision', 0.9)",
                (item_id,))
            if caption:
                conn.execute(
                    "INSERT INTO tags (item_id, namespace, value, source, "
                    "confidence) VALUES (?, 'caption', ?, 'vlm', 0.5)",
                    (item_id, caption))

    photos = UnifiedSearch(conn).query("dog on a beach", semantic=False).photos

    assert photos, "expected photo results"
    # The older photo wins because its description matches, despite the other
    # being newer.
    assert photos[0]["title"] == "old_match.jpg"
