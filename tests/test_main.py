"""Tests for app/main.py magnet-submit path.

Two concerns:

1. **Orphan prevention** (2026-06 Warehouse 13 incident): the task record must
   be created BEFORE the MoviePilot call, in a ``submitting_to_mp`` state, and
   the MP call must run in a task detached from the request lifecycle — so a
   client disconnect mid-MP-call can't lose the record (torrent ends up in qBT
   but no mp-relay task).

2. **Identity overrides**: optional ``tmdbid`` / ``doubanid`` / ``media_type``
   passed to /submit must thread into MP's ``/api/v1/download/add`` body
   (tmdbid / doubanid) and onto the task's ``mp_response._overrides`` (all
   three, for audit).

The full FastAPI middleware stack is bypassed — we call the dispatch path
functions directly and mock ``MpClient.request`` to capture the outbound HTTP
body. Matches the existing test style in ``test_cloud115.py`` (no TestClient).
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

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

    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


def _patch_mp_request(captured: dict, payload: dict | None = None):
    """Build a fake MpClient.request that captures (method, path, json body)."""
    resp = payload or {"success": True, "message": None, "data": {"download_id": "d" * 40}}

    async def fake_request(self, method, path, **kw):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kw.get("json")
        return _FakeMpResponse(resp)
    return fake_request


def _read_mp_response(row: dict) -> dict:
    mp_resp = row["mp_response"]
    return json.loads(mp_resp) if isinstance(mp_resp, str) else mp_resp


# ---------------------------------------------------------------------------
# Orphan prevention: _handle_regular_magnet records the task before the MP call
# ---------------------------------------------------------------------------

def test_handle_regular_magnet_records_pending_task_before_mp_call(monkeypatch):
    """The crux of the orphan fix: when _handle_regular_magnet returns, the
    task already exists in ``submitting_to_mp`` state and a background task has
    been scheduled — WITHOUT the MP call having run synchronously. We mock
    _spawn_bg to a no-op (closing the coroutine) so the MP call never fires,
    proving the task is recorded independent of the MP round-trip."""
    _isolated_db(monkeypatch)
    spawned: list[str] = []

    def fake_spawn(coro, *, name):
        spawned.append(name)
        coro.close()  # don't actually run it; avoids "never awaited" warning

    monkeypatch.setattr("app.main._spawn_bg", fake_spawn)

    from app.main import _handle_regular_magnet
    from app import store
    resp = asyncio.run(_handle_regular_magnet(
        "magnet:?xt=urn:btih:" + "a" * 40, "magnet", {"name": "Warehouse 13 S01"},
    ))

    body = json.loads(resp.body)
    assert body["kind"] == "magnet"
    assert body["state"] == "submitting_to_mp"
    assert "mp_response" not in body  # MP call hasn't happened yet

    tid = body["task_id"]
    row = store.get(tid)
    assert row is not None
    assert row["state"] == "submitting_to_mp"          # recorded pre-MP
    assert row["title"] == "Warehouse 13 S01"
    assert len(spawned) == 1                             # bg task scheduled
    assert spawned[0].startswith("mp-add-")


def test_handle_regular_magnet_stamps_overrides_on_pending_record(monkeypatch):
    """_overrides must be stamped at task creation so the audit trail survives
    even if the bg task dies (service restart) before completing."""
    _isolated_db(monkeypatch)
    monkeypatch.setattr("app.main._spawn_bg", lambda coro, *, name: coro.close())

    from app.main import _handle_regular_magnet
    from app import store
    resp = asyncio.run(_handle_regular_magnet(
        "magnet:?xt=urn:btih:" + "b" * 40, "magnet", {"name": "Y"},
        tmdbid=12345, media_type="电影",
    ))

    tid = json.loads(resp.body)["task_id"]
    mp_resp = _read_mp_response(store.get(tid))
    assert mp_resp["_overrides"]["tmdbid"] == 12345
    assert mp_resp["_overrides"]["media_type"] == "电影"
    assert mp_resp["_overrides"]["doubanid"] is None


def test_handle_regular_magnet_no_overrides_pending_record_has_no_mp_response(monkeypatch):
    """Lean common case: no overrides → pending record carries no mp_response
    blob (it gets populated by the bg task on completion)."""
    _isolated_db(monkeypatch)
    monkeypatch.setattr("app.main._spawn_bg", lambda coro, *, name: coro.close())

    from app.main import _handle_regular_magnet
    from app import store
    resp = asyncio.run(_handle_regular_magnet(
        "magnet:?xt=urn:btih:" + "c" * 40, "magnet", {"name": "Plain"},
    ))
    tid = json.loads(resp.body)["task_id"]
    assert store.get(tid)["mp_response"] is None


# ---------------------------------------------------------------------------
# _submit_magnet_to_mp: the detached worker — MP body threading + state update
# ---------------------------------------------------------------------------

def test_submit_magnet_to_mp_threads_tmdbid_into_body(monkeypatch):
    """Override threading: tmdbid must land in MP's request body, bypassing
    MP's title-based recognition. Task transitions to submitted_to_mp."""
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.mp_client.MpClient.request", _patch_mp_request(captured))

    from app.main import _submit_magnet_to_mp
    from app import store
    tid = store.add(kind="magnet", input_text="m", state="submitting_to_mp", title="L")
    magnet = "magnet:?xt=urn:btih:B18B0E61DD2707E9B2488A7510BABBE562DC10E8"
    asyncio.run(_submit_magnet_to_mp(
        tid, name="Lethal Seduction (2015)", enclosure=magnet,
        tmdbid=348346, doubanid=None,
        overrides={"tmdbid": 348346, "doubanid": None, "media_type": None},
    ))

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/download/add"
    body = captured["json"]
    assert body["tmdbid"] == 348346
    assert body["torrent_in"]["enclosure"] == magnet
    assert "doubanid" not in body

    row = store.get(tid)
    assert row["state"] == "submitted_to_mp"
    assert _read_mp_response(row)["_overrides"]["tmdbid"] == 348346


def test_submit_magnet_to_mp_threads_doubanid_into_body(monkeypatch):
    """doubanid is the fallback identity for Chinese-only media with no TMDB."""
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.mp_client.MpClient.request", _patch_mp_request(captured))

    from app.main import _submit_magnet_to_mp
    from app import store
    tid = store.add(kind="magnet", input_text="m", state="submitting_to_mp", title="x")
    asyncio.run(_submit_magnet_to_mp(
        tid, name="x", enclosure="magnet:?xt=urn:btih:" + "d" * 40,
        tmdbid=None, doubanid="36688563",
        overrides={"tmdbid": None, "doubanid": "36688563", "media_type": None},
    ))

    body = captured["json"]
    assert body["doubanid"] == "36688563"
    assert "tmdbid" not in body


def test_submit_magnet_to_mp_without_overrides_body_unchanged(monkeypatch):
    """Backward-compat: no overrides → MP body is just ``{torrent_in}`` and the
    task's mp_response carries no ``_overrides`` key."""
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr("app.mp_client.MpClient.request", _patch_mp_request(captured))

    from app.main import _submit_magnet_to_mp
    from app import store
    tid = store.add(kind="magnet", input_text="m", state="submitting_to_mp", title="x")
    asyncio.run(_submit_magnet_to_mp(
        tid, name="x", enclosure="magnet:?xt=urn:btih:" + "e" * 40,
        tmdbid=None, doubanid=None, overrides=None,
    ))

    assert set(captured["json"].keys()) == {"torrent_in"}
    assert "_overrides" not in _read_mp_response(store.get(tid))


def test_submit_magnet_to_mp_marks_rejected_on_mp_failure(monkeypatch):
    """MP returns success=false ("无法识别媒体信息" with no override) → task
    goes mp_rejected, not submitted_to_mp."""
    _isolated_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        "app.mp_client.MpClient.request",
        _patch_mp_request(captured, payload={"success": False, "message": "无法识别媒体信息", "data": {}}),
    )

    from app.main import _submit_magnet_to_mp
    from app import store
    tid = store.add(kind="magnet", input_text="m", state="submitting_to_mp", title="x")
    asyncio.run(_submit_magnet_to_mp(
        tid, name="x", enclosure="magnet:?xt=urn:btih:" + "f" * 40,
        tmdbid=None, doubanid=None, overrides=None,
    ))
    assert store.get(tid)["state"] == "mp_rejected"


def test_submit_magnet_to_mp_marks_rejected_on_exception(monkeypatch):
    """MP call raising (network blip) → task goes mp_rejected with the error
    captured, rather than the worker crashing silently."""
    _isolated_db(monkeypatch)

    async def boom(self, method, path, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("connection refused")

    monkeypatch.setattr("app.mp_client.MpClient.request", boom)

    from app.main import _submit_magnet_to_mp
    from app import store
    tid = store.add(kind="magnet", input_text="m", state="submitting_to_mp", title="x")
    asyncio.run(_submit_magnet_to_mp(
        tid, name="x", enclosure="magnet:?xt=urn:btih:" + "a" * 40,
        tmdbid=None, doubanid=None, overrides=None,
    ))
    row = store.get(tid)
    assert row["state"] == "mp_rejected"
    assert "connection refused" in (row["error"] or "")
