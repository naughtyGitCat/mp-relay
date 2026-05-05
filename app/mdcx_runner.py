"""Subprocess wrapper for the user's mdcx fork at E:\\mdcx-src.

Invocation matches the pattern documented in the user's mdcx_fork memory:
    cd /d E:\\mdcx-src
    set PYTHONIOENCODING=utf-8
    chcp 65001 >nul
    .venv\\Scripts\\python.exe -m mdcx.cmd.main scrape dir <path>

WHY scrape_dir walks the directory ourselves instead of running ``mdcx scrape
dir <path>`` directly:
   The user's mdcx fork has a bug in its ``scrape dir`` CLI path —
   ``_run_scrape`` calls ``manager.load()`` AFTER ``scrape_dir`` overrides
   ``manager.config.media_path``, which clobbers the override back to whatever
   the on-disk config says. Result: mdcx scans the user's GUI media_path
   (e.g. ``J:/Downloads/Share``) instead of our staging dir, finds 0 files,
   reports ``total: 0``. Verified 2026-05-05 against 63 tasks where the file
   was clearly present on disk in our staging dir.

   Workaround: enumerate top-level video files ourselves and call ``mdcx
   scrape file <path>`` once per file. ``scrape file`` (FileMode.Single) goes
   through a different code path that doesn't read media_path at all — it
   takes the file path directly via ``Flags.single_file_path``.

   Tradeoff: one mdcx subprocess per video instead of one per dir. For
   single-file 115 tasks (the common case in mp-relay) this is identical
   cost; for multi-part folders it's N subprocesses. Acceptable given
   typical N is 1-3 and each scrape takes seconds, not minutes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)


# Match mdcx's media_type list (verified 2026-05-05 against the user's config).
# Kept in sync as a frozen set so the dir walk picks the same files mdcx would
# have picked if its own scan were working.
_VIDEO_EXTS: frozenset[str] = frozenset({
    ".mp4", ".mkv", ".avi", ".rmvb", ".wmv", ".mov", ".flv",
    ".ts", ".webm", ".iso", ".mpg", ".m4v",
})

# Guard against thundering-herd against the scraper sites (JavBus etc.). The
# normal qBT path serializes via the watcher, but when the retry endpoint
# fires 60+ fire-and-forget tasks at once we must throttle here too. 2 in
# flight is enough to keep the disk + scraper warm without hitting per-host
# rate limits. Lives at module scope so all callers share the same gate.
_MDCX_CONCURRENCY: int = 2
_mdcx_semaphore: asyncio.Semaphore = asyncio.Semaphore(_MDCX_CONCURRENCY)


def _enumerate_video_files(target: Path) -> list[Path]:
    """Walk ``target`` recursively, return video files in deterministic order.

    Filters out trailers / sample / behind-the-scenes the same way mdcx does,
    so we don't accidentally feed it noise."""
    if not target.exists():
        return []
    if target.is_file():
        return [target] if target.suffix.lower() in _VIDEO_EXTS else []

    out: list[Path] = []
    for root, dirs, files in os.walk(target):
        # Mirror mdcx's directory-skip rules (base/file.py:movie_lists)
        dirs[:] = [d for d in dirs if "behind the scenes" not in d.lower()]
        for f in files:
            low = f.lower()
            if low.startswith("."):
                continue
            if "trailer." in low or "trailers." in low or "theme_video." in low:
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in _VIDEO_EXTS:
                out.append(Path(root) / f)
    out.sort()
    return out


async def _run_mdcx(args: list[str], *, timeout_sec: int) -> dict:
    """Spawn mdcx with our standard env. Returns {rc, stdout, stderr}.
    Acquires ``_mdcx_semaphore`` to keep concurrent mdcx subprocess count
    bounded (see ``_MDCX_CONCURRENCY`` for rationale)."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    if sys.platform != "win32":
        log.warning("Not on Windows (sys.platform=%s); refusing to invoke mdcx.", sys.platform)
        return {
            "rc": -1,
            "stdout": "",
            "stderr": f"mdcx invocation skipped: not on Windows (sys.platform={sys.platform!r})",
            "skipped": True,
        }

    async with _mdcx_semaphore:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=settings.mdcx_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"rc": -1, "stdout": "", "stderr": f"mdcx timed out after {timeout_sec}s"}

    return {
        "rc": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


def _merge_summaries(summaries: list[dict]) -> dict:
    """Combine per-file mdcx JSON summaries into one dir-shaped summary so
    ``post_download._parse_mdcx_summary`` keeps working unchanged."""
    total = sum(s.get("total", 0) for s in summaries)
    success = sum(s.get("success", 0) for s in summaries)
    failed = sum(s.get("failed", 0) for s in summaries)
    failed_items: list = []
    for s in summaries:
        failed_items.extend(s.get("failed_items") or [])
    return {
        "total": total,
        "success": success,
        "failed": failed,
        "failed_items": failed_items,
    }


async def scrape_dir(path: str, *, json_output: bool = True, quiet: bool = True,
                     timeout_sec: int = 60 * 30) -> dict:
    """Scrape every video file under ``path`` via mdcx ``scrape file``.

    See module docstring for why we don't use mdcx's own ``scrape dir`` CLI.

    Returns ``{rc, stdout, stderr}``. ``stdout`` is a synthesized JSON summary
    that matches what ``mdcx scrape dir --json`` *should* have returned, so
    callers (post_download) parse it identically.

    Two empty-dir flavors:
      - target doesn't exist  → ``"already_scraped"`` flag (mdcx's
        ``success_file_move`` + ``del_empty_folder`` already moved everything
        out + cleaned up). Caller should treat as success, not failure.
      - target exists but has no video files → standard ``total: 0`` so
        ``post_download._parse_mdcx_summary`` still classifies as
        ``scrape_no_match`` (genuine: file never landed, or unrecognized ext).
    """
    target = Path(path)
    if not target.exists():
        # Strong signal that mdcx already processed this dir: a successful
        # ``scrape file`` moves the video to ``success_output_folder`` and
        # ``del_empty_folder=True`` removes the now-empty parent. If we
        # re-enter on a request to retry, the dir is gone → that's a
        # *previous-run success*, not a new failure. Without this branch a
        # retry burst against orphaned-but-completed subprocs (see PR #23
        # narrative) gets classified as ``scrape_failed`` — happened to 56/63
        # tasks during the 145-batch recovery before this fix.
        log.info("mdcx scrape dir: %s already gone (likely previously scraped)", path)
        return {
            "rc": 0,
            "stdout": '{"total": 1, "success": 1, "failed": 0, "failed_items": [], "already_scraped": true}',
            "stderr": "",
            "already_scraped": True,
        }

    files = _enumerate_video_files(target)

    log.info("mdcx scrape dir: %s (found %d video files)", path, len(files))
    if not files:
        # Same shape post_download._parse_mdcx_summary classifies as
        # scrape_no_match — preserves the existing diagnostic.
        return {
            "rc": 0,
            "stdout": '{"total": 0, "success": 0, "failed": 0, "failed_items": []}',
            "stderr": "",
        }

    # Per-file timeout = total timeout / N, with a sane floor so a 1-file
    # batch doesn't get the full 30 minutes (and an N=20 folder still gives
    # each file 90 seconds minimum).
    per_file_timeout = max(timeout_sec // max(len(files), 1), 90)

    summaries: list[dict] = []
    last_stderr: list[str] = []
    aggregate_rc = 0

    for f in files:
        args = [
            settings.mdcx_python,
            "-m", settings.mdcx_module,
            "scrape", "file", str(f),
        ]
        if quiet:
            args.append("--quiet")
        if json_output:
            args.append("--json")

        log.debug("mdcx scrape file: %s", f)
        result = await _run_mdcx(args, timeout_sec=per_file_timeout)

        if result["rc"] != 0:
            aggregate_rc = result["rc"]
            last_stderr.append(f"{f.name}: rc={result['rc']} {result['stderr'][:200]}")

        # Parse the JSON summary mdcx prints; if absent (e.g. fatal startup
        # error), synthesize a 1-failed-1-total entry so the file still
        # counts as "attempted" rather than vanishing.
        parsed = _parse_mdcx_stdout(result["stdout"])
        if parsed is None:
            parsed = {
                "total": 1, "success": 0, "failed": 1,
                "failed_items": [{"path": str(f), "reason": result["stderr"][:200] or "no JSON output"}],
            }
        summaries.append(parsed)

    merged = _merge_summaries(summaries)
    import json as _json
    return {
        "rc": aggregate_rc,
        "stdout": _json.dumps(merged, ensure_ascii=False),
        "stderr": "\n".join(last_stderr),
    }


def _parse_mdcx_stdout(stdout: str) -> Optional[dict]:
    """mdcx prints chatty preamble lines + a final JSON object. Find that
    JSON object so callers always see a parseable summary."""
    if not stdout:
        return None
    import json as _json
    # Walk backward looking for a balanced { ... } at end of output.
    i = stdout.rfind("{")
    j = stdout.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    try:
        return _json.loads(stdout[i:j + 1])
    except _json.JSONDecodeError:
        return None


async def healthcheck() -> Optional[str]:
    """Return None if mdcx CLI works, else error string."""
    if sys.platform != "win32":
        return f"not on Windows (sys.platform={sys.platform!r})"
    if not os.path.isfile(settings.mdcx_python):
        return f"mdcx python not found: {settings.mdcx_python}"
    if not os.path.isdir(settings.mdcx_dir):
        return f"mdcx dir not found: {settings.mdcx_dir}"

    proc = await asyncio.create_subprocess_exec(
        settings.mdcx_python, "-m", settings.mdcx_module, "--help",
        cwd=settings.mdcx_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        return "mdcx --help timed out"
    if proc.returncode != 0:
        return f"mdcx --help failed: rc={proc.returncode} stderr={stderr.decode('utf-8', errors='replace')[:200]}"
    return None
