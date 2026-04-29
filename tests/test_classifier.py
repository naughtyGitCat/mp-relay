"""Pure-regex tests for the classifier — no I/O, no MP/qBT, no env."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.classifier import classify, is_jav_text


def test_jav_magnet_dn_detected():
    kind, hints = classify("magnet:?xt=urn:btih:abc&dn=SSIS-001-Hot")
    assert kind == "jav_magnet"
    assert "SSIS-001" in hints["name"]


def test_regular_magnet_falls_through():
    kind, hints = classify("magnet:?xt=urn:btih:abc&dn=Big%20Buck%20Bunny")
    assert kind == "magnet"
    assert hints["name"] == "Big Buck Bunny"


def test_magnet_without_dn():
    kind, hints = classify("magnet:?xt=urn:btih:abcdef")
    assert kind == "magnet"


def test_torrent_url():
    kind, _ = classify("https://example.com/foo.torrent")
    assert kind == "torrent"


def test_jav_torrent_url():
    kind, _ = classify("https://example.com/SSIS-001.torrent")
    assert kind == "jav_torrent"


def test_bare_jav_codes():
    for code in ("SSIS-001", "FC2-PPV-1234567", "FC2PPV-1234567",
                 "121319_001", "HEYZO-1234"):
        kind, hints = classify(code)
        assert kind == "jav_code", f"{code} → {kind}"


def test_id_ref_tmdb():
    kind, hints = classify("tmdb:762504")
    assert kind == "id_ref"
    assert hints["id_type"] == "tmdbid"
    assert hints["id_value"] == "762504"


def test_id_ref_tmdb_url_movie():
    kind, hints = classify("https://www.themoviedb.org/movie/762504")
    assert kind == "id_ref"
    assert hints["id_type"] == "tmdbid"
    assert hints["id_value"] == "762504"
    assert hints["media_type"] == "movie"


def test_id_ref_tmdb_url_tv():
    kind, hints = classify("https://www.themoviedb.org/tv/243141")
    assert kind == "id_ref"
    assert hints["media_type"] == "tv"


def test_id_ref_imdb():
    kind, hints = classify("tt12345678")
    assert kind == "id_ref"
    assert hints["id_type"] == "imdbid"


def test_id_ref_douban_url():
    kind, hints = classify("https://movie.douban.com/subject/35783036/")
    assert kind == "id_ref"
    assert hints["id_type"] == "doubanid"


def test_media_name_chinese():
    kind, hints = classify("繁花")
    assert kind == "media_name"
    assert hints["keyword"] == "繁花"


def test_media_name_english_year():
    kind, hints = classify("Big Buck Bunny 2008 1080p BluRay")
    assert kind == "media_name"


def test_is_jav_text_positive():
    assert is_jav_text("SSIS-001 Hot Title")
    assert is_jav_text("FC2-PPV-1234567")


def test_is_jav_text_negative():
    assert not is_jav_text("The Matrix 1999")
    assert not is_jav_text("繁花 第一季")


def test_is_jav_text_case_insensitive():
    """Regression: lowercase JAV codes used to fall through to media_name
    because _JAV_PATTERNS[0] required uppercase. Real user typed 'snos-073'
    and got 0 candidates instead of routing to jav_code (2026-04-29)."""
    assert is_jav_text("snos-073")
    assert is_jav_text("Snos-073")
    assert is_jav_text("ssis-001 hot title")
    assert is_jav_text("fc2-ppv-1234567")


def test_classify_lowercase_jav_code_routes_to_jav_code():
    """End-to-end classifier check for the snos-073 case."""
    kind, hints = classify("snos-073")
    assert kind == "jav_code"
    assert hints["code"] == "SNOS-073"   # normalised to upper
