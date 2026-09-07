"""Background enrichment: object tags, captions, idle-awareness (FR-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracepaper import enrich
from tracepaper.extract import vision
from tracepaper.index.indexer import Indexer
from tracepaper.query.unified import UnifiedSearch
from tracepaper.scan.scanner import Scanner

requires_vision = pytest.mark.skipif(
    not vision.available(), reason="macOS Vision not available")


def make_photo(path: Path, colour: str = "skyblue") -> Path:
    pytest.importorskip("PIL.Image", reason="pillow not installed")
    from PIL import Image

    Image.new("RGB", (200, 150), colour).save(path)
    return path


def test_busy_detection_reads_load_average():
    assert isinstance(enrich.system_busy(), bool)
    # An absurd threshold is never exceeded; a zero one always is.
    assert not enrich.system_busy(threshold=10_000)
    assert enrich.system_busy(threshold=0.0)


def test_enrichment_yields_when_the_machine_is_busy(conn, cfg, nas):
    """Enrichment must never compete with what the user is doing."""
    make_photo(nas / "a.jpg")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    enricher = enrich.Enricher(conn, cfg, respect_load=True, load_threshold=0.0)
    result = enricher.run()

    assert result.photos_tagged == 0
    assert result.skipped_busy >= 1, "it should report yielding, not silently idle"


def test_label_filtering_drops_noise():
    """Vision returns 1300 labels per image; almost all are useless."""
    kept = vision._useful_labels([
        ["dog", 0.9], ["material", 0.8], ["outdoor", 0.7],
        ["beach", 0.4], ["quantum_physics", 0.01],
    ])
    identifiers = [label for label, _ in kept]

    assert "dog" in identifiers
    assert "beach" in identifiers
    assert "material" not in identifiers, "too generic to search on"
    assert "outdoor" not in identifiers
    assert "quantum_physics" not in identifiers, "below the confidence floor"


def test_labels_are_ordered_by_confidence():
    kept = vision._useful_labels([["cat", 0.3], ["dog", 0.9], ["beach", 0.6]])
    assert [label for label, _ in kept] == ["dog", "beach", "cat"]


def test_tag_conversion_shapes():
    result = vision.VisionTags(labels=[("dog", 0.8)], animals=["dog"],
                              people_count=2, backend="vision")
    rows = vision.to_tags(result)
    namespaces = {namespace for namespace, _, _, _ in rows}

    assert "object" in namespaces
    assert "animal" in namespaces
    assert "people" in namespaces


@requires_vision
def test_photos_get_object_tags(conn, cfg, nas):
    make_photo(nas / "sky.jpg")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    result = enrich.Enricher(conn, cfg, respect_load=False).run()

    assert result.photos_tagged == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM tags WHERE namespace = 'object'"
    ).fetchone()["n"] >= 1


@requires_vision
def test_enrichment_is_idempotent(conn, cfg, nas):
    """Re-running must not redo work, so a nightly job is cheap."""
    make_photo(nas / "sky.jpg")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    enricher = enrich.Enricher(conn, cfg, respect_load=False)
    first = enricher.run()
    second = enricher.run()

    assert first.photos_tagged == 1
    assert second.photos_tagged == 0


@requires_vision
def test_object_tags_are_searchable_and_combine_with_other_filters(conn, cfg, nas):
    make_photo(nas / "sky.jpg")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()
    enrich.Enricher(conn, cfg, respect_load=False).run()

    labels = [r["value"] for r in conn.execute(
        "SELECT value FROM tags WHERE namespace = 'object' AND value != '_none'")]
    if not labels:
        pytest.skip("Vision produced no confident labels for this image")

    result = UnifiedSearch(conn).query(labels[0], semantic=False)

    assert result.photos
    assert any(f.startswith("object=") for f in result.photo_filters)


@requires_vision
def test_unclassifiable_photo_is_not_retried_forever(conn, cfg, nas):
    """A photo Vision cannot label must not be reprocessed on every run."""
    make_photo(nas / "blank.jpg", colour="white")
    Scanner(conn, cfg).scan(nas)
    Indexer(conn, cfg).run_pending()

    enricher = enrich.Enricher(conn, cfg, respect_load=False)
    enricher.run()
    second = enricher.run()

    assert second.photos_tagged == 0
