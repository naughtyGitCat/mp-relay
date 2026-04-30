"""Post-download pipeline — runs after a video file is on local disk.

Extracted from ``watcher.py`` so both the qBT path (Phase 0/3) and the
upcoming cloud-115 sync path (Phase 1.9) can drive the same set of steps:

  1. triage         — classify files: keep / junk / dupe / sample / extras / parts / disc
  2. execute        — delete junk + dupes + samples (extras preserved)
  3. relocate       — move extras into Extras/
  4. remux disc     — BDMV / VIDEO_TS → single .mkv
  5. merge parts    — multipart concat-copy (or Jellyfin <name>-cd1.ext fallback)
  6. QC             — ffprobe duration + min-size sanity check
                      on FAIL → call retry_handler (qBT-aware) or just mark failed
  7. mdcx           — scrape metadata
  8. post-cleanup   — sweep leftover .url/.txt/sample after mdcx

The pipeline doesn't care whether the file came from a torrent download or a
cloud sync. The caller passes (target_dir, task_id, optional retry_handler).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from . import cleanup, merger, metrics as m, notify, qc, store
from .config import settings
from .exists import extract_code
from .mdcx_runner import scrape_dir

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry handler protocol
# ---------------------------------------------------------------------------

# Called when QC fails. Should attempt to swap to an alternate source. Returns
# True if a retry was queued (caller should NOT mark task as terminal-failed),
# False if no retry is possible. The qBT-driven watcher passes a handler that
# adds the next sukebei/JavBus candidate to the JAV category. The cloud-115
# path passes None — once a file is downloaded from cloud, retrying means
# pushing a new magnet to 115 offline, which is a different lifecycle.
QcRetryHandler = Callable[[str, str, str, str], Awaitable[bool]]
# Args: (tid, code, failed_hash, qc_reason)


# ---------------------------------------------------------------------------
# Steps 1-5: pre-mdcx (triage / cleanup / extras / disc remux / multipart merge)
# ---------------------------------------------------------------------------

async def _run_pre_mdcx_pipeline(target: str, tid: str) -> tuple[bool, str]:
    """Returns (ok, note). On ok=False the caller should mark the task failed
    and skip mdcx; ``note`` is a human-readable reason."""
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

    # 4. Disc archive remux
    if settings.remux_disc_archives and triage.disc_archive is not None:
        async with m.timed_step("remux_disc"):
            rr = await merger.remux_disc(triage.disc_archive)
        notes.append(rr.note)
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

    # 5. Multipart merge
    if settings.merge_multipart and len(triage.multipart_parts) >= 2:
        async with m.timed_step("merge_parts"):
            mr = await merger.merge_parts(triage.multipart_parts)
        notes.append(mr.note)
        if mr.merged_path is None:
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


# ---------------------------------------------------------------------------
# Step 6: QC + retry chain
# ---------------------------------------------------------------------------

async def _qc_and_maybe_retry(
    target: str,
    tid: str,
    name: str,
    failed_hash: str,
    retry_handler: Optional[QcRetryHandler],
) -> bool:
    """Run QC. On fail, dispatch to retry_handler if present.

    Returns True if QC passed (caller proceeds to mdcx).
    Returns False if QC failed (caller should not mdcx; task state already
    written by this function or by the retry_handler).
    """
    async with m.timed_step("qc"):
        qc_result = await qc.run_qc(target)
    qc_class = m.classify_qc_reason(qc_result.reason)
    m.QC_RESULT.labels(
        result="pass" if qc_result.passed else "fail",
        reason_class=qc_class,
    ).inc()

    if qc_result.passed:
        m.PIPELINE_STEP_TOTAL.labels(step="qc", outcome="ok").inc()
        log.info("[qc] task=%s OK: %s", tid, qc_result.reason)
        return True

    m.PIPELINE_STEP_TOTAL.labels(step="qc", outcome="fail").inc()
    log.warning("[qc] task=%s FAIL: %s", tid, qc_result.reason)
    code = extract_code(name) or extract_code(target) or ""

    if code and retry_handler is not None:
        await retry_handler(tid, code, failed_hash, qc_result.reason)
        return False

    # No retry path possible — terminal failure.
    store.update(
        tid, state="qc_failed",
        error=f"QC failed and no retry path: {qc_result.reason}",
    )
    m.QC_RETRY.labels(outcome="no_code" if not code else "no_handler").inc()
    m.PIPELINE_RUN_TOTAL.labels(terminal_state="qc_failed_no_code").inc()
    await notify.notify(
        "qc_failed_no_code" if not code else "qc_failed_no_retry",
        "QC 失败" + ("，但无法从名称提取番号无法自动重试" if not code else "，且当前路径不支持重试"),
        task=tid, name=name[:80], reason=qc_result.reason,
    )
    return False


# ---------------------------------------------------------------------------
# Steps 7-8: mdcx + post-cleanup
# ---------------------------------------------------------------------------

async def _scrape_and_postclean(target: str, tid: str, name: str) -> None:
    """Run mdcx scrape; on success, sweep leftover junk; emit metrics + notify."""
    store.update(tid, state="scraping", error=None)
    async with m.timed_step("mdcx"):
        result = await scrape_dir(target)

    if result.get("skipped"):
        m.MDCX_RUN.labels(result="skipped").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="skip").inc()
        return

    if result["rc"] == 0:
        m.MDCX_RUN.labels(result="ok").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="ok").inc()
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
        await notify.notify(
            "scraped",
            "刮削完成 ✅",
            task=tid, name=name[:80], path=target,
        )
        return

    # Non-zero rc — distinguish timeout from generic fail.
    is_timeout = result["rc"] == -1 and "timed out" in (result.get("stderr") or "")
    m.MDCX_RUN.labels(result="timeout" if is_timeout else "fail").inc()
    m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="fail").inc()
    m.PIPELINE_RUN_TOTAL.labels(terminal_state="scrape_failed").inc()
    store.update(
        tid, state="scrape_failed", mdcx_result=result,
        error=(result.get("stderr") or "")[:1000],
    )
    log.error("[scrape] task=%s FAILED rc=%s", tid, result["rc"])
    await notify.notify(
        "scrape_failed",
        f"mdcx 刮削失败 (rc={result['rc']}{'/timeout' if is_timeout else ''})",
        task=tid, name=name[:80],
        stderr=(result.get("stderr") or "")[:200],
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

async def run_pipeline(
    target: str,
    tid: str,
    *,
    name: str = "",
    failed_hash: str = "",
    retry_handler: Optional[QcRetryHandler] = None,
) -> None:
    """Run the full post-download pipeline against ``target`` directory.

    :param target: absolute path to the directory containing the downloaded files
    :param tid: task id in mp-relay's tasks table; state is updated as we go
    :param name: torrent / file name used for code extraction + Telegram messages
    :param failed_hash: info_hash of the current source — passed to retry_handler
                        on QC fail so it can dedup against tried hashes
    :param retry_handler: callback to invoke on QC failure (qBT path supplies one;
                          cloud-115 path passes None)
    """
    # Steps 1-5
    ok, note = await _run_pre_mdcx_pipeline(target, tid)
    if not ok:
        store.update(tid, state="pre_mdcx_failed", error=note)
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="pre_mdcx_failed").inc()
        await notify.notify(
            "pre_mdcx_failed",
            "下载完成但 pre-mdcx 流水线失败（disc remux 等步骤）",
            task=tid, name=name[:80], reason=note,
        )
        return

    # Step 6
    if not await _qc_and_maybe_retry(target, tid, name, failed_hash, retry_handler):
        return

    # Steps 7-8
    await _scrape_and_postclean(target, tid, name)
