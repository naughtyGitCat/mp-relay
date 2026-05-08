"""Tests for setup_wizard — env file patching, log buffer accounting,
install state machine. The validate_path / detect / start_install paths
shell out to PowerShell + a real mdcx tree; those are exercised by the
live smoke tests in CI rather than unit tests.
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

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
