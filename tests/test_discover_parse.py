"""Tests for HTML parsing in discover.py — uses fixture HTML so no network required."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


_ACTOR_SEARCH_HTML = """
<html><body>
<a class="avatar-box" href="/star/abcdef">
    <div class="photo-frame"><img src="/star/abc.jpg" title="葵つかさ"></div>
    <div class="photo-info"><span>葵つかさ</span></div>
</a>
<a class="avatar-box" href="/star/123456">
    <div class="photo-frame"><img src="https://www.javbus.com/star/xyz.jpg" title="蓮実クレア"></div>
    <div class="photo-info"><span>蓮実クレア</span></div>
</a>
</body></html>
"""

_FILM_LIST_HTML = """
<html><body>
<a class="movie-box" href="/SSIS-001">
    <div class="photo-frame"><img src="/cover/ssis001.jpg" title="Title 1"></div>
    <div class="photo-info">
        <span>Sample Title One</span>
        <date>SSIS-001</date>
        <date>2023-01-15</date>
    </div>
</a>
<a class="movie-box" href="/IPX-999">
    <div class="photo-frame"><img src="https://example.com/cover/ipx999.jpg"></div>
    <div class="photo-info">
        <span>Sample Title Two</span>
        <date>IPX-999</date>
        <date>2024-05-20</date>
    </div>
</a>
<a id="next" href="/star/abc/2">下一頁</a>
</body></html>
"""


def test_parse_actor_search():
    from app.discover import _parse_actor_search
    actors = _parse_actor_search(_ACTOR_SEARCH_HTML, "https://www.javbus.com")
    assert len(actors) == 2
    assert actors[0]["id"] == "abcdef"
    assert actors[0]["name"] == "葵つかさ"
    assert actors[0]["photo_url"] == "https://www.javbus.com/star/abc.jpg"
    assert actors[1]["id"] == "123456"
    assert actors[1]["name"] == "蓮実クレア"


def test_parse_film_list():
    from app.discover import _parse_film_list
    films, has_next = _parse_film_list(_FILM_LIST_HTML, "https://www.javbus.com")
    assert has_next is True
    assert len(films) == 2
    assert films[0] == {
        "code": "SSIS-001",
        "title": "Sample Title One",
        "release_date": "2023-01-15",
        "cover_url": "https://www.javbus.com/cover/ssis001.jpg",
        "detail_url": "https://www.javbus.com/SSIS-001",
    }
    assert films[1]["code"] == "IPX-999"
    assert films[1]["cover_url"] == "https://example.com/cover/ipx999.jpg"


def test_parse_film_list_no_next():
    from app.discover import _parse_film_list
    html_no_next = _FILM_LIST_HTML.replace('<a id="next" href="/star/abc/2">下一頁</a>', "")
    _, has_next = _parse_film_list(html_no_next, "https://www.javbus.com")
    assert has_next is False


def test_annotate_owned_with_explicit_set():
    from app.discover import annotate_owned
    films = [{"code": "SSIS-001"}, {"code": "ipx-999"}, {"code": "ABCD-9999"}]
    owned_norm = {"SSIS001", "IPX999"}
    out = annotate_owned(films, owned_norm)
    assert out[0]["owned"] is True
    assert out[1]["owned"] is True   # case insensitive via normalisation
    assert out[2]["owned"] is False


# ---------------------------------------------------------------------------
# Phase 2c — series / studio / genre URL parsing
# ---------------------------------------------------------------------------

def test_parse_javbus_url_series():
    from app.discover import parse_javbus_url
    assert parse_javbus_url("https://www.javbus.com/series/RPC") == ("series", "RPC")


def test_parse_javbus_url_studio():
    from app.discover import parse_javbus_url
    assert parse_javbus_url("/studio/5XS") == ("studio", "5XS")


def test_parse_javbus_url_genre():
    from app.discover import parse_javbus_url
    assert parse_javbus_url("https://www.javbus.com/genre/d4") == ("genre", "d4")


def test_parse_javbus_url_director():
    from app.discover import parse_javbus_url
    assert parse_javbus_url("/director/abc123") == ("director", "abc123")


def test_parse_javbus_url_with_page_suffix():
    from app.discover import parse_javbus_url
    # URL with /page suffix should still extract correctly
    assert parse_javbus_url("https://www.javbus.com/series/RPC/2") == ("series", "RPC")


def test_parse_javbus_url_invalid():
    from app.discover import parse_javbus_url
    assert parse_javbus_url("") is None
    assert parse_javbus_url("https://example.com/foo") is None
    assert parse_javbus_url("/unknown_kind/123") is None


def test_films_by_kind_invalid_kind():
    from app.discover import films_by_kind
    import asyncio
    import pytest
    # Not exercising the actual fetch — just the kind validator
    try:
        asyncio.run(films_by_kind("nonsense", "abc"))
        assert False, "should have raised ValueError"
    except ValueError as e:
        assert "unknown kind" in str(e).lower()
