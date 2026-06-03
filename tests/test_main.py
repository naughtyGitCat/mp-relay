"""Tests for app/main.py /submit overrides → MP add_download threading.

Focus: when a caller passes optional ``tmdbid`` / ``doubanid`` / ``media_type``
form fields on /submit, those values must end up in MoviePilot's
``/api/v1/download/add`` request body (for tmdbid / doubanid) and on the
mp-relay task record (for traceability of media_type).

The full FastAPI middleware stack is bypassed — we call the dispatch path
directly (``_handle_regular_magnet``) and mock ``MpClient.request`` to
capture the outbound HTTP body. This matches the existing test style in
``test_cloud115.py`` / ``test_bangumi.py`` (no FastAPI TestClient).
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def _isolated_db(monkeypatch) -> str:
    """Point settings.state_db at a fresh tmpfile so tests don't touch each other."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    from app.config import settings
    monkeypatch.setattr(settings, "state_db", tmp.name)
    from app import store
    store.init()
    return tmp.name


class _FakeMpResponse:
    """Minimal httpx.Response stand-in for MpClient.request mocks."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _patch_mp_request(captured: dict) -> AsyncMock:
    """Build an AsyncMock for MpClient.request that captures (method, path, **kw)."""
    async def fake_request(self, method, path, **kw):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kw.get("json")
        return _FakeMpResponse({"success": True, "message": None, "data": {"download_id": "deadbeef" * 5}})
    return fake_request


# ---------------------------------------------------------------------------
# _handle_regular_magnet → MP body threading
# ---------------------------------------------------------------------------

def test_handle_regular_magnet_threads_tmdbid_into_mp_body(monkeypatch):
    """Regression for the 2026-06 "无法识别媒体信息" escape valve: when /submit
    is called with ``tmdbid=348346``, MP's add_download body must include
    ``tmdbid: 348346`` at the top level, bypassing MP's title-based recognition.
    """
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.mp_client.MpClient.request", _patch_mp_request(captured))

    from app.main import _handle_regular_magnet
    magnet = "magnet:?xt=urn:btih:B18B0E61DD2707E9B2488A7510BABBE562DC10E8&dn=Lethal+Seduction"
    asyncio.run(_handle_regular_magnet(
        magnet, "magnet", {"name": "Lethal Seduction (2015)"},
        tmdbid=348346,
    ))

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/download/add"
    body = captured["json"]
    assert body["tmdbid"] == 348346
    # torrent_in still present and well-formed
    assert body["torrent_in"]["enclosure"] == magnet
    assert body["torrent_in"]["title"] == "Lethal Seduction (2015)"
    # doubanid not in body when not provided
    assert "doubanid" not in body


def test_handle_regular_magnet_threads_doubanid_into_mp_body(monkeypatch):
    """Same as above for doubanid — Chinese-only media often only has a
    Douban ID, not TMDB."""
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.mp_client.MpClient.request", _patch_mp_request(captured))

    from app.main import _handle_regular_magnet
    asyncio.run(_handle_regular_magnet(
        "magnet:?xt=urn:btih:" + "a" * 40, "magnet", {"name": "test"},
        doubanid="36688563",
    ))

    body = captured["json"]
    assert body["doubanid"] == "36688563"
    assert "tmdbid" not in body


def test_handle_regular_magnet_without_overrides_keeps_backward_compat(monkeypatch):
    """Backward-compat: caller that doesn't pass overrides gets the old MP body
    shape (only ``torrent_in`` key). Any extra keys would risk MP behaviour
    changes for clients that haven't migrated."""
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.mp_client.MpClient.request", _patch_mp_request(captured))

    from app.main import _handle_regular_magnet
    asyncio.run(_handle_regular_magnet(
        "magnet:?xt=urn:btih:" + "b" * 40, "magnet", {"name": "x"},
    ))

    body = captured["json"]
    assert set(body.keys()) == {"torrent_in"}, f"unexpected MP body keys: {body.keys()}"


def test_handle_regular_magnet_records_overrides_on_task_for_audit(monkeypatch):
    """Overrides must be recorded on the task's mp_response under ``_overrides``
    so a later audit can answer "did this download succeed because the caller
    forced an identity?" — useful when debugging MP misrouting."""
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.mp_client.MpClient.request", _patch_mp_request(captured))

    from app.main import _handle_regular_magnet
    from app import store
    resp = asyncio.run(_handle_regular_magnet(
        "magnet:?xt=urn:btih:" + "c" * 40, "magnet", {"name": "Test"},
        tmdbid=12345, doubanid=None, media_type="电影",
    ))

    # JSONResponse body contains task_id; pull the task back and inspect mp_response
    import json as _json
    payload = _json.loads(resp.body)
    task_id = payload["task_id"]
    row = store.get(task_id)
    assert row is not None
    mp_resp = row["mp_response"]
    if isinstance(mp_resp, str):  # store may return JSON string
        mp_resp = _json.loads(mp_resp)
    assert "_overrides" in mp_resp, f"no _overrides in stored mp_response: {mp_resp}"
    assert mp_resp["_overrides"]["tmdbid"] == 12345
    assert mp_resp["_overrides"]["doubanid"] is None
    assert mp_resp["_overrides"]["media_type"] == "电影"


def test_handle_regular_magnet_without_overrides_omits_overrides_key(monkeypatch):
    """When the caller didn't pass any override, the task's mp_response should
    NOT have an ``_overrides`` key — keeps task records lean for the common
    case where MP recognition just worked."""
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.mp_client.MpClient.request", _patch_mp_request(captured))

    from app.main import _handle_regular_magnet
    from app import store
    resp = asyncio.run(_handle_regular_magnet(
        "magnet:?xt=urn:btih:" + "d" * 40, "magnet", {"name": "Plain"},
    ))

    import json as _json
    task_id = _json.loads(resp.body)["task_id"]
    row = store.get(task_id)
    mp_resp = row["mp_response"]
    if isinstance(mp_resp, str):
        mp_resp = _json.loads(mp_resp)
    assert "_overrides" not in mp_resp
