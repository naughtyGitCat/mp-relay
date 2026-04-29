"""Tests for notify.py — message formatting + filter logic + disabled fallback.

The actual httpx call to Telegram is patched out; we don't want CI hitting the
real API.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_format_message_basic():
    from app.notify import _format_message
    msg = _format_message("scraped", "all good", task="abc123", name="SSIS-001")
    assert "<b>[mp-relay] scraped</b>" in msg
    assert "all good" in msg
    assert "<i>task</i>: <code>abc123</code>" in msg
    assert "<i>name</i>: <code>SSIS-001</code>" in msg


def test_format_message_truncates_long_field():
    from app.notify import _format_message
    long_str = "x" * 500
    msg = _format_message("scrape_failed", "fail", stderr=long_str)
    # Field gets truncated to 200 chars
    assert "x" * 200 in msg
    assert "x" * 250 not in msg


def test_format_message_skips_empty_fields():
    from app.notify import _format_message
    msg = _format_message("scraped", "", task="abc", empty_field="")
    assert "<i>task</i>" in msg
    assert "empty_field" not in msg


def test_event_filter_empty_passes_all(monkeypatch):
    from app import notify, config
    monkeypatch.setattr(config.settings, "telegram_event_filter", "")
    assert notify._event_passes_filter("anything")
    assert notify._event_passes_filter("scraped")


def test_event_filter_explicit(monkeypatch):
    from app import notify, config
    monkeypatch.setattr(
        config.settings,
        "telegram_event_filter",
        "qc_failed_exhausted, scrape_failed",
    )
    assert notify._event_passes_filter("qc_failed_exhausted")
    assert notify._event_passes_filter("scrape_failed")
    assert not notify._event_passes_filter("scraped")


def test_notify_disabled_when_no_credentials(monkeypatch):
    """notify() should be a no-op (returning False) without token+chat_id."""
    from app import notify, config
    monkeypatch.setattr(config.settings, "telegram_bot_token", "")
    monkeypatch.setattr(config.settings, "telegram_chat_id", "")

    result = asyncio.run(notify.notify("scraped", "test"))
    assert result is False


def test_notify_calls_telegram_when_enabled(monkeypatch):
    """When token+chat_id set, notify() should POST to the Telegram API."""
    from app import notify, config

    monkeypatch.setattr(config.settings, "telegram_bot_token", "fake_token_123")
    monkeypatch.setattr(config.settings, "telegram_chat_id", "987654321")
    monkeypatch.setattr(config.settings, "telegram_event_filter", "")

    posted_payloads: list[dict] = []

    class FakeResp:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json=None):
            posted_payloads.append({"url": url, "json": json})
            return FakeResp()

    with patch("app.notify.httpx.AsyncClient", FakeClient):
        result = asyncio.run(notify.notify("scraped", "all good", task="abc"))

    assert result is True
    assert len(posted_payloads) == 1
    payload = posted_payloads[0]
    assert "fake_token_123" in payload["url"]
    assert payload["json"]["chat_id"] == "987654321"
    assert "scraped" in payload["json"]["text"]
    assert "abc" in payload["json"]["text"]


def test_notify_swallows_http_errors(monkeypatch):
    """notify() must NEVER raise — pipeline robustness > notification reliability."""
    import httpx
    from app import notify, config

    monkeypatch.setattr(config.settings, "telegram_bot_token", "x")
    monkeypatch.setattr(config.settings, "telegram_chat_id", "y")

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            raise httpx.ConnectError("network down")

    with patch("app.notify.httpx.AsyncClient", FakeClient):
        result = asyncio.run(notify.notify("scraped", "test"))

    assert result is False  # graceful failure, no exception


def test_notify_filtered_event_returns_false(monkeypatch):
    """Filter active and event not in list → don't send."""
    from app import notify, config

    monkeypatch.setattr(config.settings, "telegram_bot_token", "x")
    monkeypatch.setattr(config.settings, "telegram_chat_id", "y")
    monkeypatch.setattr(config.settings, "telegram_event_filter", "qc_failed_exhausted")

    result = asyncio.run(notify.notify("scraped", "should be filtered"))
    assert result is False
