"""Tests for the Phase 1.9 sync path — cloud115 download/list helpers + the
cloud115_watcher tick logic. The actual httpx CDN download is mocked since
we don't have valid 115 tokens in CI; we exercise the sequencing instead.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def _isolated_db(monkeypatch) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    from app.config import settings
    monkeypatch.setattr(settings, "state_db", tmp.name)
    from app import cloud115, store
    cloud115.init_token_table()
    store.init()
    store.init_retry_state()
    return tmp.name


# ---------------------------------------------------------------------------
# add_offline_url honors save_dir_id
# ---------------------------------------------------------------------------

def test_add_offline_url_uses_explicit_save_dir(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")
    captured: dict = {}

    async def fake_add(self, payload, **kw):
        captured["payload"] = payload
        return {"state": True, "data": []}

    monkeypatch.setattr("app.cloud115.P115OpenClient.offline_add_urls_open", fake_add)
    asyncio.run(cloud115.add_offline_url("magnet:?xt=urn:btih:abc", save_dir_id="explicit-cid"))
    assert captured["payload"]["wp_path_id"] == "explicit-cid"


def test_add_offline_url_falls_back_to_settings(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    from app.config import settings
    monkeypatch.setattr(settings, "cloud115_save_dir_id", "from-settings-cid")
    cloud115.save_tokens("at", "rt")
    captured: dict = {}

    async def fake_add(self, payload, **kw):
        captured["payload"] = payload
        return {"state": True, "data": []}

    monkeypatch.setattr("app.cloud115.P115OpenClient.offline_add_urls_open", fake_add)
    asyncio.run(cloud115.add_offline_url("magnet:?xt=urn:btih:abc"))
    assert captured["payload"]["wp_path_id"] == "from-settings-cid"


def test_add_offline_url_no_dir_omits_param(monkeypatch):
    """No explicit and no settings → don't send wp_path_id (115 default)."""
    _isolated_db(monkeypatch)
    from app import cloud115
    from app.config import settings
    monkeypatch.setattr(settings, "cloud115_save_dir_id", "")
    cloud115.save_tokens("at", "rt")
    captured: dict = {}

    async def fake_add(self, payload, **kw):
        captured["payload"] = payload
        return {"state": True, "data": []}

    monkeypatch.setattr("app.cloud115.P115OpenClient.offline_add_urls_open", fake_add)
    asyncio.run(cloud115.add_offline_url("magnet:?xt=urn:btih:abc"))
    assert "wp_path_id" not in captured["payload"]


# ---------------------------------------------------------------------------
# get_download_info parses correctly
# ---------------------------------------------------------------------------

def test_get_download_info_extracts_url_and_size(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    async def fake_info(self, payload, **kw):
        return {
            "state": True,
            "data": {
                "3418715654733859317": {
                    "file_name": "SNOS-073.mkv",
                    "file_size": 20867877919,
                    "sha1": "98E6...",
                    "url": {"url": "https://cdnfhnfile.115cdn.net/abc/file.mkv?t=123"},
                }
            },
        }

    monkeypatch.setattr("app.cloud115.P115OpenClient.download_url_info_open", fake_info)
    info = asyncio.run(cloud115.get_download_info("dhm2ya2zqlimdxl5h"))
    assert info["file_name"] == "SNOS-073.mkv"
    assert info["file_size"] == 20867877919
    assert info["url"].startswith("https://cdnfhnfile.115cdn.net/")


def test_get_download_info_handles_string_url(monkeypatch):
    """Some endpoints return url as plain string, not nested dict."""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    async def fake_info(self, payload, **kw):
        return {
            "state": True,
            "data": {
                "abc": {
                    "file_name": "x.mp4",
                    "file_size": 100,
                    "url": "https://direct.url/x.mp4",
                }
            },
        }

    monkeypatch.setattr("app.cloud115.P115OpenClient.download_url_info_open", fake_info)
    info = asyncio.run(cloud115.get_download_info("pc"))
    assert info["url"] == "https://direct.url/x.mp4"


def test_get_download_info_raises_on_failure(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    async def fake_info(self, payload, **kw):
        return {"state": False, "message": "提取码不能为空"}

    monkeypatch.setattr("app.cloud115.P115OpenClient.download_url_info_open", fake_info)
    try:
        asyncio.run(cloud115.get_download_info(""))
        assert False, "should raise"
    except RuntimeError as e:
        assert "失败" in str(e) or "failed" in str(e)


# ---------------------------------------------------------------------------
# stream_download integration (mocked httpx)
# ---------------------------------------------------------------------------

def test_stream_download_writes_file(monkeypatch, tmp_path):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    expected_bytes = b"x" * (3 * 1024 * 1024)   # 3 MiB

    async def fake_get_info(pickcode):
        return {
            "file_name": "test.bin",
            "file_size": len(expected_bytes),
            "sha1": "",
            "url": "https://fake/test.bin",
        }

    monkeypatch.setattr("app.cloud115.get_download_info", fake_get_info)

    class FakeResp:
        def raise_for_status(self): pass
        async def aiter_bytes(self, chunk_size):
            # Yield in 1 MiB chunks
            for i in range(0, len(expected_bytes), chunk_size):
                yield expected_bytes[i:i + chunk_size]
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def stream(self, method, url):
            return FakeResp()

    dest = tmp_path / "test.bin"
    with patch("app.cloud115.httpx.AsyncClient", FakeClient):
        n = asyncio.run(cloud115.stream_download("pc", dest))
    assert n == len(expected_bytes)
    assert dest.read_bytes() == expected_bytes


def test_stream_download_size_mismatch_deletes_file(monkeypatch, tmp_path):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    async def fake_get_info(pickcode):
        return {"file_name": "x", "file_size": 9999, "url": "https://fake/x"}

    monkeypatch.setattr("app.cloud115.get_download_info", fake_get_info)

    class FakeResp:
        def raise_for_status(self): pass
        async def aiter_bytes(self, chunk_size):
            yield b"only-100-bytes" * 10   # 140 bytes, not 9999
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def stream(self, *a, **kw): return FakeResp()

    dest = tmp_path / "x"
    with patch("app.cloud115.httpx.AsyncClient", FakeClient):
        try:
            asyncio.run(cloud115.stream_download("pc", dest))
            assert False, "should have raised"
        except RuntimeError as e:
            assert "size mismatch" in str(e)
    assert not dest.exists()        # cleanup ran


# ---------------------------------------------------------------------------
# sync_completed_task — folder vs singleton
# ---------------------------------------------------------------------------

def test_sync_completed_task_folder_skips_junk(monkeypatch, tmp_path):
    """When the resulting file_id is a folder, list children + download each
    non-junk file, skipping .url/.txt etc. that cleanup would delete anyway."""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    async def fake_list(folder_id):
        return [
            {"fid": "1", "fc": "1", "fn": "main.mkv", "pc": "pc-mkv"},
            {"fid": "2", "fc": "1", "fn": "promo.url", "pc": "pc-url"},        # junk
            {"fid": "3", "fc": "1", "fn": "readme.txt", "pc": "pc-txt"},        # junk
            {"fid": "4", "fc": "0", "fn": "subfolder", "pc": ""},               # folder, skipped
            {"fid": "5", "fc": "1", "fn": "extras.mp4", "pc": "pc-extras"},    # kept
        ]

    downloaded: list[str] = []

    async def fake_stream(pc, dest):
        downloaded.append(dest.name)
        dest.write_bytes(b"x")
        return 1

    monkeypatch.setattr("app.cloud115.list_folder_contents", fake_list)
    monkeypatch.setattr("app.cloud115.stream_download", fake_stream)

    task = {"name": "MOVIE-001", "info_hash": "abc", "file_id": "999", "pick_code": "pc-folder"}
    out = asyncio.run(cloud115.sync_completed_task(task, tmp_path))
    assert out == tmp_path / "MOVIE-001"
    assert sorted(downloaded) == ["extras.mp4", "main.mkv"]   # junk skipped, subfolder skipped


def test_sync_completed_task_singleton_file(monkeypatch, tmp_path):
    """If file_id is a single file (list returns empty), download by pick_code."""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    async def fake_list(folder_id):
        return []   # not a folder

    downloaded: list[Path] = []

    async def fake_stream(pc, dest):
        downloaded.append(dest)
        dest.write_bytes(b"x")
        return 1

    monkeypatch.setattr("app.cloud115.list_folder_contents", fake_list)
    monkeypatch.setattr("app.cloud115.stream_download", fake_stream)

    task = {"name": "single.mkv", "info_hash": "abc", "file_id": "f", "pick_code": "pc-single"}
    out = asyncio.run(cloud115.sync_completed_task(task, tmp_path))
    assert out == tmp_path / "single.mkv"
    assert downloaded == [tmp_path / "single.mkv" / "single.mkv"]


# ---------------------------------------------------------------------------
# cloud115_watcher tick logic
# ---------------------------------------------------------------------------

def test_watcher_skips_when_no_pending_tasks(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115_watcher, cloud115
    cloud115.save_tokens("at", "rt")

    # No tasks in submitted_to_115 state → watcher should not call list_offline.
    called = {"list": 0}

    async def fake_list(*a, **kw):
        called["list"] += 1
        return {}

    monkeypatch.setattr(
        "app.cloud115.list_offline_completed_by_hashes", fake_list,
    )
    asyncio.run(cloud115_watcher._scan_and_sync_once())
    assert called["list"] == 0


def test_watcher_skips_when_unauthorized(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115_watcher, store
    # Insert a pending task — but tokens are empty, so watcher should bail before scanning
    store.add(kind="cloud_offline_115", input_text="m", state="submitted_to_115",
              hash="abc", title="x")
    called = {"list": 0}

    async def fake_list(*a, **kw):
        called["list"] += 1
        return {}

    monkeypatch.setattr("app.cloud115.list_offline_completed_by_hashes", fake_list)
    asyncio.run(cloud115_watcher._scan_and_sync_once())
    assert called["list"] == 0


def test_watcher_marks_failed_115_task(monkeypatch):
    """115 status=-1 → mark mp-relay task cloud_failed, don't try to sync."""
    _isolated_db(monkeypatch)
    from app import cloud115_watcher, cloud115, store
    cloud115.save_tokens("at", "rt")
    tid = store.add(kind="cloud_offline_115", input_text="m", state="submitted_to_115",
                    hash="aaa", title="X")

    async def fake_scan(hashes, **kw):
        return {"aaa": {"status": -1, "name": "X", "info_hash": "aaa"}}

    monkeypatch.setattr("app.cloud115.list_offline_completed_by_hashes", fake_scan)
    # Should not need sync_completed_task for failed-on-115 case.

    asyncio.run(cloud115_watcher._scan_and_sync_once())
    after = store.get(tid)
    assert after["state"] == "cloud_failed"
    assert "status=-1" in (after["error"] or "")


def test_watcher_dispatches_sync_on_done(monkeypatch, tmp_path):
    """115 status=2 → sync to local + invoke post_download.run_pipeline."""
    _isolated_db(monkeypatch)
    from app import cloud115_watcher, cloud115, store
    from app.config import settings
    monkeypatch.setattr(settings, "cloud115_local_staging_dir", str(tmp_path))
    cloud115.save_tokens("at", "rt")
    tid = store.add(kind="cloud_offline_115", input_text="m", state="submitted_to_115",
                    hash="bbb", title="MovieB")

    async def fake_scan(hashes, **kw):
        return {"bbb": {"status": 2, "name": "MovieB", "info_hash": "bbb",
                        "file_id": "f", "pick_code": "p", "size": 1024}}

    sync_calls: list = []
    pipeline_calls: list = []

    async def fake_sync(task, dest_root):
        sync_calls.append((task["info_hash"], dest_root))
        return Path(dest_root) / task["name"]

    async def fake_pipeline(target, tid_arg, **kw):
        pipeline_calls.append((target, tid_arg, kw))

    monkeypatch.setattr("app.cloud115.list_offline_completed_by_hashes", fake_scan)
    monkeypatch.setattr("app.cloud115.sync_completed_task", fake_sync)
    monkeypatch.setattr("app.post_download.run_pipeline", fake_pipeline)

    asyncio.run(cloud115_watcher._scan_and_sync_once())

    assert sync_calls == [("bbb", tmp_path)]
    assert len(pipeline_calls) == 1
    target, tid_arg, kw = pipeline_calls[0]
    assert tid_arg == tid
    assert kw["retry_handler"] is None        # cloud-sourced files don't retry


def test_watcher_handles_sync_failure(monkeypatch, tmp_path):
    _isolated_db(monkeypatch)
    from app import cloud115_watcher, cloud115, store
    from app.config import settings
    monkeypatch.setattr(settings, "cloud115_local_staging_dir", str(tmp_path))
    cloud115.save_tokens("at", "rt")
    tid = store.add(kind="cloud_offline_115", input_text="m", state="submitted_to_115",
                    hash="ccc", title="MovieC")

    async def fake_scan(hashes, **kw):
        return {"ccc": {"status": 2, "name": "MovieC", "info_hash": "ccc",
                        "file_id": "f", "pick_code": "p", "size": 1024}}

    async def fake_sync(task, dest_root):
        raise RuntimeError("CDN 503")

    monkeypatch.setattr("app.cloud115.list_offline_completed_by_hashes", fake_scan)
    monkeypatch.setattr("app.cloud115.sync_completed_task", fake_sync)

    asyncio.run(cloud115_watcher._scan_and_sync_once())
    after = store.get(tid)
    assert after["state"] == "cloud_sync_failed"
    assert "CDN 503" in (after["error"] or "")
