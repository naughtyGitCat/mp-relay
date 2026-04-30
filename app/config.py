"""Configuration loaded from .env, with sensible defaults baked from the user's homelab."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- MoviePilot (regular media dispatcher) ---
    mp_url: str = "http://localhost:3000"
    mp_user: str = "admin"
    mp_pass: str = ""           # required at runtime; raise on startup if empty

    # --- qBittorrent WebUI (must be reachable from where mp-relay runs) ---
    qbt_url: str = "http://localhost:8080"
    qbt_user: str = "admin"
    qbt_pass: str = ""          # required at runtime

    # --- JAV path ---
    qbt_jav_category: str = "JAV"
    qbt_jav_savepath: str = r"G:\Downloads\JAV-staging"

    # --- MDCX (the fork at E:\mdcx-src) ---
    mdcx_dir: str = r"E:\mdcx-src"
    mdcx_python: str = r"E:\mdcx-src\.venv\Scripts\python.exe"
    mdcx_module: str = "mdcx.cmd.main"

    # --- Library paths (used by /check existence detection) ---
    # JAV library that mdcx organizes into. Filesystem scan checks for code.
    jav_library: str = r"E:\Jav"
    # Plus the in-flight staging path so we also catch "downloading right now" cases.
    jav_staging_extra: str = r"G:\Downloads\JAV-staging"

    # --- Phase 2 discovery (actor / series / studio lookup) ---
    javbus_base: str = "https://www.javbus.com"
    javdb_base: str = "https://javdb.com"
    missav_base: str = "https://missav.com"
    discover_cache_ttl_sec: int = 24 * 3600     # 24h default
    discover_max_pages: int = 10                  # max paginated pages per actor
    discover_proxy: str = ""                      # override default httpx proxy if needed

    # --- Phase 1 magnet sources ---
    # CSV of magnet sources to query in parallel for each 番号 search.
    # Order doesn't matter (queries run concurrently); same info_hash from
    # multiple sources is deduped (first wins).
    #
    # Default = sukebei + javbus. JavDB and MissAV are Cloudflare-protected
    # and require user-supplied session cookies (see *_cookie below) to work
    # at all; opt in by adding "javdb" / "missav" to this list AND setting
    # the matching cookie string.
    jav_search_sources: str = "sukebei,javbus"
    # Raw Cookie header string (e.g. "_ga=...; cf_clearance=...; ...") copied
    # from a logged-in browser session. Empty disables that source even if
    # listed in jav_search_sources.
    javdb_cookie: str = ""
    missav_cookie: str = ""

    # --- Service ---
    listen_host: str = "0.0.0.0"
    listen_port: int = 5000
    watcher_interval_sec: int = 60
    state_db: str = "state.db"

    # --- Behavior tuning ---
    # Wait this many seconds AFTER a torrent reports completion before triggering mdcx;
    # gives qBT time to finalize file moves and Windows to release file locks.
    mdcx_settle_sec: int = 30

    # Pre-mdcx: merge multi-part releases (CD1+CD2 / Part1+Part2 / A+B+C) into a
    # single file via ffmpeg concat demuxer. Lossless when codecs match; falls
    # back to Jellyfin-friendly multi-file naming if codecs differ.
    merge_multipart: bool = True

    # Pre-mdcx: detect BDMV/VIDEO_TS folders and remux the main playlist into a
    # single .mkv (compatible with Jellyfin/Emby/mdcx, no re-encode).
    remux_disc_archives: bool = True

    # QC retry: on failed quality-check, swap to the next-best candidate up to
    # this many attempts before giving up.
    qc_max_retries: int = 3

    # --- Notifications (Telegram) ---
    # Empty disables; on terminal pipeline events (qc exhausted, scrape failed,
    # first successful merge, etc.) mp-relay sends a short message to this chat.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # If set, only these event kinds are forwarded. Empty = all event kinds.
    telegram_event_filter: str = ""   # comma-separated, e.g. "qc_failed_exhausted,scrape_failed"

    # --- 115 cloud-drive offline download (Phase 1.8 / 1.9) ---
    # Folder ID on 115 where mp-relay-pushed offline tasks land. Empty = use
    # 115's default offline folder (我的接收), but mixing mp-relay content with
    # other offline tasks is messy — recommended to create a dedicated folder
    # on 115 and paste its cid here.
    cloud115_save_dir_id: str = ""
    # Local directory where files synced from 115 are dropped before the
    # post-download pipeline runs. Defaults to the qBT staging path so both
    # sources land in the same place.
    cloud115_local_staging_dir: str = r"G:\Downloads\JAV-staging"
    # How often to poll 115's offline list for completed tasks (seconds).
    cloud115_poll_interval_sec: int = 60
    # Cap pagination when scanning 115 offline list. With 30 tasks/page,
    # default 50 covers the most recent 1500 tasks. If a user has more
    # historical tasks than this, pending ones still in the queue may be
    # missed; raise this if needed.
    cloud115_scan_max_pages: int = 50


settings = Settings()


def validate() -> list[str]:
    """Return a list of misconfiguration messages; empty if OK."""
    issues: list[str] = []
    if not settings.mp_pass:
        issues.append("MP_PASS is empty — set it in .env (the home MoviePilot admin password)")
    if not settings.qbt_pass:
        issues.append("QBT_PASS is empty — set it in .env (the qBittorrent WebUI password)")
    return issues
