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

import httpx

from .config import settings
from . import mdcx_runner

log = logging.getLogger(__name__)


# Short timeout for connectivity probes — these run synchronously when the
# user clicks "Test connection", so they need to fail fast on a stale URL.
_PROBE_TIMEOUT_SEC: float = 6.0


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
    """Mutate the running ``settings`` so the next service call sees new
    values without an mp-relay restart. Safe because pydantic-settings'
    BaseSettings exposes attribute access.

    Maps the .env-style UPPER_SNAKE keys to settings' lower_snake attrs.
    Adding a new config? Extend ``_FIELD_MAP``.
    """
    for key, value in updates.items():
        attr = _FIELD_MAP.get(key)
        if attr and hasattr(settings, attr):
            setattr(settings, attr, value)


# .env key -> settings attribute name. Extend as new services are wired
# into the setup page.
_FIELD_MAP: dict[str, str] = {
    "MDCX_DIR": "mdcx_dir",
    "MDCX_PYTHON": "mdcx_python",
    "MDCX_MODULE": "mdcx_module",
    "FAILED_OUTPUT_DIR": "failed_output_dir",
    "MP_URL": "mp_url",
    "MP_USER": "mp_user",
    "MP_PASS": "mp_pass",
    "QBT_URL": "qbt_url",
    "QBT_USER": "qbt_user",
    "QBT_PASS": "qbt_pass",
    "JELLYFIN_URL": "jellyfin_url",
    "JELLYFIN_API_KEY": "jellyfin_api_key",
}


# ---------------------------------------------------------------------------
# mdcx config bridge — uses the fork's CLI (`mdcx config get/set/path`)
# rather than parsing config.v2.json directly. Insulates us from mdcx's
# config schema, lets mdcx validate on write, and avoids needing to know
# where config.v2.json lives. Cached for cheap polling.
# ---------------------------------------------------------------------------

async def _mdcx_config_invoke(*args: str) -> tuple[int, str, str]:
    """Run ``python -m mdcx.cmd.main config <args...>``. Returns
    ``(rc, stdout, stderr)``. ``rc=-1`` if the call couldn't be
    spawned (mdcx not configured / not on Windows / missing python)."""
    if sys.platform != "win32":
        return (-1, "", "not on Windows")
    if not (settings.mdcx_python and settings.mdcx_dir and settings.mdcx_module):
        return (-1, "", "mdcx not configured")
    py = settings.mdcx_python
    if not os.path.isfile(py):
        return (-1, "", f"mdcx python not found: {py}")
    try:
        proc = await asyncio.create_subprocess_exec(
            py, "-m", settings.mdcx_module, "config", *args,
            cwd=settings.mdcx_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (asyncio.TimeoutError, OSError) as e:
        return (-1, "", f"spawn failed: {e}")
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def mdcx_config_get(key: str) -> Optional[str]:
    """Run ``mdcx config get <key>`` and return the value (or None on error
    / mdcx not configured). The fork prints the value as a JSON string,
    so strip surrounding quotes if present."""
    rc, stdout, _ = await _mdcx_config_invoke("get", key)
    if rc != 0:
        return None
    raw = stdout.strip()
    # mdcx's `config get` emits a JSON-quoted string for str fields; strip.
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        raw = raw[1:-1]
    return raw


async def mdcx_config_set(key: str, value: str) -> dict:
    """Run ``mdcx config set <key> <value>`` to mutate mdcx's persisted
    config (config.v2.json). The fork validates against an internal
    ``_FIELDS`` whitelist; non-whitelisted keys return non-zero. Empty
    string is a valid value for path-type fields (means "unset")."""
    rc, stdout, stderr = await _mdcx_config_invoke("set", key, value)
    if rc == 0:
        return {"ok": True, "stdout": stdout.strip()}
    return {"ok": False, "rc": rc, "stderr": (stderr or stdout).strip()[:500]}


# Fields the /setup page surfaces. Split into editable (must be in mdcx's
# own _FIELDS whitelist — verified 2026-05-09) vs display-only (mdcx
# refuses ``config set``, but we still ``config get`` and warn if the
# value would break mp-relay's pipeline assumptions). Source of truth
# for "what's editable" is mdcx, not us — we just mirror the subset we
# care about.
MDCX_EDITABLE_FIELDS: tuple[str, ...] = (
    "success_output_folder",   # where mdcx puts successful scrapes -> Jellyfin lib root
    "failed_output_folder",    # mdcx-side failure landing (already covered by our takeover button)
    "website_single",          # primary scrape source when scrape_like=single
    "scrape_like",             # single | multi | more | escape
    "media_path",              # default scan dir for `mdcx scrape dir` (no path → cwd)
    "proxy",                   # http/socks proxy URL for crawlers
    "timeout",                 # int — per-crawler request timeout (sec)
    "retry",                   # int — per-crawler retry count
)

# These mdcx settings are NOT in the whitelist, so we display read-only.
# We assert the "expected" value mp-relay's pipeline assumes, and the
# UI shows a warning when it differs (user has to fix in mdcx GUI).
MDCX_READONLY_FIELDS: dict[str, str] = {
    "success_file_move":  "true",   # mp-relay assumes mdcx moves on success
    "del_empty_folder":   "true",   # post_download.run_pipeline relies on this
    "download_files":     "*poster*thumb*fanart*",  # substring check; downloads must include images
}


async def mdcx_get_surfaced_config() -> dict:
    """Fetch all surfaced mdcx fields in a single call. Each value is the
    raw string mdcx's CLI emits (quotes stripped for ``str``-typed; raw
    JSON for arrays/bools — caller handles formatting)."""
    out: dict[str, Optional[str]] = {}
    # Concurrent get for snappier /setup page load
    keys = list(MDCX_EDITABLE_FIELDS) + list(MDCX_READONLY_FIELDS.keys())
    results = await asyncio.gather(
        *(mdcx_config_get(k) for k in keys),
        return_exceptions=True,
    )
    for k, v in zip(keys, results):
        out[k] = None if isinstance(v, Exception) else v
    return out


# ---------------------------------------------------------------------------
# Connectivity probes — run on "Test connection" click. Each returns the
# usual {ok, error, ...detail} shape so the UI can surface success/failure
# inline instead of waiting for the next /health call.
# ---------------------------------------------------------------------------

async def probe_moviepilot(url: str, user: str, password: str) -> dict:
    """POST /api/v1/login/access-token against the supplied URL. Returns
    ``{ok, error, version}``. We use the login endpoint (not /api/v1/system/
    status) because (a) that's what a real client does, (b) it actually
    validates the credentials rather than just confirming the URL is up.
    """
    if not url:
        return {"ok": False, "error": "URL is required"}
    base = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SEC) as c:
            r = await c.post(
                f"{base}/api/v1/login/access-token",
                data={"username": user, "password": password},
            )
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"HTTP error: {e}"}
    if r.status_code == 200:
        try:
            tok = r.json().get("access_token")
            if tok:
                return {"ok": True, "error": None}
        except Exception:
            pass
        return {"ok": False, "error": "200 but no access_token in response"}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "credentials rejected (401/403) — check MP_USER / MP_PASS"}
    return {"ok": False, "error": f"unexpected HTTP {r.status_code}: {r.text[:200]}"}


async def probe_qbt(url: str, user: str, password: str) -> dict:
    """POST /api/v2/auth/login against qBT WebUI. qBT returns plain "Ok."
    on success and a 200 with a non-Ok body on bad creds (legacy quirk),
    so we check both status AND body."""
    if not url:
        return {"ok": False, "error": "URL is required"}
    base = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SEC) as c:
            r = await c.post(
                f"{base}/api/v2/auth/login",
                data={"username": user, "password": password},
                headers={"Referer": base},  # qBT WebUI requires same-origin Referer
            )
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"HTTP error: {e}"}
    body = (r.text or "").strip()
    if r.status_code == 200 and body == "Ok.":
        return {"ok": True, "error": None}
    if r.status_code == 200 and body == "Fails.":
        return {"ok": False, "error": "credentials rejected (qBT returned 'Fails.') — check QBT_USER / QBT_PASS"}
    if r.status_code == 403:
        return {"ok": False, "error": "qBT 403 — too many bad attempts; restart qBT or wait and retry"}
    return {"ok": False, "error": f"unexpected response HTTP {r.status_code}: {body[:200]}"}


async def probe_jellyfin(url: str, api_key: str) -> dict:
    """GET /System/Info with the API key. Returns Server / Version on success
    so the UI can surface "Jellyfin 10.10.x" as confirmation."""
    if not url:
        return {"ok": False, "error": "URL is required"}
    if not api_key:
        return {"ok": False, "error": "API key is required (Dashboard -> API Keys)"}
    base = url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SEC) as c:
            r = await c.get(
                f"{base}/System/Info",
                headers={"X-Emby-Token": api_key},
            )
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"HTTP error: {e}"}
    if r.status_code == 200:
        try:
            info = r.json()
            return {
                "ok": True, "error": None,
                "server_name": info.get("ServerName"),
                "version": info.get("Version"),
            }
        except Exception as e:
            return {"ok": False, "error": f"200 but body wasn't JSON: {e}"}
    if r.status_code == 401:
        return {"ok": False, "error": "API key rejected (401) — regenerate via Dashboard -> API Keys"}
    return {"ok": False, "error": f"unexpected HTTP {r.status_code}: {r.text[:200]}"}


# ---------------------------------------------------------------------------
# Background install
# ---------------------------------------------------------------------------

def _setup_script_path(name: str) -> Path:
    """Return the path to ``<name>.ps1`` (e.g. ``setup-mdcx`` /
    ``setup-moviepilot``).

    On the .exe-installer install layout the scripts sit at
    ``{install-dir}\\<name>.ps1``. On dev / scp installs we ship
    them under ``build/`` — both checked here so the wizard works
    in either case.
    """
    fname = f"{name}.ps1"
    candidates = [
        Path(".") / fname,
        Path(__file__).parent.parent / fname,
        Path(__file__).parent.parent / "build" / fname,
    ]
    for p in candidates:
        if p.is_file():
            return p.resolve()
    raise FileNotFoundError(
        f"{fname} not found in any of: {[str(p) for p in candidates]}"
    )


# Whitelist of scripts the wizard is allowed to spawn — guards against any
# accidental endpoint that takes a script name from user input.
_ALLOWED_SCRIPTS: frozenset[str] = frozenset({"setup-mdcx", "setup-moviepilot"})


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


async def start_install(script: str = "setup-mdcx") -> dict:
    """Spawn one of the bundled setup PS1 scripts in the background and
    stream its output into the in-memory log buffer. Returns immediately;
    the caller polls ``GET /api/setup/install/log`` for progress.

    ``script`` must be one of ``_ALLOWED_SCRIPTS`` — any other value is
    rejected, so even though endpoints currently hard-wire the value,
    we won't accidentally enable a path-traversal exploit later.

    Only one install runs at a time. Concurrent calls get
    ``{ok: False, error: "already running"}``.
    """
    if script not in _ALLOWED_SCRIPTS:
        return {"ok": False, "error": f"unknown setup script {script!r}; allowed: {sorted(_ALLOWED_SCRIPTS)}"}

    global _install
    async with _install_lock:
        if _install.running:
            return {"ok": False, "error": "another install is already running"}

        try:
            script_path = _setup_script_path(script)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}

        if sys.platform != "win32":
            return {"ok": False, "error": f"setup wizard only supports Windows (this is {sys.platform})"}

        install_dir = str(Path(__file__).parent.parent.resolve())

        # Per-script extra args. setup-mdcx supports -NoServiceRestart so
        # it doesn't kill mp-relay mid-install; setup-moviepilot is a
        # no-op script (we'd never want to skip its install) and -Silent
        # is the only knob — left as default (interactive) so the user
        # can pick MoviePilot's install dir from its own wizard.
        extra: list[str] = []
        if script == "setup-mdcx":
            extra = ["-NoServiceRestart"]

        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile",
                "-File", str(script_path),
                "-InstallDir", install_dir,
                *extra,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            log.exception("failed to spawn %s", script_path)
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
