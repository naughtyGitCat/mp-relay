"""Classify a user input string into one of the dispatch buckets.

Buckets:
  jav_magnet   — magnet:?xt=...  whose dn= matches a JAV pattern
  magnet       — magnet:?xt=...  not JAV
  jav_torrent  — http(s)://*.torrent  with JAV in URL/filename
  torrent      — http(s)://*.torrent
  jav_code     — bare JAV code like SSIS-001 (will be rejected for now per user request)
  media_name   — anything else, treated as TMDB-searchable title
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import unquote

Kind = Literal[
    "jav_magnet", "magnet", "jav_torrent", "torrent",
    "jav_code", "id_ref", "media_name",
]

# Patterns that imply Japanese AV content.
# Designed to be specific enough to avoid false positives on regular media titles.
_JAV_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:[A-Z]{2,5})-\d{3,4}\b"),                     # SSIS-001 IPX-123 ABP-456 SSNI-200
    re.compile(r"\bFC2[-_ ]?PPV[-_ ]?\d{6,7}\b", re.I),            # FC2-PPV-1234567
    re.compile(r"\b\d{6,8}[-_]\d{2,5}\b"),                          # 121319_001 (1pondo, 10musume)
    re.compile(r"\b(?:[A-Z]{2,5})-\d{3,4}-[A-Z]\b"),                # MIDV-001-A
    re.compile(r"\bN-?\d{4}\b"),                                    # n1234
    re.compile(r"\bHEYZO[-_ ]?\d{4}\b", re.I),
    re.compile(r"\bCARIB(?:BEANCOM)?\d+(?:[_-]\d+)?\b", re.I),
    re.compile(r"\b(?:T28|TEK|HEY)-?\d{3,4}\b"),
]

_MAGNET_RE = re.compile(r"^magnet:\?", re.I)
_TORRENT_URL_RE = re.compile(r"^https?://.+?\.torrent(?:\?.*)?$", re.I)

# Direct ID references — bypass MP's title search.
# tmdb:NNN / douban:NNN / bangumi:NNN / tt12345 (IMDB) / themoviedb.org URLs
_ID_REF_RES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^tmdb:(\d+)$", re.I), "tmdbid"),
    (re.compile(r"^douban:(\d+)$", re.I), "doubanid"),
    (re.compile(r"^bangumi:(\d+)$", re.I), "bangumiid"),
    (re.compile(r"^(tt\d{6,})$", re.I), "imdbid"),
    (re.compile(r"^https?://(?:www\.)?themoviedb\.org/(movie|tv)/(\d+)", re.I), "tmdb_url"),
    (re.compile(r"^https?://movie\.douban\.com/subject/(\d+)", re.I), "douban_url"),
]
_BARE_CODE_RES = (
    re.compile(r"^[A-Z]{2,5}-?\d{3,4}(?:-[A-Z])?$", re.I),
    re.compile(r"^FC2[-_ ]?PPV[-_ ]?\d{6,7}$", re.I),
    re.compile(r"^\d{6,8}[-_]\d{2,5}$"),
    re.compile(r"^HEYZO[-_ ]?\d{4}$", re.I),
    re.compile(r"^N-?\d{4}$"),
)


def _magnet_dn(magnet: str) -> str:
    m = re.search(r"[?&]dn=([^&]+)", magnet)
    return unquote(m.group(1)) if m else ""


def is_jav_text(s: str) -> bool:
    """JAV codes are conventionally uppercase but users paste whatever (lowercase
    SNOS-073, mixed-case Snos-073). Normalize to upper before matching so the
    patterns don't all need re.I (and so future-added patterns inherit the same
    leniency)."""
    upper = s.upper()
    return any(p.search(upper) for p in _JAV_PATTERNS)


def classify(raw: str) -> tuple[Kind, dict]:
    """Return (kind, hints).

    hints may include:
      name: a plausible display name extracted from the input
    """
    text = raw.strip()
    if not text:
        return "media_name", {}

    if _MAGNET_RE.match(text):
        name = _magnet_dn(text)
        if is_jav_text(name):
            return "jav_magnet", {"name": name or "(unknown)"}
        return "magnet", {"name": name or "(unknown)"}

    if _TORRENT_URL_RE.match(text):
        if is_jav_text(text):
            return "jav_torrent", {"url": text}
        return "torrent", {"url": text}

    # Direct ID reference (skips MP search, queries media detail straight away).
    # Examples: "tmdb:762504", "tt12345678", "https://www.themoviedb.org/movie/762504"
    for pat, ref_kind in _ID_REF_RES:
        m = pat.match(text)
        if m:
            if ref_kind == "tmdb_url":
                return "id_ref", {"id_type": "tmdbid", "id_value": m.group(2),
                                   "media_type": "movie" if m.group(1) == "movie" else "tv"}
            if ref_kind == "douban_url":
                return "id_ref", {"id_type": "doubanid", "id_value": m.group(1)}
            return "id_ref", {"id_type": ref_kind, "id_value": m.group(1)}

    # Bare JAV code without any prefix? E.g. user pastes "SSIS-001" / "FC2-PPV-1234567"
    if any(p.match(text) for p in _BARE_CODE_RES) and is_jav_text(text):
        return "jav_code", {"code": text.upper()}

    # Fallback: treat as TMDB media title (per user's design)
    return "media_name", {"keyword": text}
