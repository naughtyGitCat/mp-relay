"""Configuration loaded from .env, with sensible defaults baked from the user's homelab."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- MoviePilot (home instance — for non-JAV media) ---
    mp_url: str = "http://10.100.100.13:3000"
    mp_user: str = "admin"
    mp_pass: str = "sa123456"

    # --- qBittorrent WebUI (local on .13) ---
    qbt_url: str = "http://127.0.0.1:8080"
    qbt_user: str = "admin"
    qbt_pass: str = "123456"

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

    # --- Service ---
    listen_host: str = "0.0.0.0"
    listen_port: int = 5000
    watcher_interval_sec: int = 60
    state_db: str = "state.db"

    # --- Behavior tuning ---
    # Wait this many seconds AFTER a torrent reports completion before triggering mdcx;
    # gives qBT time to finalize file moves and Windows to release file locks.
    mdcx_settle_sec: int = 30


settings = Settings()
