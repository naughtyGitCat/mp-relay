"""Tests for bangumi.py — search response parsing + name extraction."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# Real shape of `GET /search/subject/<kw>?responseGroup=small` — captured from
# live API call for "漆黑的射干" on 2026-04-29 (the canonical regression case).
_BGM_RESPONSE = {
    "results": 1,
    "list": [
        {
            "id": 210268,
            "url": "http://bgm.tv/subject/210268",
            "type": 2,
            "name": "漆黒のシャガ THE ANIMATION",
            "name_cn": "漆黑的射干",
            "summary": "",
            "air_date": "",
            "air_weekday": 0,
            "images": {"large": "http://lain.bgm.tv/.../210268_BlSB1.jpg"},
        }
    ]
}


_BGM_NOT_FOUND = {"results": 0, "code": 404, "error": "Not Found"}


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
        async def get(self, url, params=None):
            return resp
    return FakeClient


def test_search_subjects_parses_list():
    from app import bangumi
    fake = _make_fake_client(_FakeResp(200, _BGM_RESPONSE))
    with patch("app.bangumi.httpx.AsyncClient", fake):
        subjects = asyncio.run(bangumi.search_subjects("漆黑的射干"))
    assert len(subjects) == 1
    assert subjects[0]["id"] == 210268
    assert subjects[0]["name"] == "漆黒のシャガ THE ANIMATION"
    assert subjects[0]["name_cn"] == "漆黑的射干"


def test_search_subjects_empty_keyword():
    from app import bangumi
    assert asyncio.run(bangumi.search_subjects("")) == []
    assert asyncio.run(bangumi.search_subjects("   ")) == []


def test_search_subjects_handles_404():
    """Bangumi returns dict-shaped 'not found' (no list key)."""
    from app import bangumi
    fake = _make_fake_client(_FakeResp(200, _BGM_NOT_FOUND))
    with patch("app.bangumi.httpx.AsyncClient", fake):
        result = asyncio.run(bangumi.search_subjects("nonsense_query"))
    assert result == []


def test_search_subjects_handles_http_error():
    from app import bangumi
    fake = _make_fake_client(_FakeResp(500, {"error": "boom"}))
    with patch("app.bangumi.httpx.AsyncClient", fake):
        result = asyncio.run(bangumi.search_subjects("anything"))
    assert result == []


def test_search_subjects_handles_network_error():
    import httpx
    from app import bangumi

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, *a, **kw):
            raise httpx.ConnectError("network down")

    with patch("app.bangumi.httpx.AsyncClient", FakeClient):
        result = asyncio.run(bangumi.search_subjects("test"))
    assert result == []


def test_alternate_titles_extracts_both_names():
    """When user types Chinese, JP name should be the useful alt (and vice versa)."""
    from app import bangumi
    fake = _make_fake_client(_FakeResp(200, _BGM_RESPONSE))
    with patch("app.bangumi.httpx.AsyncClient", fake):
        alts = asyncio.run(bangumi.alternate_titles_bangumi("漆黑的射干"))
    titles = [a["title"] for a in alts]
    # Native JP is the alternate (user typed Chinese)
    assert "漆黒のシャガ THE ANIMATION" in titles
    # The JP entry has via=bangumi:name, bangumi_id=210268
    jp = next(a for a in alts if a["title"] == "漆黒のシャガ THE ANIMATION")
    assert jp["via"] == "bangumi:name"
    assert jp["bangumi_id"] == 210268


def test_alternate_titles_excludes_query_itself():
    """Don't suggest the user's own input back to them."""
    from app import bangumi
    fake = _make_fake_client(_FakeResp(200, _BGM_RESPONSE))
    with patch("app.bangumi.httpx.AsyncClient", fake):
        alts = asyncio.run(bangumi.alternate_titles_bangumi("漆黑的射干"))
    titles = [a["title"] for a in alts]
    assert "漆黑的射干" not in titles


def test_alternate_titles_empty_input():
    from app import bangumi
    assert asyncio.run(bangumi.alternate_titles_bangumi("")) == []


def test_alternate_titles_empty_response():
    from app import bangumi
    fake = _make_fake_client(_FakeResp(200, _BGM_NOT_FOUND))
    with patch("app.bangumi.httpx.AsyncClient", fake):
        result = asyncio.run(bangumi.alternate_titles_bangumi("nonexistent"))
    assert result == []
