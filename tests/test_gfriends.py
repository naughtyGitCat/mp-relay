"""Tests for gfriends.py — index building + name normalization + URL resolution.

httpx network calls are mocked; we don't hit GitHub from CI.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


_FAKE_FILETREE = {
    "Content": {
        "AA": {
            "葵つかさ.jpg": {"sha": "abc"},
            "蓮実クレア.jpg": {"sha": "def"},
        },
        "BB": {
            "Yua Mikami.jpg": {"sha": "xyz"},
            "深田えいみ.jpg": {"sha": "jkl"},
        },
    },
    "Information": {"updated": "2025-01-01"},
}


def test_normalize():
    from app.gfriends import _normalize
    # Whitespace stripped
    assert _normalize("葵 つかさ") == _normalize("葵つかさ")
    # Fullwidth space
    assert _normalize("葵　つかさ") == _normalize("葵つかさ")
    # Latin lowercase
    assert _normalize("Yua Mikami") == "yuamikami"
    # Empty
    assert _normalize("") == ""
    assert _normalize(None) == ""


def test_build_index():
    from app.gfriends import _build_index, _normalize
    idx = _build_index(_FAKE_FILETREE)
    assert _normalize("葵つかさ") in idx
    assert _normalize("Yua Mikami") in idx
    assert idx[_normalize("葵つかさ")] == "Content/AA/葵つかさ.jpg"
    assert idx[_normalize("Yua Mikami")] == "Content/BB/Yua Mikami.jpg"
    assert len(idx) == 4


def test_build_index_handles_malformed():
    from app.gfriends import _build_index
    # Missing Content key
    assert _build_index({}) == {}
    # Content not a dict
    assert _build_index({"Content": "not a dict"}) == {}
    # Bucket not a dict
    assert _build_index({"Content": {"AA": "not a dict"}}) == {}


def _reset_state():
    from app.gfriends import _filetree_state
    _filetree_state["fetched_at"] = 0.0
    _filetree_state["tree"] = None
    _filetree_state["index"] = None


def test_find_actor_avatar_url_hit():
    from app import gfriends

    _reset_state()

    class FakeResp:
        status_code = 200
        def json(self):
            return _FAKE_FILETREE

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url):
            return FakeResp()

    with patch("app.gfriends.httpx.AsyncClient", FakeClient):
        url = asyncio.run(gfriends.find_actor_avatar_url("葵つかさ"))
        assert url == "https://raw.githubusercontent.com/gfriends/gfriends/master/Content/AA/葵つかさ.jpg"


def test_find_actor_avatar_url_miss():
    from app import gfriends
    _reset_state()

    class FakeResp:
        status_code = 200
        def json(self): return _FAKE_FILETREE

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url): return FakeResp()

    with patch("app.gfriends.httpx.AsyncClient", FakeClient):
        url = asyncio.run(gfriends.find_actor_avatar_url("does not exist"))
        assert url is None


def test_find_actor_avatar_url_empty_input():
    from app.gfriends import find_actor_avatar_url
    assert asyncio.run(find_actor_avatar_url("")) is None
    assert asyncio.run(find_actor_avatar_url(None)) is None


def test_find_actor_avatar_url_handles_fetch_error():
    """Network error during filetree fetch should return None (not raise)."""
    import httpx
    from app import gfriends
    _reset_state()

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url):
            raise httpx.ConnectError("network down")

    with patch("app.gfriends.httpx.AsyncClient", FakeClient):
        url = asyncio.run(gfriends.find_actor_avatar_url("葵つかさ"))
        assert url is None


def test_save_avatar_skips_existing(tmp_path):
    """save_avatar must not overwrite an existing file unless overwrite=True."""
    from app.gfriends import save_avatar
    dest = tmp_path / "existing.jpg"
    dest.write_bytes(b"original")

    result = asyncio.run(save_avatar("葵つかさ", dest, overwrite=False))
    assert result is False
    assert dest.read_bytes() == b"original"  # unchanged
