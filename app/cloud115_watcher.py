"""Phase 1.9 — background worker that completes the 115 round-trip.

Lifecycle of a cloud-115 task in mp-relay:

  submitted_to_115     ← created by /api/cloud115-add (Phase 1.8)
       ↓ (this watcher detects 115 status=2)
  syncing_to_local     ← we open httpx stream from 115 CDN to local disk
       ↓ (download finishes)
  processing           ← post_download.run_pipeline takes over
       ↓
  scraping → scraped (or scrape_failed / qc_failed_*)

The watcher is the only place that polls 115's offline list. Frequency =
``settings.cloud115_poll_interval_sec`` (default 60s). On each tick:

  1. find mp-relay tasks in state=submitted_to_115
  2. scan 115 offline pages (capped at ``cloud115_scan_max_pages``) until all
     tracked hashes are located OR pages exhausted
  3. for each match with status=2, sync to local + dispatch post_download
  4. mark cloud_sync_failed for any sync that raises

NOT done in v1:
  - Resumable downloads (httpx Range requests). On error we delete the
    partial file and the next tick retries from scratch.
  - Concurrent syncs. Tasks are processed one at a time so we don't blast
    115's CDN. If one task takes 30 minutes to download, others wait.
  - Auto-retry on cloud_sync_failed. User can manually re-trigger by
    clearing the error state via the UI / CLI (TODO).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from . import cloud115, metrics as m, notify, post_download, store
from .config import settings

log = logging.getLogger(__name__)


# 115 offline-task status codes (from SheltonZhu/115driver, observed live):
#   0  = 待开始 (queued)
#   1  = 下载中 (running)
#   2  = 完成 (done)
#  -1  = 失败 (failed)
_STATUS_DONE: int = 2
_STATUS_FAILED: int = -1


# States that indicate "this task hasn't been synced yet — keep watching".
# Also list states we treat as terminal-on-this-side so we don't keep
# rescanning forever.
_PENDING_SYNC_STATES: list[str] = ["submitted_to_115"]


async def _scan_and_sync_once() -> None:
    """One tick of the loop."""
    if not cloud115.is_authorized():
        return

    pending = store.list_in_states(_PENDING_SYNC_STATES, kind="cloud_offline_115", limit=100)
    if not pending:
        return

    pending_by_hash: dict[str, dict] = {}
    for row in pending:
        h = (row.get("hash") or "").lower()
        if h:
            pending_by_hash[h] = row
    if not pending_by_hash:
        return

    log.info("[c115 watch] tracking %d pending sync(s)", len(pending_by_hash))

    try:
        c115_tasks = await cloud115.list_offline_completed_by_hashes(
            set(pending_by_hash.keys()),
            max_pages=settings.cloud115_scan_max_pages,
        )
    except Exception as e:
        log.warning("[c115 watch] scan failed: %s", e)
        return

    for h, mp_row in pending_by_hash.items():
        c115 = c115_tasks.get(h)
        if c115 is None:
            # Could be deeper than max_pages, or removed from 115. Don't
            # transition the task; next tick may pick it up if 115's queue
            # progresses.
            continue
        status = int(c115.get("status", 0))
        if status == _STATUS_FAILED:
            store.update(
                mp_row["id"],
                state="cloud_failed",
                error=f"115 marked task failed (status=-1): {(c115.get('name') or '')[:80]}",
            )
            m.PIPELINE_RUN_TOTAL.labels(terminal_state="cloud_failed").inc()
            log.warning("[c115 watch] task=%s failed on 115 side", mp_row["id"])
            continue
        if status != _STATUS_DONE:
            continue   # still 0 (queued) or 1 (running)
        await _sync_one(mp_row, c115)


async def _sync_one(mp_row: dict, c115_task: dict) -> None:
    """Sync a single completed 115 task to local + dispatch post-download."""
    tid = mp_row["id"]
    name = c115_task.get("name") or mp_row.get("title") or "(unnamed)"
    log.info("[c115 watch] sync starting tid=%s name=%s size=%.2fGiB",
             tid, name[:60], (c115_task.get("size", 0) or 0) / (1024**3))
    store.update(tid, state="syncing_to_local", error=None)

    dest_root = Path(settings.cloud115_local_staging_dir)
    try:
        async with m.timed_step("cloud_sync"):
            local_dir = await cloud115.sync_completed_task(c115_task, dest_root)
    except Exception as e:
        log.exception("[c115 watch] sync failed tid=%s: %s", tid, e)
        store.update(tid, state="cloud_sync_failed", error=str(e)[:300])
        m.PIPELINE_STEP_TOTAL.labels(step="cloud_sync", outcome="fail").inc()
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="cloud_sync_failed").inc()
        await notify.notify(
            "cloud_sync_failed",
            "115 → 本地同步失败",
            task=tid, name=name[:80], error=str(e)[:200],
        )
        return

    log.info("[c115 watch] sync done tid=%s → %s", tid, local_dir)
    m.PIPELINE_STEP_TOTAL.labels(step="cloud_sync", outcome="ok").inc()
    store.update(tid, state="processing", save_path=str(local_dir))

    # Hand off to the same post-download pipeline qBT uses. No retry handler
    # — once a file's been downloaded from cloud, there's no fallback magnet
    # to swap to (the user already chose 115 over qBT).
    try:
        await post_download.run_pipeline(
            str(local_dir), tid,
            name=name,
            failed_hash=(mp_row.get("hash") or ""),
            retry_handler=None,
        )
    except Exception as e:
        log.exception("[c115 watch] post-download pipeline failed tid=%s: %s", tid, e)
        store.update(tid, state="scrape_failed", error=str(e)[:300])
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="scrape_failed").inc()


async def cloud115_watch_loop(stop: asyncio.Event) -> None:
    """Top-level loop — runs until ``stop`` is set."""
    interval = settings.cloud115_poll_interval_sec
    log.info("cloud115 watcher started (interval=%ss)", interval)

    while not stop.is_set():
        try:
            await _scan_and_sync_once()
        except Exception as e:
            log.exception("cloud115 watch tick crashed: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    log.info("cloud115 watcher stopped")
