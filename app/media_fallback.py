"""Phase 1.5 / 1.6 — alternate-title fallback for media-name search.

Problem: when a user types a name TMDB doesn't index (Chinese fan-translation,
niche OVA, etc.), MoviePilot returns zero candidates and the user is stuck.

Approach: query free metadata APIs that DO index the work, extract every
title variant they know (English / romaji / native / synonyms / Chinese fan-
translation), and re-run MP's search with each. Often one of the alternates
hits TMDB.

Two sources, queried concurrently per call:
- **AniList** — strong for mainstream anime & manga. Tracks official titles +
  upstream synonyms but NOT Chinese fan-translations.
- **Bangumi (bgm.tv)** — Chinese-language community DB. Tracks Chinese fan-
  translations alongside Japanese names (`name_cn` <-> `name`). Fills the gap
  AniList leaves on the "Chinese fan-translation → original" direction.

Scope notes:
- We never touch TMDB directly here — MP owns that integration. We just feed
  it better search terms.
- For 18+ OVA / niche works that genuinely aren't on TMDB at all, even after
  resolving the original title MP will still return zero. The orchestrator
  (``_handle_media_name``) surfaces the Bangumi/AniList match in the response
  body so the user gets ``Bangumi #210268: 漆黒のシャガ THE ANIMATION`` plus
  a link, even when the subscribe path is dead-end.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx

from .config import settings
from . import bangumi

log = logging.getLogger(__name__)


_ANILIST_URL: str = "https://graphql.anilist.co"

# GraphQL query — return up to 3 best matches per search to allow disambiguation.
_ANILIST_QUERY: str = """
query ($search: String!) {
  Page(perPage: 3) {
    media(search: $search) {
      id
      type
      format
      seasonYear
      title { romaji english native }
      synonyms
    }
  }
}
""".strip()


def _make_client() -> httpx.AsyncClient:
    kw: dict = dict(timeout=20.0)
    if settings.discover_proxy:
        kw["proxy"] = settings.discover_proxy
    return httpx.AsyncClient(**kw)


async def alternate_titles_anilist(query: str, *,
                                    limit: int = 5) -> list[dict]:
    """Resolve ``query`` via AniList → list of alternate titles to retry.

    Each entry: ``{"title": str, "via": "anilist:<field>", "media_id": int|None,
                   "type": str|None, "year": int|None}``

    Field is one of ``english`` / ``romaji`` / ``native`` / ``synonyms`` so the
    caller can prioritize (e.g. English titles are most likely to hit TMDB).

    Returns at most ``limit`` entries. Empty list on error / no matches.
    """
    if not query.strip():
        return []

    payload = {"query": _ANILIST_QUERY, "variables": {"search": query}}
    try:
        async with _make_client() as c:
            r = await c.post(_ANILIST_URL, json=payload)
    except httpx.HTTPError as e:
        log.warning("anilist search failed: %s", e)
        return []
    if r.status_code != 200:
        log.warning("anilist HTTP %s: %s", r.status_code, r.text[:200])
        return []
    try:
        data = r.json()
    except ValueError:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    media_list = ((data.get("data") or {}).get("Page") or {}).get("media") or []

    for media in media_list:
        media_id = media.get("id")
        media_type = media.get("type")
        year = media.get("seasonYear")
        title = media.get("title") or {}

        # Priority: english > romaji > native > synonyms
        for field in ("english", "romaji", "native"):
            t = (title.get(field) or "").strip()
            if t and t.lower() not in seen and t.lower() != query.strip().lower():
                seen.add(t.lower())
                out.append({
                    "title": t,
                    "via": f"anilist:{field}",
                    "media_id": media_id,
                    "type": media_type,
                    "year": year,
                })
                if len(out) >= limit:
                    return out

        for syn in media.get("synonyms") or []:
            s = (syn or "").strip()
            if s and s.lower() not in seen and s.lower() != query.strip().lower():
                seen.add(s.lower())
                out.append({
                    "title": s,
                    "via": "anilist:synonym",
                    "media_id": media_id,
                    "type": media_type,
                    "year": year,
                })
                if len(out) >= limit:
                    return out

    return out


async def healthcheck() -> Optional[str]:
    """Probe AniList. Returns None on success, error string otherwise."""
    try:
        results = await alternate_titles_anilist("test", limit=1)
    except Exception as e:
        return f"anilist probe raised: {e}"
    # We don't care that "test" finds anything — we just want the request to work.
    if results is None:
        return "anilist returned None"
    return None


# ---------------------------------------------------------------------------
# Orchestrator: run AniList + Bangumi concurrently, merge alternates
# ---------------------------------------------------------------------------


async def alternate_titles_all(query: str, *, limit: int = 8) -> list[dict]:
    """Query every fallback source concurrently and merge alternate titles.

    Source order on dedup tie-break: AniList first (it's typically more
    canonical for romaji / english), then Bangumi (which contributes the
    Chinese fan-translation coverage AniList misses).

    Each entry retains its source-tagging via ``via`` so the caller can
    surface "via anilist:english" / "via bangumi:name_cn" in UI / logs.
    """
    if not query.strip():
        return []

    coros = [
        alternate_titles_anilist(query, limit=limit),
        bangumi.alternate_titles_bangumi(query, limit=limit),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    merged: list[dict] = []
    seen: set[str] = set()
    q_lower = query.strip().lower()

    for src_result in results:
        if isinstance(src_result, Exception):
            log.warning("alternate_titles source raised: %s", src_result)
            continue
        for item in src_result:
            t_lower = (item.get("title") or "").strip().lower()
            if not t_lower or t_lower == q_lower or t_lower in seen:
                continue
            seen.add(t_lower)
            merged.append(item)
            if len(merged) >= limit:
                return merged

    return merged


async def find_bangumi_match(query: str) -> Optional[dict]:
    """Get the single most relevant Bangumi subject for ``query``.

    Used by the response builder so even when the MP retry comes up empty, we
    can still tell the user "Bangumi found this work — here's the link" with
    the Chinese name + JP name + bgm.tv URL.
    """
    subjects = await bangumi.search_subjects(query, max_results=1)
    if not subjects:
        return None
    s = subjects[0]
    return {
        "id": s.get("id"),
        "name": s.get("name"),
        "name_cn": s.get("name_cn"),
        "url": s.get("url") or f"https://bgm.tv/subject/{s.get('id')}",
        "type": s.get("type"),
    }
