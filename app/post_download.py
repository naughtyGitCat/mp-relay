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

import datetime
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import cleanup, merger, metrics as m, notify, qc, store
from .config import settings
from .exists import extract_code
from .mdcx_runner import scrape_dir

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failed-scrape holding — move staging dirs to a holding location on failure
# so they're easy to find / retry / bulk-delete later.
# ---------------------------------------------------------------------------

def _move_to_failed_holding(staging_dir: str, kind: str) -> Optional[str]:
    """Move ``staging_dir`` to a failed-holding location.

    ``kind`` picks the subdir name and is one of:
      - ``"scrape"`` — mdcx-side failures (rc != 0 / no_match / failed_items)
      - ``"qc"``     — QC-side failures (fake video, undersize, etc.)

    Resolves destination root as:
      - ``settings.failed_output_dir / <kind>...`` if set
      - else ``<staging_parent> / <kind>...`` (sibling-collector default)

    Returns the new path, or ``None`` if nothing to move:
      - target dir doesn't exist (already moved by mdcx, or never existed)
      - target dir is empty (mdcx moved files out, left empty staging)
      - filesystem move failed (logged at WARNING)

    On success the original ``staging_dir`` no longer exists. Conflict
    handling: if dest path is taken, append a ``.YYYYmmdd-HHMMSS`` suffix.
    """
    if not staging_dir or not os.path.isdir(staging_dir):
        return None
    try:
        if not any(os.scandir(staging_dir)):
            log.debug("[failed-move] %s is empty — mdcx already moved files; no-op", staging_dir)
            return None
    except OSError as e:
        log.warning("[failed-move] could not scan %s: %s", staging_dir, e)
        return None

    src = Path(staging_dir)
    sub = "scrapefailed" if kind == "scrape" else "qcfailed"
    base = (settings.failed_output_dir or "").strip()
    dest_root = Path(base) / sub if base else src.parent / sub

    dest = dest_root / src.name
    if dest.exists():
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = dest.with_name(f"{dest.name}.{ts}")

    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    except OSError as e:
        log.warning("[failed-move] %s -> %s failed: %s", src, dest, e)
        return None

    log.info("[failed-move] %s -> %s (kind=%s)", src, dest, kind)
    return str(dest)


# ---------------------------------------------------------------------------
# Path / filename sanitization for mdcx compatibility
# ---------------------------------------------------------------------------
#
# mdcx's internal scanning uses Python ``Path.glob`` / ``rglob`` which treat
# ``[...]`` as character classes. Folders + filenames with ``[4K]`` /
# ``[H265]`` / ``[CN]`` pass through every other tool (qBT, ffmpeg, our
# triage) silently, then mdcx returns "Movie folder does not exist" or
# scans an empty list and reports total=0. The pipeline marks the task
# scraped in either case — quiet failure.
#
# Real example that triggered this fix: 115 sync produced
#     G:\Downloads\JAV-staging\SNOS-073.[4K]@R90s\169bbs.com@SNOS-073_[4K].mkv
# — both dir and file have ``[4K]``. mdcx scanned 0 files; we wrote
# ``state=scraped`` and the user discovered it manually a day later.
#
# Fix: rename the staging dir + the video files inside it to a safe form
# before invoking mdcx. The replacements are conservative — only chars
# known to interact with glob / mdcx's filename regex are stripped:
#   [, ], (, ), {, }   — glob char classes and grouping
#   @                   — sometimes used as fragment separator
# Unicode + spaces + Chinese characters are left alone (mdcx handles them).

_UNSAFE_CHARS_RE: re.Pattern = re.compile(r"[\[\](){}@]")


def _sanitize_name(name: str) -> str:
    """Replace mdcx-incompatible chars with underscore; collapse runs;
    trim leading/trailing punctuation. Preserves the file extension separator."""
    out = _UNSAFE_CHARS_RE.sub("_", name)
    out = re.sub(r"_+", "_", out).strip("_-. ")
    return out or "unnamed"


def _sanitize_target_dir(target: str) -> str:
    """If the target dir name has unsafe chars, rename it. Returns the
    (possibly new) absolute path."""
    p = Path(target)
    if not p.exists():
        return target
    safe_stem = _sanitize_name(p.name)
    if safe_stem == p.name:
        return target
    # On collision, append numeric suffix.
    new_p = p.parent / safe_stem
    n = 1
    while new_p.exists() and new_p.resolve() != p.resolve():
        n += 1
        new_p = p.parent / f"{safe_stem}-{n}"
        if n > 100:
            log.warning("could not pick safe dir name for %s, leaving as-is", p.name)
            return target
    try:
        p.rename(new_p)
        log.info("sanitized staging dir: %s → %s", p.name, new_p.name)
        return str(new_p)
    except OSError as e:
        log.warning("dir rename failed (%s); leaving original path", e)
        return target


# Video extensions we'll rename if their basename has unsafe chars. We don't
# touch images / .nfo / etc. — mdcx's filename regex only runs on these.
_VIDEO_EXTS_FOR_SANITIZE: frozenset[str] = frozenset({
    ".mp4", ".mkv", ".avi", ".wmv", ".m4v", ".mov", ".ts",
})


def _sanitize_video_filenames(target: str) -> list[str]:
    """Rename top-level video files whose basenames contain unsafe chars.

    Returns a list of human-readable note lines for the audit trail.
    """
    base = Path(target)
    notes: list[str] = []
    if not base.is_dir():
        return notes
    try:
        children = list(base.iterdir())
    except OSError as e:
        log.warning("can't list %s: %s", base, e)
        return notes
    for p in children:
        if not p.is_file() or p.suffix.lower() not in _VIDEO_EXTS_FOR_SANITIZE:
            continue
        safe_stem = _sanitize_name(p.stem)
        if safe_stem == p.stem:
            continue
        new_p = p.with_name(f"{safe_stem}{p.suffix}")
        if new_p.exists() and new_p.resolve() != p.resolve():
            notes.append(f"skip rename {p.name} → {new_p.name} (target exists)")
            continue
        try:
            p.rename(new_p)
            notes.append(f"renamed for mdcx: {p.name} → {new_p.name}")
            log.info("sanitized video file: %s → %s", p.name, new_p.name)
        except OSError as e:
            notes.append(f"rename failed: {p.name} ({e})")
    return notes


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

    # No retry path possible — terminal failure. Move staging to qcfailed/
    # so it's out of the active staging dir + easy to bulk-clean later.
    moved_to = _move_to_failed_holding(target, kind="qc")
    store.update(
        tid, state="qc_failed",
        error=f"QC failed and no retry path: {qc_result.reason}",
        save_path=moved_to or target,
    )
    m.QC_RETRY.labels(outcome="no_code" if not code else "no_handler").inc()
    m.PIPELINE_RUN_TOTAL.labels(terminal_state="qc_failed_no_code").inc()
    await notify.notify(
        "qc_failed_no_code" if not code else "qc_failed_no_retry",
        "QC 失败" + ("，但无法从名称提取番号无法自动重试" if not code else "，且当前路径不支持重试"),
        task=tid, name=name[:80], reason=qc_result.reason,
        moved_to=moved_to or "(unchanged)",
    )
    return False


# ---------------------------------------------------------------------------
# Steps 7-8: mdcx + post-cleanup
# ---------------------------------------------------------------------------

def _parse_mdcx_summary(stdout: str) -> dict:
    """Pull the {total, success, failed, failed_items} object out of mdcx's
    stdout. Returns ``{"total": int, "success": int, "failed": int,
    "failed_items": list}``.

    mdcx (the user's fork) writes a JSON object to stdout when invoked with
    ``--json``. The object is a single line; if for some reason it's
    interleaved with other output we still try to find it via brace match.
    """
    out = {"total": 0, "success": 0, "failed": 0, "failed_items": []}
    if not stdout:
        return out
    # Try parsing the whole thing; if that fails, find the largest JSON object
    # by simple brace count.
    candidates: list[str] = []
    try:
        json.loads(stdout)
        candidates.append(stdout)
    except (json.JSONDecodeError, ValueError):
        depth = 0
        start = -1
        for i, ch in enumerate(stdout):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append(stdout[start:i + 1])
                    start = -1
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "total" in parsed and "success" in parsed:
            out["total"] = int(parsed.get("total") or 0)
            out["success"] = int(parsed.get("success") or 0)
            out["failed"] = int(parsed.get("failed") or 0)
            items = parsed.get("failed_items") or []
            if isinstance(items, list):
                out["failed_items"] = items[:5]
            break
    return out


async def _scrape_and_postclean(target: str, tid: str, name: str) -> None:
    """Run mdcx scrape; classify outcome by parsing mdcx's summary; emit
    metrics + notify accordingly.

    Outcome states:
      - scraped              : mdcx success >= 1 (genuine win)
      - scrape_no_match      : mdcx rc=0 but total=0 (silently scanned nothing)
      - scrape_failed_items  : mdcx rc=0 and total>0 but success=0 (every file
                                rejected — wrong番号前缀 / no crawler match)
      - scrape_failed        : mdcx rc != 0 (subprocess error / timeout)
    """
    store.update(tid, state="scraping", error=None)
    async with m.timed_step("mdcx"):
        result = await scrape_dir(target)

    if result.get("skipped"):
        m.MDCX_RUN.labels(result="skipped").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="skip").inc()
        return

    if result["rc"] != 0:
        # Subprocess error / timeout — bubble up as before.
        is_timeout = result["rc"] == -1 and "timed out" in (result.get("stderr") or "")
        m.MDCX_RUN.labels(result="timeout" if is_timeout else "fail").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="fail").inc()
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="scrape_failed").inc()
        moved_to = _move_to_failed_holding(target, kind="scrape")
        store.update(
            tid, state="scrape_failed", mdcx_result=result,
            error=(result.get("stderr") or "")[:1000],
            save_path=moved_to or target,
        )
        log.error("[scrape] task=%s FAILED rc=%s", tid, result["rc"])
        await notify.notify(
            "scrape_failed",
            f"mdcx 刮削失败 (rc={result['rc']}{'/timeout' if is_timeout else ''})",
            task=tid, name=name[:80],
            stderr=(result.get("stderr") or "")[:200],
            moved_to=moved_to or "(unchanged)",
        )
        return

    # rc == 0 — but did it actually scrape anything?
    summary = _parse_mdcx_summary(result.get("stdout") or "")
    log.info(
        "[scrape] task=%s mdcx summary: total=%d success=%d failed=%d",
        tid, summary["total"], summary["success"], summary["failed"],
    )

    if summary["success"] > 0:
        # Genuine success path
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

    if summary["total"] == 0:
        # mdcx ran cleanly but matched 0 video files in target dir.
        # Common cause: filename has chars mdcx's regex rejects ([4K] / @ / 169bbs.com prefix),
        # or path contains [] which mdcx's glob misinterprets. Treat as failure
        # so the user is told instead of silently moving on.
        m.MDCX_RUN.labels(result="no_match").inc()
        m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="fail").inc()
        m.PIPELINE_RUN_TOTAL.labels(terminal_state="scrape_no_match").inc()
        moved_to = _move_to_failed_holding(target, kind="scrape")
        store.update(
            tid, state="scrape_no_match", mdcx_result=result,
            error="mdcx scanned 0 files (filename / path chars may block detection)",
            save_path=moved_to or target,
        )
        log.warning("[scrape] task=%s NO_MATCH (total=0) at %s", tid, target)
        await notify.notify(
            "scrape_no_match",
            "mdcx 跑完但识别到 0 个视频 — 检查文件名是否含 []/@/前缀",
            task=tid, name=name[:80], path=moved_to or target,
        )
        return

    # total > 0 and success == 0 → mdcx counted N files but every one rejected.
    # Typical reason: 番号前缀不在白名单 (SNOS / niche studios) or all crawlers
    # failed. Show the user the failure reasons so they know what to fix.
    reasons = [
        f"{(it.get('path') or '')[-40:]}: {it.get('reason', '?')}"
        for it in summary["failed_items"][:3]
    ]
    reason_summary = " | ".join(reasons) or "(no reasons reported)"
    m.MDCX_RUN.labels(result="failed_items").inc()
    m.PIPELINE_STEP_TOTAL.labels(step="mdcx", outcome="fail").inc()
    m.PIPELINE_RUN_TOTAL.labels(terminal_state="scrape_failed_items").inc()
    moved_to = _move_to_failed_holding(target, kind="scrape")
    store.update(
        tid, state="scrape_failed_items", mdcx_result=result,
        error=f"mdcx counted {summary['total']} but all failed: {reason_summary}"[:500],
        save_path=moved_to or target,
    )
    log.warning(
        "[scrape] task=%s FAILED_ITEMS total=%d failed=%d: %s",
        tid, summary["total"], summary["failed"], reason_summary,
    )
    await notify.notify(
        "scrape_failed_items",
        f"mdcx 识别到 {summary['total']} 个文件但全部失败",
        task=tid, name=name[:80], reason=reason_summary[:200],
        moved_to=moved_to or "(unchanged)",
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
    # Step 0 — sanitize the staging dir name for mdcx compat. Python's Path.glob
    # treats [] as char classes, and mdcx's Path.walk silently returns empty if
    # the start path's name is a non-matching glob. We rename eagerly so every
    # downstream step (triage included) sees the safe path.
    safe_target = _sanitize_target_dir(target)
    if safe_target != target:
        store.update(tid, save_path=safe_target)
        target = safe_target

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

    # Step 5b — sanitize the inner video filenames AFTER triage/merge so any
    # multi-part renames have settled. mdcx's filename regex chokes on ``[4K]``
    # / ``@`` prefixes (e.g. ``169bbs.com@SNOS-073_[4K].mkv`` is silently
    # skipped by its name parser even when the dir is fine).
    sanitize_notes = _sanitize_video_filenames(target)
    if sanitize_notes:
        log.info("[scrape] task=%s sanitized %d video filenames", tid, len(sanitize_notes))

    # Step 6
    if not await _qc_and_maybe_retry(target, tid, name, failed_hash, retry_handler):
        return

    # Steps 7-8
    await _scrape_and_postclean(target, tid, name)
