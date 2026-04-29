"""qBittorrent WebUI v2 API wrapper. Handles cookie session lifecycle."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from .config import settings

log = logging.getLogger(__name__)


class QbtClient:
    """Minimal async qBT client. Created once, reused; auto-relogins on 403."""

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None or self._client.is_closed:
                c = httpx.AsyncClient(
                    base_url=settings.qbt_url, timeout=30.0,
                    headers={"Referer": settings.qbt_url},
                )
                r = await c.post(
                    "/api/v2/auth/login",
                    data={"username": settings.qbt_user, "password": settings.qbt_pass},
                )
                if r.status_code != 200 or r.text != "Ok.":
                    await c.aclose()
                    raise RuntimeError(f"qBT login failed: {r.status_code} {r.text!r}")
                self._client = c
                log.info("qBT login OK")
            return self._client

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        c = await self._ensure_client()
        r = await c.request(method, path, **kw)
        if r.status_code == 403:  # session expired
            log.info("qBT session expired, re-logging in")
            await self._client.aclose()
            self._client = None
            c = await self._ensure_client()
            r = await c.request(method, path, **kw)
        return r

    # ---- ensure category exists with the right path ----
    async def ensure_category(self, name: str, save_path: str) -> None:
        cats = await self.categories()
        if name not in cats:
            await self._request(
                "POST", "/api/v2/torrents/createCategory",
                data={"category": name, "savePath": save_path},
            )
            log.info("Created qBT category %s -> %s", name, save_path)
        elif (cats[name].get("savePath") or "").rstrip("/\\") != save_path.rstrip("/\\"):
            await self._request(
                "POST", "/api/v2/torrents/editCategory",
                data={"category": name, "savePath": save_path},
            )
            log.info("Updated qBT category %s -> %s", name, save_path)

    async def categories(self) -> dict:
        r = await self._request("GET", "/api/v2/torrents/categories")
        r.raise_for_status()
        return r.json()

    # ---- add torrent ----
    async def add_url(self, url: str, *, category: str = "", save_path: str = "",
                      paused: bool = False) -> str:
        data = {"urls": url}
        if category:
            data["category"] = category
        if save_path:
            data["savepath"] = save_path
        if paused:
            data["paused"] = "true"
        r = await self._request("POST", "/api/v2/torrents/add", data=data)
        r.raise_for_status()
        return r.text  # qBT returns "Ok." on success

    # ---- list/inspect ----
    async def list_torrents(self, *, category: Optional[str] = None) -> list[dict]:
        params = {}
        if category is not None:
            params["category"] = category
        r = await self._request("GET", "/api/v2/torrents/info", params=params)
        r.raise_for_status()
        return r.json()

    async def info(self, torrent_hash: str) -> Optional[dict]:
        torrents = await self.list_torrents()
        for t in torrents:
            if t.get("hash") == torrent_hash:
                return t
        return None

    async def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        """Remove a torrent from qBT. Used by retry chain when QC fails."""
        r = await self._request(
            "POST", "/api/v2/torrents/delete",
            data={
                "hashes": torrent_hash,
                "deleteFiles": "true" if delete_files else "false",
            },
        )
        r.raise_for_status()
        log.info("qBT delete %s (delete_files=%s)", torrent_hash[:8], delete_files)

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
