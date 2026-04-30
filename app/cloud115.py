"""Phase 1.8 — 115 cloud-drive offline-download integration via official OAuth.

Flow:
1. User visits /auth/115 once → mp-relay calls 115's authDeviceCode → gets a
   uid + qrcode URL. Page displays the QR (rendered via qr-server.com) and
   polls scan_status every 3s.
2. User scans with their 115 mobile / desktop app → status flips to 2
   (logged in). mp-relay exchanges the device code for access_token +
   refresh_token, persists both to SQLite (cloud115_token table, single row).
3. From then on, every magnet candidate row in the UI has a "☁️ 推 115 离线"
   button alongside the "加入 qBT" button. Click → POST /api/cloud115-add →
   the magnet lands in 115's offline queue (cloud-side download).

Implementation uses ``p115client.P115OpenClient`` which already wraps every
endpoint on ``proapi.115.com`` plus the device-code flow on ``qrcodeapi``.
We use the public app_id ``100195125`` (the same one p115client defaults to),
so the user does NOT need to register a developer app on open.115.com.

Token rotation: on `40140116` (expired access_token), automatically call
refresh_token, persist new pair, retry once. Refresh tokens themselves are
IP-pinned but rotate on use (old one invalidates).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from p115client import P115OpenClient

from .config import settings

log = logging.getLogger(__name__)

# Public app_id baked into p115client. Avoids forcing the user to register
# their own developer app on open.115.com. Limit: same app_id supports max 2
# concurrent active sessions.
_APP_ID: int = 100195125

# In-memory state for in-flight device-code sessions (keyed by uid).
# Holds the PKCE code_verifier we generated when start_auth() ran. Single
# process so a dict is fine; if we ever multi-process this, move to SQLite.
_pending_auth: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Token storage (single-row SQLite table)
# ---------------------------------------------------------------------------

def init_token_table() -> None:
    """Create cloud115_token if needed. Idempotent — safe to call on startup."""
    with sqlite3.connect(settings.state_db) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cloud115_token (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                access_token  TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            )
        """)


def load_tokens() -> Optional[tuple[str, str]]:
    """Return (access_token, refresh_token) or None if not yet authorized."""
    with sqlite3.connect(settings.state_db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT access_token, refresh_token FROM cloud115_token WHERE id = 1"
        ).fetchone()
    return (row["access_token"], row["refresh_token"]) if row else None


def save_tokens(access_token: str, refresh_token: str, expires_in: int = 7200) -> None:
    """Upsert the single token row."""
    expires_at = time.time() + int(expires_in)
    now = time.time()
    with sqlite3.connect(settings.state_db) as c:
        c.execute(
            """INSERT INTO cloud115_token (id, access_token, refresh_token, expires_at, updated_at)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 access_token  = excluded.access_token,
                 refresh_token = excluded.refresh_token,
                 expires_at    = excluded.expires_at,
                 updated_at    = excluded.updated_at""",
            (access_token, refresh_token, expires_at, now),
        )


def clear_tokens() -> None:
    """Forget current authorization (forces re-auth)."""
    with sqlite3.connect(settings.state_db) as c:
        c.execute("DELETE FROM cloud115_token WHERE id = 1")


def is_authorized() -> bool:
    return load_tokens() is not None


# ---------------------------------------------------------------------------
# Device-code (QR) auth flow
# ---------------------------------------------------------------------------

def _gen_code_verifier() -> str:
    """PKCE: 43-128 char URL-safe random string. RFC 7636."""
    return secrets.token_urlsafe(64)


def _gen_code_challenge(verifier: str) -> str:
    """Standard base64 of SHA-256(verifier). 115 docs specify base64, not base64url."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _unwrap(resp: dict) -> dict:
    """Most 115 open-API responses wrap the payload under .data; some endpoints
    return flat. Return the payload regardless of shape."""
    if isinstance(resp, dict) and "data" in resp and isinstance(resp["data"], dict):
        return resp["data"]
    return resp or {}


async def start_auth() -> dict:
    """Begin a device-code flow.

    Returns a dict carrying the fields the UI needs to render a QR:
      - uid:        opaque session id
      - time, sign: needed when polling scan status
      - qrcode:     URL/string the 115 client will encode in its scanner
                    (we wrap it in a public QR-image service for display)

    Side effect: registers the verifier in _pending_auth keyed by uid.
    """
    code_verifier = _gen_code_verifier()
    payload = {
        "client_id": _APP_ID,
        "code_challenge": _gen_code_challenge(code_verifier),
        "code_challenge_method": "sha256",
    }
    resp = await P115OpenClient.login_qrcode_token_open(payload, async_=True)
    data = _unwrap(resp)
    uid = str(data.get("uid") or "")
    if not uid:
        raise RuntimeError(f"115 authDeviceCode returned no uid: {resp}")
    _pending_auth[uid] = code_verifier
    return {
        "uid": uid,
        "time": data.get("time"),
        "sign": data.get("sign"),
        "qrcode": data.get("qrcode") or "",
    }


async def poll_auth(uid: str, time_: Any, sign: str) -> dict:
    """Poll the scan-status endpoint.

    115 returns one of:
      status =  0  未扫描
      status =  1  已扫描，待确认
      status =  2  已确认登录       ← we then exchange tokens
      status = -1  已取消
      status = -2  已过期

    On status == 2, we exchange code_verifier for tokens and persist them.
    """
    payload = {"uid": uid, "time": time_, "sign": sign}
    resp = await P115OpenClient.login_qrcode_scan_status(payload, async_=True)
    data = _unwrap(resp)
    status = int(data.get("status", 0))
    msg = data.get("msg", "") or data.get("message", "")

    if status != 2:
        return {"status": status, "msg": msg, "authorized": False}

    verifier = _pending_auth.pop(uid, None)
    if not verifier:
        return {
            "status": status,
            "msg": "code_verifier 丢失（服务端可能重启过），请刷新页面重新授权",
            "authorized": False,
        }

    token_resp = await P115OpenClient.login_qrcode_access_token_open(
        {"uid": uid, "code_verifier": verifier}, async_=True,
    )
    td = _unwrap(token_resp)
    at = td.get("access_token")
    rt = td.get("refresh_token")
    if not at or not rt:
        return {
            "status": status,
            "msg": f"token exchange 失败: {token_resp}",
            "authorized": False,
        }
    save_tokens(at, rt, expires_in=int(td.get("expires_in") or 7200))
    log.info("cloud115 OAuth: authorization saved (uid=%s)", uid[:8])
    return {"status": status, "msg": "授权成功", "authorized": True}


# ---------------------------------------------------------------------------
# Offline ops with auto token-refresh
# ---------------------------------------------------------------------------

def _client() -> Optional[P115OpenClient]:
    tokens = load_tokens()
    if not tokens:
        return None
    return P115OpenClient.from_token(*tokens)


async def _refresh_now(client: P115OpenClient) -> P115OpenClient:
    """Use the current refresh_token to mint a fresh pair, persist, return new client."""
    resp = await P115OpenClient.login_refresh_token_open(
        {"refresh_token": client.refresh_token}, async_=True,
    )
    td = _unwrap(resp)
    at = td.get("access_token")
    new_rt = td.get("refresh_token")
    if not at or not new_rt:
        raise RuntimeError(f"cloud115 token refresh failed: {resp}")
    save_tokens(at, new_rt, expires_in=int(td.get("expires_in") or 7200))
    log.info("cloud115 token refreshed")
    return P115OpenClient.from_token(at, new_rt)


_TOKEN_EXPIRED_MARKERS: tuple[str, ...] = ("40140116", "40140117", "401 ", "expired")


async def _call(method_name: str, *args, **kwargs) -> dict:
    """Invoke a P115OpenClient method, refreshing tokens once on expiry."""
    client = _client()
    if client is None:
        raise RuntimeError("115 未授权 — 请先访问 /auth/115 完成扫码授权")
    method = getattr(client, method_name)
    try:
        return await method(*args, async_=True, **kwargs)
    except Exception as e:
        msg = str(e)
        if not any(marker in msg for marker in _TOKEN_EXPIRED_MARKERS):
            raise
        log.info("cloud115 access_token expired (%s), refreshing", msg[:80])
        client = await _refresh_now(client)
        method = getattr(client, method_name)
        return await method(*args, async_=True, **kwargs)


async def add_offline_url(magnet: str, save_dir_id: str = "") -> dict:
    """Push a single magnet/HTTP/ed2k URL into 115's offline queue.

    Returns the raw API response. Useful fields in ``.data``:
      - state: bool (True on success)
      - data: list of task dicts with {info_hash, name, size, ...}

    ``save_dir_id`` is the 115 folder ID where the completed file will land.
    If empty, falls back to ``settings.cloud115_save_dir_id``; if that's also
    empty, 115 uses its default offline folder (云下载 / 我的接收).
    """
    payload: dict[str, Any] = {"urls": magnet}
    target = save_dir_id or settings.cloud115_save_dir_id
    if target:
        payload["wp_path_id"] = target
    return await _call("offline_add_urls_open", payload)


async def list_offline(page: int = 1) -> dict:
    """List current offline tasks (paginated, 30 per page)."""
    return await _call("offline_list_open", page)


async def quota_info() -> dict:
    """Get remaining offline quota (per-day add limit + total/used)."""
    return await _call("offline_quota_info_open")


async def healthcheck() -> Optional[str]:
    """Return None if cloud115 is reachable + token alive, else error string."""
    if not is_authorized():
        return "unauthorized — open /auth/115 to scan QR"
    try:
        resp = await quota_info()
        if isinstance(resp, dict) and resp.get("state") is False:
            return f"quota probe rejected: {resp.get('message') or resp.get('error') or resp}"
        return None
    except Exception as e:
        return f"cloud115 probe error: {e}"


# ---------------------------------------------------------------------------
# Cloud → local sync (Phase 1.9)
#
# Once an offline task hits status=2 on 115, the file (or folder) lives in
# the user's 115 drive. To run mdcx and surface it in Jellyfin we have to
# stream it back to local disk. The flow is:
#
#   1. fs_files_open(file_id)          → list folder contents (most magnets
#                                        resolve to a folder; if the file_id
#                                        IS a single file the list is empty)
#   2. download_url_info_open(pickcode) → signed CDN URL + metadata
#   3. httpx.stream("GET", url)         → write to local path in 1 MiB chunks
#
# 115's CDN URLs are short-lived (~minutes); fetch one immediately before
# starting the actual download. Resumable downloads are out of scope for v1
# — on partial failure we delete the bad file and let the watcher retry.
# ---------------------------------------------------------------------------

# Chunk size for streaming downloads. 1 MiB is a reasonable balance — too
# small and we burn CPU on syscalls, too large and we hold memory + delay
# disk flush.
_CHUNK_SIZE: int = 1024 * 1024

# 115's CDN signs download URLs against the User-Agent that requested them.
# If we generate the URL with one UA and fetch with another, the CDN returns
# 403. Pin a single value used for both calls. Browser-style UA works; the
# specific token doesn't matter as long as both sides match.
_DOWNLOAD_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


async def list_folder_contents(folder_id: str | int) -> list[dict]:
    """List children of a 115 folder. Empty list if it's not a folder
    (file_id pointing at a single file) — caller should treat that as a
    'singleton' download case."""
    client = _client()
    if client is None:
        raise RuntimeError("115 未授权")
    try:
        resp = await client.fs_files_open(int(folder_id), async_=True)
    except Exception as e:
        msg = str(e)
        if any(k in msg for k in _TOKEN_EXPIRED_MARKERS):
            client = await _refresh_now(client)
            resp = await client.fs_files_open(int(folder_id), async_=True)
        else:
            raise
    if isinstance(resp, dict) and resp.get("state") is False:
        # Often returned when the cid is actually a file, not a folder.
        return []
    return resp.get("data") or []


async def get_download_info(pickcode: str) -> dict:
    """Resolve a pickcode → ``{file_name, file_size, sha1, url}``.

    Passes ``user_agent=_DOWNLOAD_UA`` so the signed URL the CDN issues is
    bound to that UA. ``stream_download`` uses the same UA when fetching.
    """
    client = _client()
    if client is None:
        raise RuntimeError("115 未授权 — 请先访问 /auth/115 完成扫码授权")
    method = client.download_url_info_open
    try:
        resp = await method({"pick_code": pickcode}, user_agent=_DOWNLOAD_UA, async_=True)
    except Exception as e:
        msg = str(e)
        if not any(marker in msg for marker in _TOKEN_EXPIRED_MARKERS):
            raise
        client = await _refresh_now(client)
        method = client.download_url_info_open
        resp = await method({"pick_code": pickcode}, user_agent=_DOWNLOAD_UA, async_=True)

    if not resp.get("state"):
        raise RuntimeError(
            f"download_url_info_open failed: {resp.get('message') or resp}"
        )
    items = resp.get("data") or {}
    if not items:
        raise RuntimeError("download_url_info_open returned empty data")
    # Response keyed by file_id; we just want the single entry.
    first = next(iter(items.values()))
    url_obj = first.get("url") or {}
    return {
        "file_name": first.get("file_name", ""),
        "file_size": int(first.get("file_size") or 0),
        "sha1": first.get("sha1", ""),
        "url": url_obj.get("url", "") if isinstance(url_obj, dict) else str(url_obj),
    }


async def stream_download(pickcode: str, dest: Path) -> int:
    """Stream a single 115 file to ``dest``. Returns bytes written.

    Removes the partial file on any error so the next attempt starts clean.
    Aborts (raises) if the actual size doesn't match what 115 advertised.
    """
    info = await get_download_info(pickcode)
    url = info["url"]
    expected = info["file_size"]
    if not url:
        raise RuntimeError(f"empty download URL for pickcode={pickcode}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        timeout = httpx.Timeout(60.0, read=600.0)
        # follow_redirects: 115 CDN sometimes 302s to a regional edge.
        # User-Agent must match the one used when generating the signed URL —
        # 115's CDN binds the signature to UA and returns 403 otherwise.
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _DOWNLOAD_UA},
        ) as c:
            async with c.stream("GET", url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                        f.write(chunk)
                        written += len(chunk)
    except Exception:
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        raise

    if expected and written != expected:
        try:
            dest.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"size mismatch downloading {dest.name}: wrote {written}, expected {expected}"
        )
    return written


# Files we never want to drag down from 115 to local disk. Forum HTML, .url
# shortcuts, .txt readme spam — same junk classes that cleanup.py would
# delete locally anyway. Saves bandwidth.
_SKIP_EXTS_ON_SYNC: frozenset[str] = frozenset({
    ".url", ".lnk", ".html", ".htm", ".txt", ".md", ".rtf", ".docx", ".doc",
})


async def sync_completed_task(task: dict, dest_root: Path) -> Path:
    """Download a completed 115 offline task to ``dest_root/<name>/``.

    The 115 task dict is what ``offline_list_open`` returns (status=2
    expected). For magnets that resolved to a folder, every file inside is
    pulled (skipping junk extensions). For singleton-file results, the file
    is dropped directly into the target dir.

    Returns the path to the per-task local folder.
    """
    name = task.get("name") or task.get("info_hash") or "unknown"
    file_id = task.get("file_id") or ""
    pick_code = task.get("pick_code") or ""

    if not file_id and not pick_code:
        raise RuntimeError(
            f"task {task.get('info_hash')} has neither file_id nor pick_code"
        )

    dest_dir = dest_root / name
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Probe: is file_id a folder?
    children: list[dict] = []
    if file_id:
        try:
            children = await list_folder_contents(file_id)
        except Exception as e:
            log.warning("list_folder_contents(%s) failed: %s", file_id, e)
            children = []

    if children:
        for child in children:
            # fc: "1" = file, "0"/"" = folder. Skip subfolders for v1; mp-relay
            # rarely needs nesting and the existing watcher pipeline assumes
            # everything's at one level under the task dir.
            if child.get("fc") != "1":
                continue
            cn = child.get("fn") or "unknown"
            if Path(cn).suffix.lower() in _SKIP_EXTS_ON_SYNC:
                log.info("[c115 sync] skip junk: %s", cn)
                continue
            cpc = child.get("pc") or ""
            if not cpc:
                log.warning("[c115 sync] child has no pickcode: %s", child)
                continue
            log.info("[c115 sync] %s/%s", name, cn)
            await stream_download(cpc, dest_dir / cn)
    elif pick_code:
        # Singleton-file task — file_id WAS the file, list returned empty.
        log.info("[c115 sync] singleton %s", name)
        await stream_download(pick_code, dest_dir / name)
    else:
        raise RuntimeError(
            f"task {task.get('info_hash')} folder is empty and no singleton pickcode"
        )

    return dest_dir


async def list_offline_completed_by_hashes(
    info_hashes: set[str], *, max_pages: int = 50,
) -> dict[str, dict]:
    """Scan offline-task pages for tasks matching any hash in the input set.

    Returns ``{info_hash: task_dict}`` for every match found, regardless of
    status. Caller filters by status to find completed (=2) ones.

    Stops scanning early when every requested hash has been located.
    """
    if not info_hashes:
        return {}
    targets = {h.lower() for h in info_hashes}
    found: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        try:
            resp = await list_offline(page=page)
        except Exception as e:
            log.warning("[c115 watch] list_offline(page=%s) failed: %s", page, e)
            break
        tasks = (resp.get("data") or {}).get("tasks") or []
        if not tasks:
            break
        for t in tasks:
            h = (t.get("info_hash") or "").lower()
            if h in targets and h not in found:
                found[h] = t
        if len(found) >= len(targets):
            break
    return found
