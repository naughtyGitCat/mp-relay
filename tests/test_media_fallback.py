"""Tests for media_fallback.py — AniList query, response parsing, error handling."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


_ANILIST_RESPONSE = {
    "data": {
        "Page": {
            "media": [
                {
                    "id": 12345,
                    "type": "ANIME",
                    "format": "TV",
                    "seasonYear": 2023,
                    "title": {
                        "romaji": "Karasu wa Aruji wo Erabanai",
                        "english": "Crow's Choice",
                        "native": "鴉は主を選ばない",
                    },
                    "synonyms": ["乌鸦不择主", "漆黑的射干"],
                },
            ]
        }
    }
}


class _FakeResp:
    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = ""

    def json(self):
        return self._json


def _make_fake_client(resp: _FakeResp):
    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, json=None):
            return resp
    return FakeClient


def test_alternate_titles_returns_english_first():
    from app import media_fallback

    fake = _make_fake_client(_FakeResp(200, _ANILIST_RESPONSE))
    with patch("app.media_fallback.httpx.AsyncClient", fake):
        alts = asyncio.run(media_fallback.alternate_titles_anilist("漆黑的射干"))

    titles = [a["title"] for a in alts]
    # English title first (highest priority)
    assert titles[0] == "Crow's Choice"
    assert alts[0]["via"] == "anilist:english"
    # Then romaji
    assert "Karasu wa Aruji wo Erabanai" in titles
    # Then native
    assert "鴉は主を選ばない" in titles
    # Synonym (乌鸦不择主) is also present
    assert "乌鸦不择主" in titles


def test_alternate_titles_excludes_query_itself():
    """Don't suggest the user's own input back to them."""
    from app import media_fallback
    fake = _make_fake_client(_FakeResp(200, _ANILIST_RESPONSE))
    with patch("app.media_fallback.httpx.AsyncClient", fake):
        alts = asyncio.run(media_fallback.alternate_titles_anilist("漆黑的射干"))
    titles = [a["title"] for a in alts]
    assert "漆黑的射干" not in titles


def test_alternate_titles_empty_query():
    from app import media_fallback
    assert asyncio.run(media_fallback.alternate_titles_anilist("")) == []
    assert asyncio.run(media_fallback.alternate_titles_anilist("   ")) == []


def test_alternate_titles_handles_http_error():
    from app import media_fallback
    fake = _make_fake_client(_FakeResp(500, {"errors": [{"message": "boom"}]}))
    with patch("app.media_fallback.httpx.AsyncClient", fake):
        alts = asyncio.run(media_fallback.alternate_titles_anilist("anything"))
    assert alts == []


def test_alternate_titles_handles_network_error():
    """Connection error should return [], not raise."""
    import httpx
    from app import media_fallback

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw):
            raise httpx.ConnectError("network down")

    with patch("app.media_fallback.httpx.AsyncClient", FakeClient):
        alts = asyncio.run(media_fallback.alternate_titles_anilist("test"))
    assert alts == []


def test_alternate_titles_handles_invalid_json():
    from app import media_fallback

    class FakeResp:
        status_code = 200
        text = "not json"
        def json(self):
            raise ValueError("not json")

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw):
            return FakeResp()

    with patch("app.media_fallback.httpx.AsyncClient", FakeClient):
        alts = asyncio.run(media_fallback.alternate_titles_anilist("test"))
    assert alts == []


def test_alternate_titles_empty_response():
    from app import media_fallback
    fake = _make_fake_client(_FakeResp(200, {"data": {"Page": {"media": []}}}))
    with patch("app.media_fallback.httpx.AsyncClient", fake):
        alts = asyncio.run(media_fallback.alternate_titles_anilist("nonexistent"))
    assert alts == []


def test_alternate_titles_respects_limit():
    from app import media_fallback
    fake = _make_fake_client(_FakeResp(200, _ANILIST_RESPONSE))
    with patch("app.media_fallback.httpx.AsyncClient", fake):
        alts = asyncio.run(media_fallback.alternate_titles_anilist("漆黑的射干", limit=2))
    assert len(alts) == 2
