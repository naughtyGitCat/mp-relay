"""Check whether a piece of media already exists locally.

Two paths:
  - JAV (by 番号): walk the JAV library tree on disk
  - Regular media: query MoviePilot for transfer history / existing subscription
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from .config import settings
from .mp_client import MpClient

log = logging.getLogger(__name__)


# Capture a JAV code from a free-form string (file name, magnet dn, or bare).
_JAV_CODE_RES: list[re.Pattern] = [
    re.compile(r"\b(FC2[-_ ]?PPV[-_ ]?\d{6,7})\b", re.I),
    re.compile(r"\b(HEYZO[-_ ]?\d{4})\b", re.I),
    re.compile(r"\b(\d{6,8}[-_]\d{2,5})\b"),
    re.compile(r"\b([A-Z]{2,5}-\d{3,4}(?:-[A-Z])?)\b"),
    re.compile(r"\b([A-Z]{2,5}\d{3,4})\b"),
]


def extract_code(text: str) -> Optional[str]:
    """Return the canonical JAV code (UPPER, dashed) from any string."""
    for pat in _JAV_CODE_RES:
        m = pat.search(text)
        if m:
            raw = m.group(1).upper()
            # Normalise: collapse any internal whitespace/underscores to single dash where appropriate.
            # FC2/HEYZO/numeric series keep their natural separators; lettered codes get dashed.
            if raw.startswith("FC2"):
                # Force "FC2-PPV-1234567" — match the FC2 movie id (6+ digits)
                m2 = re.search(r"\d{6,}", raw)
                if m2:
                    return f"FC2-PPV-{m2.group()}"
            if raw.startswith("HEYZO"):
                m2 = re.search(r"\d{4,}", raw)
                if m2:
                    return f"HEYZO-{m2.group()}"
            return raw  # "SSIS-001" or "SSIS001"
    return None


def _normalise(s: str) -> str:
    """Strip dashes/underscores/spaces and uppercase, for fuzzy matching."""
    return re.sub(r"[\s_\-\.]+", "", s).upper()


def check_jav_code(code: str) -> list[dict]:
    """Scan settings.jav_library + jav_staging_extra for any folder containing the code.

    Walks up to 3 levels deep; mdcx's organisation is typically
    base/<actor>/<code> <title>/ but flat layouts also exist.
    """
    code_norm = _normalise(code)
    matches: list[dict] = []
    seen: set[str] = set()  # dedupe by absolute path

    roots = [settings.jav_library, settings.jav_staging_extra]
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            entries = list(base.iterdir())
        except (PermissionError, OSError) as e:
            log.warning("scan failed at %s: %s", root, e)
            continue

        for level1 in entries:
            if not level1.is_dir():
                continue
            if code_norm in _normalise(level1.name):
                key = str(level1)
                if key not in seen:
                    seen.add(key)
                    matches.append({
                        "path": key,
                        "name": level1.name,
                        "depth": 1,
                        "in": "library" if root == settings.jav_library else "staging",
                    })
                continue

            # Walk one more level: <actor>/<code> <title>/
            try:
                for level2 in level1.iterdir():
                    if not level2.is_dir():
                        continue
                    if code_norm in _normalise(level2.name):
                        key = str(level2)
                        if key not in seen:
                            seen.add(key)
                            matches.append({
                                "path": key,
                                "name": level2.name,
                                "depth": 2,
                                "parent": level1.name,
                                "in": "library" if root == settings.jav_library else "staging",
                            })
            except (PermissionError, OSError):
                pass

    return matches


async def check_media_name(keyword: str, mp: Optional[MpClient] = None) -> dict:
    """Check whether a regular media title is already in MP / library.

    Uses two MP signals:
      - existing subscription (active or processed)
      - download history with the same title

    Returns:
      {"subscriptions": [...], "downloads": [...]}
    """
    mp = mp or MpClient()

    # Search subscriptions (the user might have already subscribed).
    subs_resp = await mp.request("GET", "/api/v1/subscribe/")
    subs: list[dict] = []
    if subs_resp.status_code == 200:
        all_subs = subs_resp.json() or []
        kw_norm = _normalise(keyword)
        for s in all_subs:
            name = s.get("name") or ""
            if kw_norm and (kw_norm in _normalise(name) or _normalise(name) in kw_norm):
                subs.append({
                    "id": s.get("id"),
                    "name": name,
                    "year": s.get("year"),
                    "type": s.get("type"),
                    "tmdbid": s.get("tmdbid"),
                    "state": s.get("state"),
                    "season": s.get("season"),
                })

    # Search download history (already pulled the same title).
    # Scan first 5 pages × 50 items so we cover ~250 most recent torrents.
    downloads: list[dict] = []
    seen_keys: set[tuple] = set()
    kw_norm = _normalise(keyword)
    for page in range(1, 6):
        hist_resp = await mp.request(
            "GET", "/api/v1/history/download", params={"page": page, "count": 50}
        )
        if hist_resp.status_code != 200:
            break
        hist = hist_resp.json() or []
        if not hist:
            break
        for h in hist:
            title = h.get("title") or ""
            if not title:
                continue
            t_norm = _normalise(title)
            if kw_norm and (kw_norm in t_norm or t_norm in kw_norm):
                key = (title, h.get("date"), h.get("tmdbid"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                downloads.append({
                    "title": title,
                    "type": h.get("type"),
                    "date": h.get("date"),
                    "tmdbid": h.get("tmdbid"),
                    "torrent_name": h.get("torrent_name"),
                })

    return {"subscriptions": subs, "downloads": downloads}


async def check_input(text: str, kind: str, hints: dict) -> dict:
    """Top-level: dispatch based on classifier kind. Returns {existing_jav, existing_media}."""
    result: dict = {"existing_jav": [], "existing_media": {}}

    if kind in ("jav_magnet", "jav_torrent", "jav_code"):
        # Try to find a code from hints first, then from the raw text.
        candidate = hints.get("code") or hints.get("name") or text
        code = extract_code(candidate)
        if code:
            result["jav_code"] = code
            result["existing_jav"] = check_jav_code(code)
        return result

    if kind in ("magnet", "torrent"):
        # We may not be able to identify "existing" until MP recognises the magnet.
        # Best-effort: pull the dn= and try the regular media check using that as keyword.
        keyword = hints.get("name") or ""
        if keyword:
            result["existing_media"] = await check_media_name(keyword)
        return result

    if kind == "media_name":
        keyword = hints.get("keyword") or text
        result["existing_media"] = await check_media_name(keyword)
        return result

    return result
