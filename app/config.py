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
    discover_cache_ttl_sec: int = 24 * 3600     # 24h default
    discover_max_pages: int = 10                  # max paginated pages per actor
    discover_proxy: str = ""                      # override default httpx proxy if needed

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


settings = Settings()


def validate() -> list[str]:
    """Return a list of misconfiguration messages; empty if OK."""
    issues: list[str] = []
    if not settings.mp_pass:
        issues.append("MP_PASS is empty — set it in .env (the home MoviePilot admin password)")
    if not settings.qbt_pass:
        issues.append("QBT_PASS is empty — set it in .env (the qBittorrent WebUI password)")
    return issues
