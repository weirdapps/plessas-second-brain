import sqlite3
from pathlib import Path
from unittest.mock import patch

from src.extract.image_classifier import Classification
from src.extract.image_vision import classify_with_vision, parse_vision_response
from src.store.schema import create_database, run_migrations


def _setup_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    run_migrations(conn)
    return conn


def test_parse_vision_response_content():
    label, desc = parse_vision_response("CONTENT: bar chart of revenue by region")
    assert label == Classification.CONTENT
    assert "bar chart" in desc


def test_parse_vision_response_decoration():
    label, desc = parse_vision_response("DECORATION: ACME logo")
    assert label == Classification.SIGNATURE
    assert "ACME logo" in desc


def test_parse_vision_response_invalid_falls_back_to_signature():
    label, desc = parse_vision_response("garbage output")
    assert label == Classification.SIGNATURE  # safe default — don't enrich extractor with junk


@patch("src.extract.claude_extract._get_client_and_model")
def test_classify_with_vision_uses_cache(mock_get_client, tmp_path):
    from PIL import Image

    img = tmp_path / "x.png"
    Image.new("RGB", (800, 600), "blue").save(img)
    db = _setup_db(tmp_path)
    from src.extract.image_classifier import sha256_of_file

    sha = sha256_of_file(img)
    db.execute(
        """INSERT INTO inline_images
           (sha256, width, height, bytes, classification, classification_method, classified_at, vision_description)
           VALUES (?, 800, 600, 1000, 'content', 'vision_llm', '2026-04-22T00:00:00Z', 'cached desc')""",
        (sha,),
    )
    db.commit()

    label, desc = classify_with_vision(img, conn=db)
    assert label == Classification.CONTENT
    assert desc == "cached desc"
    assert not mock_get_client.called
