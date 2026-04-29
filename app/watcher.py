"""Watch qBT for completed JAV torrents and run the post-download pipeline.

Pipeline (per completed torrent):

  1. triage       — classify files: keep / junk / dupe / sample / extras / parts / disc
  2. execute      — delete junk + dupes + samples (extras are preserved)
  3. relocate     — move extras into Extras/ subfolder
  4. remux disc   — if BDMV / VIDEO_TS, ffmpeg-remux to a single .mkv
  5. merge parts  — if multipart, ffmpeg concat-copy into one file
                    (fall back to Jellyfin <name>-cd1.ext naming on codec mismatch)
  6. QC           — ffprobe duration + min-size sanity check on the main file
                    on FAIL → record tried hash, swap to next-best candidate from
                    the cached sukebei search, re-add to qBT, return early.
  7. mdcx         — scrape metadata
  8. post-cleanup — sweep leftover .url/.txt/sample after mdcx

Each step's outcome is written to the SQLite store so the /tasks UI can show
where a job got to (or where it stalled).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from . import cleanup, jav_search, merger, metrics as m, qc, store
from .config import settings
from .exists import extract_code
from .mdcx_runner import scrape_dir
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
# Pipeline steps (each one updates store with its outcome notes).
# ---------------------------------------------------------------------------


async def _run_pre_mdcx_pipeline(target: str, tid: str) -> tuple[bool, str]:
    """Steps 1-5: triage → execute → extras → disc remux → multipart merge.

    Returns (ok, note). On ok=False the caller should mark the task failed and
    not invoke mdcx; ``note`` is a human-readable reason.
    """
    notes: list[str] = []

    # 1. Triage
    async with m.timed_step("triage"):
        triage = await cleanup.triage_dir(target)
    notes.extend(triage.notes)
    m.PIPELINE_STEP_TOTAL.labels(step="triage", outcome="ok").inc()

    # 2. Delete junk + dupes + samples
    async with m.timed_step("execute"):
        del_logs = cleanup.execute(triage)
    notes.extend(del_logs)
    m.FILES_DELETED.labels(category="junk").inc(len(triage.delete_junk))
    m.FILES_DELETED.labels(category="dupe").inc(len(triage.delete_dupes))
    m.FILES_DELETED.labels(category="sample").inc(len(triage.delete_samples))
    m.PIPELINE_STEP_TOTAL.labels(step="execute", outcome="ok").inc()

    # 3. Move extras into Extras/
    async with m.timed_step("relocate_extras"):
        extra_logs = cleanup.relocate_extras(triage, target)
    notes.extend(extra_logs)
    if triage.extras:
        m.EXTRAS_RELOCATED.inc(len(triage.extras))
    m.PIPELINE_STEP_TOTAL.labels(
        step="relocate_extras",
        outcome="ok" if triage.extras else "skip",
    ).inc()

    # 4. Disc archive remux (BDMV / VIDEO_TS → single .mkv)
    if settings.remux_disc_archives and triage.disc_archive is not None:
        async with m.timed_step("remux_disc"):
            rr = await merger.remux_disc(triage.disc_archive)
        notes.append(rr.note)
        # Detect kind from note (cheap; the note string is shaped "remuxed bdmv → ...").
        kind = "bdmv" if "bdmv" in rr.note.lower() else "dvd"
        if rr.output_path is None:
            m.DISC_REMUX.labels(kind=kind, outcome="fail").inc()
            m.PIPELINE_STEP_TOTAL.labels(step="remux_disc", outcome="fail").inc()
            store.update(tid, error=f"disc remux failed: {rr.note}")
            return False, rr.note
        m.DISC_REMUX.labels(kind=kind, outcome="success").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="remux_disc", outcome="ok").inc()
    else:
        m.PIPELINE_STEP_TOTAL.labels(step="remux_disc", outcome="skip").inc()

    # 5. Multipart merge (CD1+CD2+... → one file)
    if settings.merge_multipart and len(triage.multipart_parts) >= 2:
        async with m.timed_step("merge_parts"):
            mr = await merger.merge_parts(triage.multipart_parts)
        notes.append(mr.note)
        if mr.merged_path is None:
            # Fall back to Jellyfin-friendly naming so the parts aren't lost.
            rename_logs = merger.rename_parts_jellyfin(triage.multipart_parts)
            notes.extend(rename_logs)
            m.MULTIPART_MERGED.labels(outcome="fallback_rename").inc()
            m.PIPELINE_STEP_TOTAL.labels(step="merge_parts", outcome="fail").inc()
        else:
            m.MULTIPART_MERGED.labels(outcome="concat_copy").inc()
            m.PIPELINE_STEP_TOTAL.labels(step="merge_parts", outcome="ok").inc()
    else:
        m.PIPELINE_STEP_TOTAL.labels(step="merge_parts", outcome="skip").inc()

    store.update(tid, mdcx_result={"pre_mdcx_notes": notes[:30]})
    return True, "; ".join(notes[-3:])


async def _retry_with_next_candidate(
    qbt: QbtClient,
    tid: str,
    code: str,
    failed_hash: str,
    qc_reason: str,
) -> bool:
    """On QC fail, try the next-best alternate from the cached sukebei search.

    Returns True if a retry was queued, False if we've exhausted options.
    """
    tried, attempts = store.retry_get_tried(code)
    tried.add(failed_hash.lower())

    if attempts >= settings.qc_max_retries:
        store.retry_set_state(code, "qc_failed_exhausted", qc_reason)
        store.update(
            tid,
            state="qc_failed_exhausted",
            error=f"QC failed {attempts}× — no more candidates: {qc_reason}",
        )
        m.QC_RETRY.labels(outcome="exhausted").inc()
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="qc_failed_exhausted").inc()
        log.warning("[qc] task=%s code=%s exhausted retries: %s", tid, code, qc_reason)
        return False

    candidates = await jav_search.search_jav_code(code)
    nxt = jav_search.best_candidate(candidates, exclude_hashes=tried)
    if nxt is None:
        store.retry_set_state(code, "qc_failed_no_alt", qc_reason)
        store.update(
            tid,
            state="qc_failed_no_alt",
            error=f"QC failed and no alternate candidate: {qc_reason}",
        )
        m.QC_RETRY.labels(outcome="no_alt").inc()
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="qc_failed_no_alt").inc()
        log.warning("[qc] task=%s code=%s no alt candidate: %s", tid, code, qc_reason)
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

    # Queue the new magnet under the JAV category — the watcher will catch it
    # when it completes via the same _process_done path.
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
# Top-level per-torrent handler
# ---------------------------------------------------------------------------


async def _process_done(qbt: QbtClient, t: dict) -> None:
    """A JAV-category torrent reached done state — run the full pipeline."""
    h: str = t["hash"]
    existing = store.find_by_hash(h)
    if existing and existing.get("state") in (
        "scraped", "scrape_failed", "scraping",
        "qc_failed_retried", "qc_failed_exhausted", "qc_failed_no_alt",
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

    # Steps 1-5: pre-mdcx triage + merge + remux
    ok, note = await _run_pre_mdcx_pipeline(target, tid)
    if not ok:
        store.update(tid, state="pre_mdcx_failed", error=note)
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="pre_mdcx_failed").inc()
        return

    # Step 6: QC
    async with m.timed_step("qc"):
        qc_result = await qc.run_qc(target)
    qc_class = m.classify_qc_reason(qc_result.reason)
    m.QC_RESULT.labels(
        result="pass" if qc_result.passed else "fail",
        reason_class=qc_class,
    ).inc()
    if not qc_result.passed:
        m.PIPELINE_STEP_TOTAL.labels(step="qc", outcome="fail").inc()
        log.warning("[qc] task=%s FAIL: %s", tid, qc_result.reason)
        code = extract_code(t.get("name") or "") or extract_code(target) or ""
        if code:
            await _retry_with_next_candidate(qbt, tid, code, h, qc_result.reason)
        else:
            store.update(
                tid, state="qc_failed",
                error=f"QC failed and no JAV code parseable for retry: {qc_result.reason}",
            )
            m.QC_RETRY.labels(outcome="no_code").inc()
            m.PIPELINE_RUN_TOTAL.labels(terminal_state="qc_failed_no_code").inc()
        return

    m.PIPELINE_STEP_TOTAL.labels(step="qc", outcome="ok").inc()
    log.info("[qc] task=%s OK: %s", tid, qc_result.reason)
    store.update(tid, state="scraping", error=None)

    # Step 7: mdcx scrape
    async with m.timed_step("mdcx"):
        result = await scrape_dir(target)
    if result.get("skipped"):
        m.MDCX_RUN.labels(result="skipped").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="skip").inc()
    elif result["rc"] == 0:
        m.MDCX_RUN.labels(result="ok").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="ok").inc()
        # Step 8: post-mdcx cleanup
        async with m.timed_step("post_cleanup"):
            post_logs = cleanup.post_mdcx_cleanup(target)
        m.FILES_DELETED.labels(category="post_mdcx").inc(
            sum(1 for line in post_logs if line.startswith("DELETE"))
        )
        m.PIPELINE_STEP_TOTAL.labels(step="post_cleanup", outcome="ok").inc()
        merged_result = {**result, "post_mdcx": post_logs[:50]}
        store.update(tid, state="scraped", mdcx_result=merged_result)
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="scraped").inc()
        log.info("[scrape] task=%s OK (post-cleanup deleted %d)", tid, len(post_logs))
    else:
        # Distinguish timeout (rc=-1 + "timed out" stderr) from generic fail.
        is_timeout = result["rc"] == -1 and "timed out" in (result.get("stderr") or "")
        m.MDCX_RUN.labels(result="timeout" if is_timeout else "fail").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="fail").inc()
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="scrape_failed").inc()
        store.update(
            tid, state="scrape_failed", mdcx_result=result,
            error=(result.get("stderr") or "")[:1000],
        )
        log.error("[scrape] task=%s FAILED rc=%s", tid, result["rc"])


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
        except Exception as e:  # never let metrics break the watcher
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
