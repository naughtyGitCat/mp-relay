"""Phase 1.6 — Bangumi (bgm.tv) fallback for media-name searches.

Why this is separate from the AniList fallback:
- AniList covers mainstream anime well but only stores official titles +
  upstream-tracked synonyms. Chinese fan-translations like "漆黑的射干"
  (a kanji-substitution rendering of "漆黒のシャガ") are NOT in AniList.
- Bangumi is a Chinese-language community DB; users routinely register the
  Chinese fan-translation as ``name_cn`` next to the Japanese ``name``. So
  exactly the lookup AniList misses (Chinese-fan-translation → original
  title) is Bangumi's bread and butter.

Realistic expectation: many of the works Bangumi finds are 18+ OVA / niche
titles TMDB doesn't index. So even after we resolve a better title, the
follow-up MoviePilot / TMDB search may still return zero. We surface the
Bangumi match in the response anyway so the user sees ``Bangumi #210268:
漆黒のシャガ THE ANIMATION`` and a link to bgm.tv — they can take it from
there (paste a magnet directly, etc.).

API: documented at https://bangumi.github.io/api/. ``/search/subject/<kw>``
is the legacy endpoint, JSON, no auth, returns id + name + name_cn.
``/v0/search/subjects`` is the modern POST equivalent — we use the legacy
GET because the response shape is simpler and stable.
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_BGM_API: str = "https://api.bgm.tv"

# Bangumi recommends every API caller send a descriptive User-Agent so they
# can contact us if we cause traffic problems. Per their guidelines.
_UA: str = "naughtyGitCat/mp-relay (https://github.com/naughtyGitCat/mp-relay)"

# Subject types: 1=book, 2=anime, 3=music, 4=game, 6=real-life
# Anime + real-life cover the cases we care about (TV / OVA / live-action JP
# drama). Skip music / game / book — out of scope for mp-relay.
_DEFAULT_TYPES: list[int] = [2, 6]

_TIMEOUT_SEC: float = 15.0


async def search_subjects(keyword: str, *, max_results: int = 5,
                          types: Optional[list[int]] = None) -> list[dict]:
    """Search Bangumi for matching subjects. Returns the raw ``list`` array.

    Each entry has at minimum ``id`` / ``name`` (JP) / ``name_cn`` / ``url`` /
    ``type``. Empty list on any error — Bangumi being slow/down should never
    block the broader fallback chain.
    """
    if not keyword.strip():
        return []
    types = types or _DEFAULT_TYPES
    type_csv = ",".join(str(t) for t in types)
    url = f"{_BGM_API}/search/subject/{quote(keyword)}"
    params = {
        "type": type_csv,
        "responseGroup": "small",
        "max_results": str(max_results),
    }
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SEC,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        ) as c:
            r = await c.get(url, params=params)
    except httpx.HTTPError as e:
        log.warning("bangumi search failed: %s", e)
        return []
    if r.status_code != 200:
        log.warning("bangumi → HTTP %s: %s", r.status_code, r.text[:200])
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    # Bangumi returns ``{"results": 0, "code": 404, "error": "..."}`` for misses.
    items = data.get("list") or []
    return [x for x in items if isinstance(x, dict)]


async def alternate_titles_bangumi(query: str, *, limit: int = 5) -> list[dict]:
    """Resolve ``query`` via Bangumi → list of alternate titles to retry MP.

    Each entry: ``{"title": str, "via": "bangumi:<field>", "bangumi_id": int,
                   "url": str, "type": int}``.

    Returns at most ``limit`` entries. Empty list on error / no matches.
    """
    if not query.strip():
        return []

    subjects = await search_subjects(query, max_results=3)

    out: list[dict] = []
    seen: set[str] = set()
    q_lower = query.strip().lower()

    for subj in subjects:
        bgm_id = subj.get("id")
        url = subj.get("url") or (f"https://bgm.tv/subject/{bgm_id}" if bgm_id else "")
        subj_type = subj.get("type")

        # Both the Japanese (``name``) and Chinese (``name_cn``) titles are
        # candidates. Whichever the user didn't type is the useful alt.
        for field in ("name", "name_cn"):
            t = (subj.get(field) or "").strip()
            t_lower = t.lower()
            if not t or t_lower == q_lower or t_lower in seen:
                continue
            seen.add(t_lower)
            out.append({
                "title": t,
                "via": f"bangumi:{field}",
                "bangumi_id": bgm_id,
                "url": url,
                "type": subj_type,
            })
            if len(out) >= limit:
                return out

    return out


async def healthcheck() -> Optional[str]:
    """Return None if Bangumi search is reachable, else error string."""
    try:
        results = await search_subjects("test", max_results=1)
    except Exception as e:
        return f"bangumi probe raised: {e}"
    return None if isinstance(results, list) else "bangumi returned non-list"
