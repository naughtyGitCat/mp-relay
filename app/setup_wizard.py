"""First-run setup wizard backend.

Surfaces mdcx configuration in the web UI instead of forcing the user to
SSH in and edit ``.env`` by hand. Two flows:

  A) **Auto-install** — runs the bundled ``setup-mdcx.ps1`` as a
     subprocess; logs stream into an in-memory deque the UI tails via
     polling. ``setup-mdcx.ps1`` itself handles uv install / git clone /
     uv sync / Chromium download / .env patching.

  B) **Point to existing install** — user supplies an ``mdcx_dir`` (e.g.
     ``E:\\mdcx-src``); we probe ``<dir>\\.venv\\Scripts\\python.exe``
     against ``mdcx.cmd.main`` then ``mdcx.cmd.crawl`` to figure out
     which CLI module is exposed, then patch ``.env`` and live-mutate
     the running ``settings`` so the next scrape uses the new paths
     without a service restart.

State for an in-flight install is module-global; only one install can
run at a time. Concurrent ``POST /api/setup/install`` requests get a 409.
The state is intentionally NOT persisted to disk — if mp-relay crashes
mid-install, the orphaned ``setup-mdcx.ps1`` process keeps running and
``setup-mdcx.ps1 -InstallDir`` is idempotent on re-run, so worst case
the user re-clicks "install" and the script picks up where it left off.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import settings
from . import mdcx_runner

log = logging.getLogger(__name__)


# CLI modules to probe in order. Auto-pick the first one that responds
# to ``--help`` cleanly. ``mdcx.cmd.main`` is the LLM-friendly wrapper
# from naughtyGitCat's local fork; ``mdcx.cmd.crawl`` is the typer-based
# entry point on upstream master. The setup wizard tries both so a fresh
# clone of either flavor works.
_CLI_CANDIDATES: tuple[str, ...] = ("mdcx.cmd.main", "mdcx.cmd.crawl")

# Cap log buffer so long installs don't OOM us. setup-mdcx.ps1 emits
# ~200 lines start-to-end normally, ~2-3K on first chromium download.
_LOG_BUFFER_MAX: int = 5000


@dataclass
class InstallState:
    """In-memory tracker for the current setup-mdcx.ps1 subprocess."""
    running: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    return_code: Optional[int] = None
    log_lines: deque = field(default_factory=lambda: deque(maxlen=_LOG_BUFFER_MAX))
    # Total lines ever appended (deque drops old lines but the counter
    # keeps growing). Clients use this for ``?since=<n>`` polling.
    total_lines: int = 0
    process: Optional[asyncio.subprocess.Process] = None


_install: InstallState = InstallState()
_install_lock: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

async def detect() -> dict:
    """Return the current mdcx config + a healthcheck-derived status.

    Used by the setup page to render the "currently configured" section
    AND by the index page to decide whether to show the setup banner.
    """
    err = await mdcx_runner.healthcheck()
    return {
        "ok": err is None,
        "error": err,
        "config": {
            "mdcx_dir": settings.mdcx_dir,
            "mdcx_python": settings.mdcx_python,
            "mdcx_module": settings.mdcx_module,
        },
        "platform": sys.platform,
    }


async def validate_path(mdcx_dir: str) -> dict:
    """Probe a user-supplied mdcx_dir to see if it's a working install.

    Returns ``{ok, python, module, error}``. Caller writes those into
    ``.env`` only if ``ok``. Validation steps:

      1. dir exists + has a ``.venv\\Scripts\\python.exe``
      2. Run ``python -m mdcx.cmd.main --help``; if rc != 0 try
         ``mdcx.cmd.crawl``. First success wins.
      3. Both fail → return error pointing at the dir's structure
         (most likely a uv-managed project that hasn't run ``uv sync``).
    """
    if not mdcx_dir or not os.path.isdir(mdcx_dir):
        return {"ok": False, "error": f"directory does not exist: {mdcx_dir!r}"}

    candidate_pythons = [
        os.path.join(mdcx_dir, ".venv", "Scripts", "python.exe"),
        os.path.join(mdcx_dir, ".venv", "bin", "python"),  # cross-platform fallback
        os.path.join(mdcx_dir, "venv", "Scripts", "python.exe"),
    ]
    py = next((p for p in candidate_pythons if os.path.isfile(p)), None)
    if not py:
        return {
            "ok": False,
            "error": f"no .venv\\Scripts\\python.exe under {mdcx_dir} — run setup-mdcx.ps1 or `uv sync` inside the dir first",
        }

    for module in _CLI_CANDIDATES:
        try:
            proc = await asyncio.create_subprocess_exec(
                py, "-m", module, "--help",
                cwd=mdcx_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
            except asyncio.TimeoutError:
                proc.kill()
                continue
            if proc.returncode == 0:
                return {"ok": True, "python": py, "module": module, "error": None}
            log.debug("validate_path: %s -m %s → rc=%s, stderr=%s",
                      py, module, proc.returncode, stderr[:200])
        except Exception as e:
            log.warning("validate_path subprocess failed: %s", e)

    return {
        "ok": False,
        "error": f"none of {_CLI_CANDIDATES!r} responded to --help in {mdcx_dir}. "
                 "Verify deps are installed (`uv sync` inside the dir) "
                 "and that mdcx/cmd/<module>.py exists.",
    }


# ---------------------------------------------------------------------------
# .env patching (line-by-line, no regex pitfalls with Windows paths)
# ---------------------------------------------------------------------------

def _env_path() -> Path:
    """Locate the running mp-relay's ``.env``. Looks in CWD first (matches
    how config.py loads it), then alongside this file."""
    cwd = Path(".env")
    if cwd.is_file():
        return cwd
    parent = Path(__file__).parent.parent / ".env"
    return parent


def write_env_keys(updates: dict[str, str]) -> Path:
    """Replace or append ``KEY=VALUE`` lines in ``.env``.

    Matches keys at the start of an uncommented line; commented variants
    (``# MDCX_DIR=...``) are left alone. Writes back with the same
    line endings as the source.
    """
    path = _env_path()
    if not path.is_file():
        # Bootstrap from .env.example if mp-relay was run without one.
        example = path.parent / ".env.example"
        if example.is_file():
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.touch()

    # ``read_text`` does universal-newlines translation by default, which
    # would silently rewrite a CRLF .env to LF on save. Read bytes + decode
    # so we observe the raw line endings and can echo them back.
    raw = path.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)

    for key, value in updates.items():
        new_line = f"{key}={value}"
        pat = re.compile(rf"^{re.escape(key)}\s*=")
        replaced = False
        for i, line in enumerate(lines):
            if pat.match(line):
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            lines.append(new_line)

    # Trim trailing empties so we don't add a new blank line every save.
    while lines and lines[-1] == "":
        lines.pop()
    # Round-trip via bytes too, to preserve our chosen line ending.
    path.write_bytes((nl.join(lines) + nl).encode("utf-8"))
    return path


def apply_settings_in_place(updates: dict[str, str]) -> None:
    """Mutate the running ``settings`` so the next mdcx call sees new
    values without an mp-relay restart. Safe because pydantic-settings'
    BaseSettings exposes attribute access.
    """
    field_map = {
        "MDCX_DIR": "mdcx_dir",
        "MDCX_PYTHON": "mdcx_python",
        "MDCX_MODULE": "mdcx_module",
    }
    for key, value in updates.items():
        attr = field_map.get(key)
        if attr and hasattr(settings, attr):
            setattr(settings, attr, value)


# ---------------------------------------------------------------------------
# Background install
# ---------------------------------------------------------------------------

def _setup_script_path() -> Path:
    """Return the path to ``setup-mdcx.ps1``.

    On the .exe-installer install layout the script sits at
    ``{install-dir}\\setup-mdcx.ps1``. On dev / scp installs we ship
    it under ``build/`` for now — both checked here so the wizard
    works in either case.
    """
    candidates = [
        Path(".") / "setup-mdcx.ps1",
        Path(__file__).parent.parent / "setup-mdcx.ps1",
        Path(__file__).parent.parent / "build" / "setup-mdcx.ps1",
    ]
    for p in candidates:
        if p.is_file():
            return p.resolve()
    raise FileNotFoundError(
        f"setup-mdcx.ps1 not found in any of: {[str(p) for p in candidates]}"
    )


async def _stream_to_buffer(stream: asyncio.StreamReader, state: InstallState) -> None:
    """Copy a subprocess output stream into the install's log buffer.
    One coroutine each for stdout + stderr; tagged so the UI knows
    which is which when we eventually want color-coding."""
    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
        state.log_lines.append(line)
        state.total_lines += 1


async def start_install() -> dict:
    """Spawn ``setup-mdcx.ps1`` in the background and start streaming its
    output into the in-memory log buffer. Returns immediately; the
    caller polls ``GET /api/setup/install/log`` for progress.

    Refuses to start a second install while one is running (returns
    ``{ok: False, error: "already running"}``).
    """
    global _install
    async with _install_lock:
        if _install.running:
            return {"ok": False, "error": "another install is already running"}

        try:
            script = _setup_script_path()
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}

        if sys.platform != "win32":
            return {"ok": False, "error": f"setup wizard only supports Windows (this is {sys.platform})"}

        install_dir = str(Path(__file__).parent.parent.resolve())

        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile",
                "-File", str(script),
                "-InstallDir", install_dir,
                "-NoServiceRestart",   # don't kill ourselves mid-install
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            log.exception("failed to spawn setup-mdcx.ps1")
            return {"ok": False, "error": f"failed to spawn powershell: {e}"}

        _install = InstallState(
            running=True,
            started_at=asyncio.get_event_loop().time(),
            process=proc,
        )

        async def _watch():
            try:
                if proc.stdout is not None:
                    await _stream_to_buffer(proc.stdout, _install)
                rc = await proc.wait()
            except Exception as e:
                _install.log_lines.append(f"[wizard] watcher crashed: {e}")
                _install.total_lines += 1
                rc = -1
            _install.return_code = rc
            _install.finished_at = asyncio.get_event_loop().time()
            _install.running = False
            _install.process = None
            log.info("setup-mdcx.ps1 finished rc=%s", rc)

        asyncio.create_task(_watch(), name="setup-mdcx-watcher")
        return {"ok": True, "started_at": _install.started_at}


def install_status(since: int = 0) -> dict:
    """Return install progress + new log lines since ``since``.

    The deque only retains the last ``_LOG_BUFFER_MAX`` lines, so if
    the client polls slowly it might miss some — return ``dropped``
    so the UI can warn. ``next_since`` is the cursor for the next poll.
    """
    s = _install
    total = s.total_lines

    # Buffer's first line corresponds to total - len(buffer). If the client's
    # cursor predates the buffer's start, some lines were evicted from the
    # capped deque — return what we have + a `dropped` count so the UI can
    # show "(N earlier lines truncated)".
    first_in_buffer = total - len(s.log_lines)
    if since >= total:
        new_lines: list[str] = []
        dropped = 0
    elif since >= first_in_buffer:
        new_lines = list(s.log_lines)[since - first_in_buffer:]
        dropped = 0
    else:
        new_lines = list(s.log_lines)
        dropped = first_in_buffer - since

    return {
        "running": s.running,
        "started_at": s.started_at,
        "finished_at": s.finished_at if not s.running else 0.0,
        "return_code": s.return_code,
        "total_lines": total,
        "next_since": total,
        "lines": new_lines,
        "dropped": dropped,
    }
