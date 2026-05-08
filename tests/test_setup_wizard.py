"""Tests for setup_wizard — env file patching, log buffer accounting,
install state machine, connectivity probes. The validate_path / detect /
start_install paths shell out to PowerShell + a real mdcx tree; those
are exercised by the live smoke tests in CI rather than unit tests.
"""
from __future__ import annotations

import asyncio
import sys
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# write_env_keys — line-by-line .env editor
# ---------------------------------------------------------------------------

def test_write_env_keys_replaces_existing(tmp_path, monkeypatch):
    from app import setup_wizard
    env = tmp_path / ".env"
    env.write_text("MP_URL=http://old\nMDCX_DIR=C:\\old\nQBT_USER=admin\n", encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "_env_path", lambda: env)

    setup_wizard.write_env_keys({"MDCX_DIR": "E:\\new"})
    text = env.read_text(encoding="utf-8")
    assert "MDCX_DIR=E:\\new" in text
    assert "MDCX_DIR=C:\\old" not in text
    # Other keys untouched
    assert "MP_URL=http://old" in text
    assert "QBT_USER=admin" in text


def test_write_env_keys_appends_missing(tmp_path, monkeypatch):
    from app import setup_wizard
    env = tmp_path / ".env"
    env.write_text("MP_URL=http://x\n", encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "_env_path", lambda: env)

    setup_wizard.write_env_keys({"MDCX_DIR": "E:\\foo", "MDCX_PYTHON": "E:\\foo\\.venv\\python.exe"})
    text = env.read_text(encoding="utf-8")
    assert "MDCX_DIR=E:\\foo" in text
    assert "MDCX_PYTHON=E:\\foo\\.venv\\python.exe" in text


def test_write_env_keys_preserves_crlf(tmp_path, monkeypatch):
    """If .env was edited on Windows (CRLF), don't normalize to LF."""
    from app import setup_wizard
    env = tmp_path / ".env"
    env.write_bytes(b"MP_URL=x\r\nQBT_USER=admin\r\n")
    monkeypatch.setattr(setup_wizard, "_env_path", lambda: env)

    setup_wizard.write_env_keys({"MDCX_DIR": "E:\\foo"})
    raw = env.read_bytes()
    assert raw.count(b"\r\n") >= 3  # all three lines retain CRLF
    assert b"\r\nMDCX_DIR=E:\\foo\r\n" in raw


def test_write_env_keys_doesnt_touch_commented(tmp_path, monkeypatch):
    """Commented-out variants of the same key are left alone — we only
    edit uncommented assignments."""
    from app import setup_wizard
    env = tmp_path / ".env"
    env.write_text("# MDCX_DIR=C:\\example_for_docs\nMDCX_DIR=C:\\real\n", encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "_env_path", lambda: env)

    setup_wizard.write_env_keys({"MDCX_DIR": "E:\\new"})
    text = env.read_text(encoding="utf-8")
    assert "# MDCX_DIR=C:\\example_for_docs" in text   # comment preserved
    assert "MDCX_DIR=E:\\new" in text                  # real value updated
    assert "C:\\real" not in text                      # old uncommented gone


def test_write_env_bootstraps_from_example(tmp_path, monkeypatch):
    """If .env doesn't exist but .env.example does, copy and patch."""
    from app import setup_wizard
    env = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text("MP_URL=http://example\nQBT_PASS=change-me\n", encoding="utf-8")
    monkeypatch.setattr(setup_wizard, "_env_path", lambda: env)

    setup_wizard.write_env_keys({"MDCX_DIR": "E:\\foo"})
    assert env.is_file()
    text = env.read_text(encoding="utf-8")
    assert "MP_URL=http://example" in text
    assert "MDCX_DIR=E:\\foo" in text


# ---------------------------------------------------------------------------
# install_status — log buffer accounting (the dropped-cursor path is the
# trickiest part; deque truncation makes naive math wrong)
# ---------------------------------------------------------------------------

def test_install_status_fresh_state():
    from app import setup_wizard
    setup_wizard._install = setup_wizard.InstallState()
    s = setup_wizard.install_status(since=0)
    assert s["running"] is False
    assert s["total_lines"] == 0
    assert s["lines"] == []
    assert s["dropped"] == 0


def test_install_status_full_buffer():
    """Buffer hasn't dropped anything yet → lines are returned cleanly."""
    from app import setup_wizard
    s = setup_wizard.InstallState()
    for i in range(50):
        s.log_lines.append(f"line-{i}")
        s.total_lines += 1
    setup_wizard._install = s

    out = setup_wizard.install_status(since=0)
    assert len(out["lines"]) == 50
    assert out["next_since"] == 50
    assert out["dropped"] == 0

    # Resume from cursor
    out = setup_wizard.install_status(since=30)
    assert len(out["lines"]) == 20
    assert out["lines"][0] == "line-30"
    assert out["dropped"] == 0


def test_install_status_buffer_truncation_dropped(monkeypatch):
    """Long install, slow client: deque dropped some lines; we report
    `dropped` so the UI can show "N earlier lines truncated"."""
    from app import setup_wizard
    monkeypatch.setattr(setup_wizard, "_LOG_BUFFER_MAX", 100)
    s = setup_wizard.InstallState(log_lines=deque(maxlen=100))
    for i in range(500):
        s.log_lines.append(f"line-{i}")
        s.total_lines += 1
    setup_wizard._install = s

    # Cursor at 50; buffer's first line is 400 (since 500 total, deque cap 100)
    out = setup_wizard.install_status(since=50)
    assert out["dropped"] == 350
    assert len(out["lines"]) == 100
    assert out["lines"][0] == "line-400"
    assert out["next_since"] == 500


def test_install_status_cursor_past_total():
    """Client cursor is somehow ahead of us (shouldn't happen, but if a
    client misuses next_since we return empty rather than negative)."""
    from app import setup_wizard
    s = setup_wizard.InstallState()
    s.log_lines.append("only-line")
    s.total_lines = 1
    setup_wizard._install = s

    out = setup_wizard.install_status(since=10)
    assert out["lines"] == []
    assert out["dropped"] == 0


# ---------------------------------------------------------------------------
# probe_moviepilot / probe_qbt / probe_jellyfin — service connectivity tests
# httpx.AsyncClient is patched so we don't hit the network. The point of
# these tests is to verify our adapter logic (status code interpretation,
# 401 vs Fails. vs 200-but-empty handling) rather than HTTP itself.
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code: int, text: str = "", json_body: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._json = json_body or {}
    def json(self): return self._json


def _fake_client(post_resp=None, get_resp=None):
    """Build a context-manager-shaped fake AsyncClient that returns the
    given canned response from .post() / .get()."""
    client = AsyncMock()
    if post_resp is not None:
        client.post = AsyncMock(return_value=post_resp)
    if get_resp is not None:
        client.get = AsyncMock(return_value=get_resp)
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = False
    return cm


# --- MoviePilot ---

def test_probe_mp_empty_url():
    from app import setup_wizard
    r = asyncio.run(setup_wizard.probe_moviepilot("", "u", "p"))
    assert r == {"ok": False, "error": "URL is required"}


def test_probe_mp_success():
    from app import setup_wizard
    resp = _FakeResp(200, json_body={"access_token": "abc.def.ghi"})
    with patch.object(setup_wizard.httpx, "AsyncClient", return_value=_fake_client(post_resp=resp)):
        r = asyncio.run(setup_wizard.probe_moviepilot("http://x:3000", "admin", "good"))
    assert r["ok"] is True


def test_probe_mp_bad_creds():
    from app import setup_wizard
    resp = _FakeResp(401, text="bad")
    with patch.object(setup_wizard.httpx, "AsyncClient", return_value=_fake_client(post_resp=resp)):
        r = asyncio.run(setup_wizard.probe_moviepilot("http://x:3000", "admin", "wrong"))
    assert r["ok"] is False
    assert "credentials rejected" in r["error"]


def test_probe_mp_200_but_no_token():
    """Some misconfigured reverse proxies return 200 with the wrong body —
    we shouldn't treat that as success."""
    from app import setup_wizard
    resp = _FakeResp(200, json_body={"some": "other"})
    with patch.object(setup_wizard.httpx, "AsyncClient", return_value=_fake_client(post_resp=resp)):
        r = asyncio.run(setup_wizard.probe_moviepilot("http://x:3000", "admin", "p"))
    assert r["ok"] is False


# --- qBittorrent ---

def test_probe_qbt_success():
    from app import setup_wizard
    resp = _FakeResp(200, text="Ok.")
    with patch.object(setup_wizard.httpx, "AsyncClient", return_value=_fake_client(post_resp=resp)):
        r = asyncio.run(setup_wizard.probe_qbt("http://x:8080", "admin", "good"))
    assert r["ok"] is True


def test_probe_qbt_fails_body():
    """qBT returns HTTP 200 with body 'Fails.' on bad creds — legacy quirk
    we must inspect the body for, not just the status code."""
    from app import setup_wizard
    resp = _FakeResp(200, text="Fails.")
    with patch.object(setup_wizard.httpx, "AsyncClient", return_value=_fake_client(post_resp=resp)):
        r = asyncio.run(setup_wizard.probe_qbt("http://x:8080", "admin", "wrong"))
    assert r["ok"] is False
    assert "credentials rejected" in r["error"]


def test_probe_qbt_403_rate_limited():
    from app import setup_wizard
    resp = _FakeResp(403, text="")
    with patch.object(setup_wizard.httpx, "AsyncClient", return_value=_fake_client(post_resp=resp)):
        r = asyncio.run(setup_wizard.probe_qbt("http://x:8080", "admin", "wrong"))
    assert r["ok"] is False
    assert "403" in r["error"] or "too many" in r["error"]


# --- Jellyfin ---

def test_probe_jf_requires_api_key():
    from app import setup_wizard
    r = asyncio.run(setup_wizard.probe_jellyfin("http://x:8096", ""))
    assert r["ok"] is False
    assert "API key" in r["error"]


def test_probe_jf_success_returns_version():
    from app import setup_wizard
    resp = _FakeResp(200, json_body={"ServerName": "homelab", "Version": "10.10.7"})
    with patch.object(setup_wizard.httpx, "AsyncClient", return_value=_fake_client(get_resp=resp)):
        r = asyncio.run(setup_wizard.probe_jellyfin("http://x:8096", "abc123"))
    assert r["ok"] is True
    assert r["server_name"] == "homelab"
    assert r["version"] == "10.10.7"


def test_probe_jf_bad_api_key():
    from app import setup_wizard
    resp = _FakeResp(401, text="unauthorized")
    with patch.object(setup_wizard.httpx, "AsyncClient", return_value=_fake_client(get_resp=resp)):
        r = asyncio.run(setup_wizard.probe_jellyfin("http://x:8096", "wrong"))
    assert r["ok"] is False
    assert "API key rejected" in r["error"]
