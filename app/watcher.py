"""Watch qBT for completed JAV-category torrents and trigger mdcx scrape."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

from . import store
from .config import settings
from .mdcx_runner import scrape_dir
from .qbt_client import QbtClient

log = logging.getLogger(__name__)

# Torrent states that mean "download is complete and seeding/idle".
_DONE_STATES = frozenset({
    "uploading", "stalledUP", "pausedUP", "queuedUP",
    "checkingUP", "forcedUP",
})


def _is_done(t: dict) -> bool:
    return t.get("progress", 0) >= 0.999 and t.get("state") in _DONE_STATES


def _torrent_dir(t: dict) -> str:
    """Pick the directory mdcx should scrape.

    Prefer content_path for single-file torrents (it points to the file's parent).
    For multi-file torrents content_path is the torrent root dir; that's also fine.
    Fallback to save_path if content_path missing.
    """
    cp = t.get("content_path") or ""
    if cp:
        # If content_path is a file, mdcx wants its parent dir
        # We can't stat the remote path from here, but qBT's content_path for
        # multi-file torrents is the torrent root (a dir), and for single-file
        # torrents is the file path. mdcx scrape dir handles a single file too
        # by scanning the parent — but to be safe, hand over the parent if it
        # looks like a file (has an extension).
        import os
        base = os.path.basename(cp)
        if "." in base and not cp.endswith(("\\", "/")):
            # heuristic: looks like a file, use parent
            return os.path.dirname(cp) or t.get("save_path") or cp
        return cp
    return t.get("save_path") or ""


async def _process_done(qbt: QbtClient, t: dict) -> None:
    """A JAV-category torrent reached done state — kick off mdcx, recording state."""
    h = t["hash"]
    existing = store.find_by_hash(h)
    if existing and existing.get("state") in ("scraped", "scrape_failed", "scraping"):
        log.debug("hash=%s already processed (state=%s), skip", h[:8], existing["state"])
        return

    target = _torrent_dir(t)
    if not target:
        log.warning("hash=%s no path (save_path/content_path empty), skip", h[:8])
        return

    if existing:
        store.update(
            existing["id"],
            state="scraping",
            save_path=target,
            title=t.get("name"),
        )
        tid = existing["id"]
    else:
        # Torrent wasn't added through us (e.g. user added directly to qBT under
        # the JAV category) — record it now so we can track scrape result.
        tid = store.add(
            kind="jav_external",
            input_text=t.get("name") or "(qbt-direct)",
            state="scraping",
            hash=h,
            save_path=target,
            title=t.get("name"),
        )

    log.info("[scrape] task=%s hash=%s name=%s -> %s",
             tid, h[:8], (t.get("name") or "")[:60], target)

    # Wait a beat before mdcx, in case qBT is still moving files post-completion.
    await asyncio.sleep(settings.mdcx_settle_sec)

    result = await scrape_dir(target)
    if result["rc"] == 0:
        store.update(tid, state="scraped", mdcx_result=result)
        log.info("[scrape] task=%s OK", tid)
    else:
        store.update(tid, state="scrape_failed", mdcx_result=result,
                     error=(result.get("stderr") or "")[:1000])
        log.error("[scrape] task=%s FAILED rc=%s", tid, result["rc"])


async def watch_loop(stop: asyncio.Event) -> None:
    """Main watcher loop. Polls qBT every settings.watcher_interval_sec."""
    qbt = QbtClient()
    log.info("watcher started (interval=%ss, category=%s)",
             settings.watcher_interval_sec, settings.qbt_jav_category)

    seen_done: set[str] = set()  # in-memory dedupe; store.find_by_hash is the durable check

    while not stop.is_set():
        try:
            torrents = await qbt.list_torrents(category=settings.qbt_jav_category)
        except Exception as e:
            log.warning("watcher poll failed: %s", e)
            await _sleep_or_stop(stop, settings.watcher_interval_sec)
            continue

        for t in torrents:
            h = t.get("hash") or ""
            if not h or not _is_done(t):
                continue
            if h in seen_done:
                continue
            seen_done.add(h)
            try:
                await _process_done(qbt, t)
            except Exception as e:
                log.exception("watcher: processing %s failed: %s", h[:8], e)
                seen_done.discard(h)

        await _sleep_or_stop(stop, settings.watcher_interval_sec)

    await qbt.close()
    log.info("watcher stopped")


async def _sleep_or_stop(stop: asyncio.Event, sec: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=sec)
    except asyncio.TimeoutError:
        pass
