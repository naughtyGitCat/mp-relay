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


# ---------------------------------------------------------------------------
# Phase 1.6: orchestrator combining AniList + Bangumi
# ---------------------------------------------------------------------------

def test_alternate_titles_all_merges_sources():
    """Orchestrator queries both sources concurrently, merges results."""
    from app import media_fallback

    async def fake_anilist(q, **kw):
        return [{"title": "Karasu wa Aruji", "via": "anilist:romaji"}]

    async def fake_bangumi(q, **kw):
        return [{"title": "漆黒のシャガ THE ANIMATION", "via": "bangumi:name", "bangumi_id": 210268}]

    with patch("app.media_fallback.alternate_titles_anilist", fake_anilist), \
         patch("app.media_fallback.bangumi.alternate_titles_bangumi", fake_bangumi):
        merged = asyncio.run(media_fallback.alternate_titles_all("漆黑的射干"))
    titles = [m["title"] for m in merged]
    assert "Karasu wa Aruji" in titles
    assert "漆黒のシャガ THE ANIMATION" in titles


def test_alternate_titles_all_dedupes_across_sources():
    """If both sources return the same title, only one entry survives.
    AniList wins on tie because it's queried first in the orchestrator."""
    from app import media_fallback

    async def fake_anilist(q, **kw):
        return [{"title": "Same Title", "via": "anilist:english"}]

    async def fake_bangumi(q, **kw):
        return [{"title": "Same Title", "via": "bangumi:name"}]

    with patch("app.media_fallback.alternate_titles_anilist", fake_anilist), \
         patch("app.media_fallback.bangumi.alternate_titles_bangumi", fake_bangumi):
        merged = asyncio.run(media_fallback.alternate_titles_all("query"))
    assert len(merged) == 1
    assert merged[0]["via"] == "anilist:english"


def test_alternate_titles_all_excludes_query_echo():
    """Don't suggest the user's own input back to them, even if a source
    happens to return it."""
    from app import media_fallback

    async def fake_anilist(q, **kw):
        return [{"title": "MyQuery", "via": "anilist:english"}]    # echoes input

    async def fake_bangumi(q, **kw):
        return [{"title": "Real Alt", "via": "bangumi:name"}]

    with patch("app.media_fallback.alternate_titles_anilist", fake_anilist), \
         patch("app.media_fallback.bangumi.alternate_titles_bangumi", fake_bangumi):
        merged = asyncio.run(media_fallback.alternate_titles_all("myquery"))
    titles = [m["title"] for m in merged]
    assert "MyQuery" not in titles
    assert "Real Alt" in titles


def test_alternate_titles_all_one_source_failing_doesnt_kill_other():
    """asyncio.gather with return_exceptions=True isolates failures."""
    from app import media_fallback

    async def fake_anilist(q, **kw):
        raise RuntimeError("anilist down")

    async def fake_bangumi(q, **kw):
        return [{"title": "Survivor", "via": "bangumi:name"}]

    with patch("app.media_fallback.alternate_titles_anilist", fake_anilist), \
         patch("app.media_fallback.bangumi.alternate_titles_bangumi", fake_bangumi):
        merged = asyncio.run(media_fallback.alternate_titles_all("test"))
    assert [m["title"] for m in merged] == ["Survivor"]


def test_find_bangumi_match_returns_first_subject():
    from app import media_fallback

    async def fake_search(*a, **kw):
        return [{"id": 210268, "name": "漆黒のシャガ THE ANIMATION",
                 "name_cn": "漆黑的射干", "url": "http://bgm.tv/subject/210268",
                 "type": 2}]

    with patch("app.media_fallback.bangumi.search_subjects", fake_search):
        match = asyncio.run(media_fallback.find_bangumi_match("漆黑的射干"))
    assert match["id"] == 210268
    assert match["name"] == "漆黒のシャガ THE ANIMATION"
    assert match["name_cn"] == "漆黑的射干"
    assert "210268" in match["url"]


def test_find_bangumi_match_no_results():
    from app import media_fallback

    async def fake_search(*a, **kw):
        return []

    with patch("app.media_fallback.bangumi.search_subjects", fake_search):
        match = asyncio.run(media_fallback.find_bangumi_match("nothing"))
    assert match is None
