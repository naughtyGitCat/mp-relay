"""Subprocess wrapper for the user's mdcx fork at E:\\mdcx-src.

Invocation matches the pattern documented in the user's mdcx_fork memory:
    cd /d E:\\mdcx-src
    set PYTHONIOENCODING=utf-8
    chcp 65001 >nul
    .venv\\Scripts\\python.exe -m mdcx.cmd.main scrape dir <path>
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)


async def scrape_dir(path: str, *, json_output: bool = True, quiet: bool = True,
                     timeout_sec: int = 60 * 30) -> dict:
    """Run `mdcx scrape dir <path>` and return {rc, stdout, stderr}.

    Always sets PYTHONIOENCODING=utf-8 + chcp 65001 to avoid GBK mojibake on
    Chinese titles and JSON output (per memory file).
    """
    args = [
        settings.mdcx_python,
        "-m", settings.mdcx_module,
        "scrape", "dir", path,
    ]
    if quiet:
        args.append("--quiet")
    if json_output:
        args.append("--json")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # chcp doesn't matter for the child python process if PYTHONIOENCODING is set,
    # but we keep stdout decoding bytes->utf-8 ourselves below.

    log.info("mdcx scrape dir: %s", path)
    log.debug("cmd: %s (cwd=%s)", args, settings.mdcx_dir)

    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=settings.mdcx_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        # Dev / non-Windows hosts: just record the call without executing.
        log.warning("Not on Windows (sys.platform=%s); refusing to invoke mdcx.", sys.platform)
        return {
            "rc": -1,
            "stdout": "",
            "stderr": f"mdcx invocation skipped: not on Windows (sys.platform={sys.platform!r})",
            "skipped": True,
        }

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
