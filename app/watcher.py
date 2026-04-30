"""Watch qBT for completed JAV torrents and run the post-download pipeline.

The actual triage / merge / QC / mdcx / post-cleanup logic lives in
``app/post_download.py`` so the cloud-115 sync path (Phase 1.9) can drive the
same pipeline without duplicating it. This module is just:

  - poll qBT every ``settings.watcher_interval_sec`` for completed JAV
    torrents
  - per-torrent bookkeeping in mp-relay's tasks table
  - dispatch into ``post_download.run_pipeline``, supplying a qBT-flavored
    QC retry handler that can swap to the next sukebei/JavBus candidate
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from . import jav_search, metrics as m, notify, post_download, store
from .config import settings
from .qbt_client import QbtClient

log = logging.getLogger(__name__)

# Torrent states meaning "download is complete and seeding/idle".
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
        base = os.path.basename(cp)
        if "." in base and not cp.endswith(("\\", "/")):
            # heuristic: looks like a file, use parent
            return os.path.dirname(cp) or t.get("save_path") or cp
        return cp
    return t.get("save_path") or ""


# ---------------------------------------------------------------------------
# qBT-flavored QC retry chain (the only piece that's qBT-specific)
# ---------------------------------------------------------------------------

async def _retry_with_next_candidate(
    qbt: QbtClient,
    tid: str,
    code: str,
    failed_hash: str,
    qc_reason: str,
) -> bool:
    """On QC fail: try the next-best alternate from cached sukebei/JavBus search.

    Deletes the failed torrent's files (frees disk on a dud), adds the next
    candidate's magnet under the JAV category. Watcher catches the new
    completion via the same _process_done path.

    Returns True if a retry was queued, False if exhausted / no alt available.
    """
    tried, attempts = store.retry_get_tried(code)
    tried.add(failed_hash.lower())

    if attempts >= settings.qc_max_retries:
        store.retry_set_state(code, "qc_failed_exhausted", qc_reason)
        store.update(
            tid, state="qc_failed_exhausted",
            error=f"QC failed {attempts}× — no more candidates: {qc_reason}",
        )
        m.QC_RETRY.labels(outcome="exhausted").inc()
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="qc_failed_exhausted").inc()
        log.warning("[qc] task=%s code=%s exhausted retries: %s", tid, code, qc_reason)
        await notify.notify(
            "qc_failed_exhausted",
            f"已重试 {attempts} 次仍 QC 失败，需要人工介入",
            code=code, task=tid, reason=qc_reason,
        )
        return False

    candidates = await jav_search.search_jav_code(code)
    nxt = jav_search.best_candidate(candidates, exclude_hashes=tried)
    if nxt is None:
        store.retry_set_state(code, "qc_failed_no_alt", qc_reason)
        store.update(
            tid, state="qc_failed_no_alt",
            error=f"QC failed and no alternate candidate: {qc_reason}",
        )
        m.QC_RETRY.labels(outcome="no_alt").inc()
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="qc_failed_no_alt").inc()
        log.warning("[qc] task=%s code=%s no alt candidate: %s", tid, code, qc_reason)
        await notify.notify(
            "qc_failed_no_alt",
            "QC 失败但 sukebei / JavBus 没有其他候选可用",
            code=code, task=tid, reason=qc_reason,
        )
        return False

    # Record the new hash as tried before adding (so the watcher won't re-pick
    # it if QC fires again before we update state).
    store.retry_record_try(code, nxt["info_hash"])
    store.retry_set_state(code, "retry_queued", f"swap to {nxt['info_hash'][:8]}")

    # Delete the failed torrent's files so we don't burn disk on a dud.
    try:
        await qbt.delete(failed_hash, delete_files=True)
    except Exception as e:
        log.warning("retry: failed to delete bad torrent %s: %s", failed_hash[:8], e)

    # Queue the new magnet under the JAV category.
    await qbt.add_url(
        nxt["magnet"],
        category=settings.qbt_jav_category,
        save_path=settings.qbt_jav_savepath,
    )
    log.info(
        "[qc] task=%s code=%s retry queued: %s (suspicion=%d quality=%d seeders=%d)",
        tid, code, nxt["info_hash"][:8],
        nxt.get("suspicion_score", 0),
        nxt.get("quality_score", 0),
        nxt.get("seeders", 0),
    )

    # Track the new task so /tasks shows the retry chain.
    store.add(
        kind="jav_retry",
        input_text=f"retry of {code} (after {failed_hash[:8]} failed QC)",
        state="queued",
        hash=nxt["info_hash"],
        title=nxt["title"],
        save_path=settings.qbt_jav_savepath,
    )

    store.update(tid, state="qc_failed_retried",
                 error=f"QC failed: {qc_reason}; retried with {nxt['info_hash'][:8]}")
    m.QC_RETRY.labels(outcome="swapped").inc()
    m.PIPELINE_RUN_TOTAL.labels(terminal_state="qc_failed_retried").inc()
    return True


# ---------------------------------------------------------------------------
# Per-torrent handler
# ---------------------------------------------------------------------------

async def _process_done(qbt: QbtClient, t: dict) -> None:
    """A JAV-category torrent reached done state — run the post-download pipeline."""
    h: str = t["hash"]
    existing = store.find_by_hash(h)
    if existing and existing.get("state") in (
        "scraped", "scrape_failed", "scraping",
        "qc_failed_retried", "qc_failed_exhausted", "qc_failed_no_alt",
        "migrated_to_115",
    ):
        log.debug("hash=%s already processed (state=%s), skip", h[:8], existing["state"])
        return

    target = _torrent_dir(t)
    if not target:
        log.warning("hash=%s no path (save_path/content_path empty), skip", h[:8])
        return

    if existing:
        store.update(
            existing["id"],
            state="processing",
            save_path=target,
            title=t.get("name"),
        )
        tid = existing["id"]
    else:
        tid = store.add(
            kind="jav_external",
            input_text=t.get("name") or "(qbt-direct)",
            state="processing",
            hash=h,
            save_path=target,
            title=t.get("name"),
        )

    log.info("[pipeline] task=%s hash=%s name=%s -> %s",
             tid, h[:8], (t.get("name") or "")[:60], target)

    # Wait a beat in case qBT is still moving files post-completion.
    await asyncio.sleep(settings.mdcx_settle_sec)

    # Bind the qBT-aware retry handler so post_download.run_pipeline can call
    # it on QC failure.
    async def retry_handler(_tid: str, code: str, failed_hash: str, qc_reason: str) -> bool:
        return await _retry_with_next_candidate(qbt, _tid, code, failed_hash, qc_reason)

    await post_download.run_pipeline(
        target, tid,
        name=t.get("name", "") or "",
        failed_hash=h,
        retry_handler=retry_handler,
    )


# ---------------------------------------------------------------------------
# Watcher loop
# ---------------------------------------------------------------------------

async def watch_loop(stop: asyncio.Event) -> None:
    """Main watcher loop. Polls qBT every settings.watcher_interval_sec."""
    qbt = QbtClient()
    log.info("watcher started (interval=%ss, category=%s)",
             settings.watcher_interval_sec, settings.qbt_jav_category)

    seen_done: set[str] = set()  # in-memory dedupe; store.find_by_hash is the durable check

    while not stop.is_set():
        # Refresh the inflight gauge each tick so /metrics reflects current
        # task distribution by state.
        try:
            m.refresh_inflight_gauge(store.list_recent(limit=200))
        except Exception as e:
            log.debug("metrics refresh failed: %s", e)

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
