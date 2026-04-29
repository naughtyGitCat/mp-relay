"""Phase 1.5 — alternate-title fallback for media-name search.

Problem: when a user types a Chinese fan-translation that TMDB doesn't index
(e.g. "漆黑的射干" — TMDB only has "乌鸦不择主"), MoviePilot returns zero
candidates and the user is stuck.

Approach: query AniList's free GraphQL API for the title. AniList stores every
known title variant (romaji / english / native / synonyms) per work, so a
Chinese fan-translation often resolves to a single Media node from which we
can extract the canonical title, English title, romaji, and synonyms — all of
which are much more likely to hit TMDB.

The caller (``_handle_media_name``) re-runs the MoviePilot search for each
alternate title and merges any new candidates.

Scope notes:
- AniList covers anime + manga only. Live-action Chinese-to-original lookups
  would need Douban scraping or similar; out of scope for this iteration.
- We never touch TMDB directly here — MP owns that integration. We just feed
  it better search terms.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .config import settings

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
