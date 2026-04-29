"""MoviePilot home-instance client. Bearer auth with auto-refresh."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from .config import settings

log = logging.getLogger(__name__)


class MpClient:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()

    async def _login(self) -> None:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(
                f"{settings.mp_url}/api/v1/login/access-token",
                data={
                    "username": settings.mp_user,
                    "password": settings.mp_pass,
                    "grant_type": "password",
                },
            )
            r.raise_for_status()
            self._token = r.json()["access_token"]
            log.info("MP login OK")

    async def _ensure_token(self) -> str:
        async with self._lock:
            if not self._token:
                await self._login()
            return self._token  # type: ignore[return-value]

    async def request(self, method: str, path: str, **kw) -> httpx.Response:
        headers = kw.pop("headers", {})
        token = await self._ensure_token()
        headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.request(method, f"{settings.mp_url}{path}", headers=headers, **kw)
            # 401 OR 403-with-token-error: refresh and retry once.
            if r.status_code == 401 or (
                r.status_code == 403 and "token校验不通过" in (r.text or "")
            ):
                self._token = None
                token = await self._ensure_token()
                headers["Authorization"] = f"Bearer {token}"
                r = await c.request(method, f"{settings.mp_url}{path}", headers=headers, **kw)
            return r

    # ---- high-level ops ----
    async def add_download(self, *, title: str, enclosure: str,
                           tmdbid: Optional[int] = None,
                           save_path: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "torrent_in": {"title": title, "enclosure": enclosure},
        }
        if tmdbid is not None:
            body["tmdbid"] = tmdbid
        if save_path:
            body["save_path"] = save_path
        r = await self.request("POST", "/api/v1/download/add", json=body)
        # MP returns 200 even on identification failure — body has success=false
        try:
            return r.json()
        except Exception:
            return {"success": False, "message": r.text, "status": r.status_code}

    async def search_media(self, title: str, type_: str = "") -> list[dict]:
        params = {"title": title}
        if type_:
            params["type"] = type_
        r = await self.request("GET", "/api/v1/media/search", params=params)
        if r.status_code != 200:
            return []
        return r.json() or []

    async def subscribe(self, *, name: str, tmdbid: int, type_: str,
                        season: Optional[int] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "tmdbid": tmdbid, "type": type_}
        if season is not None:
            body["season"] = season
        r = await self.request("POST", "/api/v1/subscribe/", json=body)
        try:
            return r.json()
        except Exception:
            return {"success": False, "message": r.text}
