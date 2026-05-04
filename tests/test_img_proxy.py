"""Tests for img_proxy.py — host whitelist, fetch, cache LRU.

httpx is mocked because (a) we don't want CI hitting real CDNs and (b) the
real failure mode (Cloudflare 403 without Referer) is the point of this
module — we exercise the *fix*, not the bug.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# is_allowed
# ---------------------------------------------------------------------------

def test_is_allowed_javbus():
    from app.img_proxy import is_allowed
    assert is_allowed("https://www.javbus.com/pics/thumb/c4lq.jpg")
    assert is_allowed("http://img.javbus.com/cover/abc.jpg")
    assert is_allowed("https://lain.bgm.tv/pic/cover/m/aa/bb/123.jpg")


def test_is_allowed_rejects_unknown_host():
    from app.img_proxy import is_allowed
    assert not is_allowed("https://evil.com/x.jpg")
    assert not is_allowed("https://googleusercontent.com/x.jpg")


def test_is_allowed_rejects_non_http():
    from app.img_proxy import is_allowed
    assert not is_allowed("file:///etc/passwd")
    assert not is_allowed("ftp://www.javbus.com/x.jpg")
    assert not is_allowed("data:image/png;base64,iVBOR...")


def test_is_allowed_rejects_garbage():
    from app.img_proxy import is_allowed
    assert not is_allowed("")
    assert not is_allowed("not-a-url-at-all")
    assert not is_allowed("///")


# ---------------------------------------------------------------------------
# fetch — happy + sad paths
# ---------------------------------------------------------------------------

def _setup_fake_httpx(monkeypatch, *, status: int = 200,
                       body: bytes = b"\xff\xd8jpeg-bytes",
                       content_type: str = "image/jpeg",
                       captured_headers: dict | None = None,
                       raise_exc: Exception | None = None):
    """Install a fake httpx.AsyncClient that returns a single canned response.
    Pass ``captured_headers`` to receive the headers we sent on the GET."""
    if captured_headers is None:
        captured_headers = {}

    class FakeResp:
        def __init__(self):
            self.status_code = status
            self.content = body
            self.headers = {"content-type": content_type}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, headers=None):
            captured_headers.update(headers or {})
            if raise_exc is not None:
                raise raise_exc
            return FakeResp()

    monkeypatch.setattr("app.img_proxy.httpx.AsyncClient", FakeClient)
    return captured_headers


def test_fetch_javbus_sends_correct_referer(monkeypatch):
    """The whole point of this module: we MUST set Referer:
    https://www.javbus.com/ for JavBus's CDN."""
    from app import img_proxy
    img_proxy.cache_clear()
    headers = _setup_fake_httpx(monkeypatch)
    out = asyncio.run(img_proxy.fetch("https://www.javbus.com/pics/thumb/x.jpg"))
    assert out is not None
    body, ct = out
    assert body == b"\xff\xd8jpeg-bytes"
    assert ct == "image/jpeg"
    assert headers.get("Referer") == "https://www.javbus.com/"
    assert "User-Agent" in headers
    img_proxy.cache_clear()


def test_fetch_bangumi_uses_bgm_referer(monkeypatch):
    from app import img_proxy
    img_proxy.cache_clear()
    headers = _setup_fake_httpx(monkeypatch)
    out = asyncio.run(img_proxy.fetch("https://lain.bgm.tv/pic/cover/m/x.jpg"))
    assert out is not None
    assert headers.get("Referer") == "https://bgm.tv/"
    img_proxy.cache_clear()


def test_fetch_rejects_disallowed_host(monkeypatch):
    from app import img_proxy
    img_proxy.cache_clear()
    # Don't even install a fake — we shouldn't reach httpx
    out = asyncio.run(img_proxy.fetch("https://evil.com/leak.jpg"))
    assert out is None


def test_fetch_returns_none_on_403(monkeypatch):
    from app import img_proxy
    img_proxy.cache_clear()
    _setup_fake_httpx(monkeypatch, status=403)
    out = asyncio.run(img_proxy.fetch("https://www.javbus.com/pics/x.jpg"))
    assert out is None
    img_proxy.cache_clear()


def test_fetch_returns_none_on_network_error(monkeypatch):
    import httpx
    from app import img_proxy
    img_proxy.cache_clear()
    _setup_fake_httpx(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    out = asyncio.run(img_proxy.fetch("https://www.javbus.com/pics/x.jpg"))
    assert out is None
    img_proxy.cache_clear()


def test_fetch_strips_charset_from_content_type(monkeypatch):
    """Some CDNs return ``image/jpeg; charset=binary`` — strip the charset
    so the browser doesn't get confused."""
    from app import img_proxy
    img_proxy.cache_clear()
    _setup_fake_httpx(monkeypatch, content_type="image/png; charset=binary")
    out = asyncio.run(img_proxy.fetch("https://www.javbus.com/pics/x.jpg"))
    assert out is not None
    _, ct = out
    assert ct == "image/png"
    img_proxy.cache_clear()


def test_fetch_returns_none_on_empty_body(monkeypatch):
    from app import img_proxy
    img_proxy.cache_clear()
    _setup_fake_httpx(monkeypatch, body=b"")
    out = asyncio.run(img_proxy.fetch("https://www.javbus.com/pics/x.jpg"))
    assert out is None
    img_proxy.cache_clear()


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------

def test_fetch_caches_result(monkeypatch):
    """Second fetch of same URL doesn't re-hit upstream."""
    from app import img_proxy
    img_proxy.cache_clear()

    call_count = {"n": 0}

    class FakeResp:
        status_code = 200
        content = b"img-bytes"
        headers = {"content-type": "image/jpeg"}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, headers=None):
            call_count["n"] += 1
            return FakeResp()

    with patch("app.img_proxy.httpx.AsyncClient", FakeClient):
        url = "https://www.javbus.com/pics/x.jpg"
        a = asyncio.run(img_proxy.fetch(url))
        b = asyncio.run(img_proxy.fetch(url))
    assert a == b == (b"img-bytes", "image/jpeg")
    assert call_count["n"] == 1
    img_proxy.cache_clear()


def test_cache_lru_eviction(monkeypatch):
    """When cache exceeds capacity, oldest entries get evicted."""
    from app import img_proxy
    img_proxy.cache_clear()
    # Shrink capacity for the test
    monkeypatch.setattr(img_proxy, "_CACHE_MAX", 3)

    bodies = {f"u{i}": f"body{i}".encode() for i in range(5)}

    class FakeResp:
        def __init__(self, body): self.status_code, self.content = 200, body
        headers = {"content-type": "image/jpeg"}

    current = {"body": b""}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, headers=None):
            return FakeResp(current["body"])

    with patch("app.img_proxy.httpx.AsyncClient", FakeClient):
        for i in range(5):
            current["body"] = bodies[f"u{i}"]
            asyncio.run(img_proxy.fetch(f"https://www.javbus.com/pics/u{i}.jpg"))

    stats = img_proxy.cache_stats()
    assert stats["size"] == 3   # capped
    img_proxy.cache_clear()


def test_cache_clear():
    from app import img_proxy
    # Manually warm the cache
    img_proxy._cache["x"] = (b"y", "image/jpeg")
    n = img_proxy.cache_clear()
    assert n == 1
    assert img_proxy.cache_stats()["size"] == 0
