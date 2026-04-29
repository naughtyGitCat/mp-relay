"""Telegram notifications for terminal pipeline events.

Why Telegram: per user preference (avoid mainland-China channels — bias toward
Telegram/Signal/Slack/Discord/Matrix). Telegram bots are simple HTTP and don't
require any inbound network access.

The watcher (and submission paths) call ``await notify(kind, text)`` on
interesting events. If telegram credentials aren't set, ``notify()`` is a no-op
so dev/test setups don't need a token.

Event kinds emitted by mp-relay:
  - ``scraped``                    — pipeline succeeded end-to-end
  - ``scrape_failed``              — mdcx returned non-zero
  - ``qc_failed_exhausted``        — 3× retry chain exhausted, manual review needed
  - ``qc_failed_no_alt``           — QC failed and no alternate candidates exist
  - ``qc_failed_no_code``          — QC failed but no JAV code parseable for retry
  - ``pre_mdcx_failed``            — disc remux or another pre-mdcx step blew up
  - ``submit_error``               — /submit raised an unexpected exception

Override which events are sent via ``TELEGRAM_EVENT_FILTER`` in .env (CSV).
Empty filter (default) = all events.

Concurrency note: each ``notify()`` opens a fresh httpx.AsyncClient. We don't
bother with connection pooling — Telegram volume is < 100/day even for a busy
homelab. Failed sends log a warning and swallow the error; the pipeline must
not stall waiting on a notification ack.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger(__name__)


_TELEGRAM_API: str = "https://api.telegram.org"


def _enabled() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _event_passes_filter(kind: str) -> bool:
    if not settings.telegram_event_filter.strip():
        return True
    allowed = {
        x.strip() for x in settings.telegram_event_filter.split(",") if x.strip()
    }
    return kind in allowed


def _format_message(kind: str, text: str, **fields: str) -> str:
    """Compose the Telegram message body. HTML parse mode for nicer rendering."""
    lines: list[str] = [f"<b>[mp-relay] {kind}</b>"]
    if text:
        lines.append(text)
    if fields:
        lines.append("")
        for k, v in fields.items():
            if v:
                # truncate to keep messages readable on phone
                lines.append(f"<i>{k}</i>: <code>{str(v)[:200]}</code>")
    return "\n".join(lines)


async def notify(kind: str, text: str = "", **fields: str) -> bool:
    """Send a Telegram message. Returns True on success, False on disabled/fail.

    Never raises — pipeline robustness > notification reliability.
    """
    if not _enabled():
        log.debug("notify(%s): disabled (no token/chat_id)", kind)
        return False
    if not _event_passes_filter(kind):
        log.debug("notify(%s): filtered out", kind)
        return False

    body = _format_message(kind, text, **fields)
    url = f"{_TELEGRAM_API}/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": body,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, json=payload)
            if r.status_code != 200:
                log.warning(
                    "telegram %s failed: HTTP %s body=%s",
                    kind, r.status_code, r.text[:200],
                )
                return False
            return True
    except (httpx.HTTPError, OSError) as e:
        log.warning("telegram %s send raised: %s", kind, e)
        return False


async def healthcheck() -> Optional[str]:
    """Return None if Telegram is reachable + token is valid, else error string.

    Used by /health endpoint to surface notification-channel issues.
    """
    if not _enabled():
        return "disabled (no token / chat_id)"
    url = f"{_TELEGRAM_API}/bot{settings.telegram_bot_token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return f"getMe HTTP {r.status_code}: {r.text[:120]}"
            data = r.json() or {}
            if not data.get("ok"):
                return f"getMe returned ok=false: {data}"
            return None
    except (httpx.HTTPError, OSError) as e:
        return f"getMe error: {e}"
