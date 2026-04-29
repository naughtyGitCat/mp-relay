"""Tests for the multi-source jav_search refactor — JavBus / JavDB / MissAV
parsers + concurrent merge & dedupe."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# JavBus AJAX endpoint response (parsed by _parse_javbus_magnet_html)
# ---------------------------------------------------------------------------

_JAVBUS_AJAX_HTML = """
<table>
<tr>
  <td>
    <a href="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&amp;dn=SSIS-001-4K">SSIS-001 4K高清版</a>
    <br><a class="btn btn-mini-new btn-warning disabled">高清</a>
    <a class="btn btn-mini-new btn-info disabled">中文字幕</a>
  </td>
  <td><a>5.97GB</a></td>
  <td><a>2024-08-05</a></td>
</tr>
<tr>
  <td>
    <a href="magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb">SSIS-001-1080p</a>
    <a class="btn btn-mini-new btn-warning disabled">高清</a>
  </td>
  <td><a>2.1GB</a></td>
  <td><a>2024-07-12</a></td>
</tr>
</table>
"""

_JAVBUS_DETAIL_PAGE = """
<html><body>
<script type="text/javascript">
var gid = 51074267490;
var uc = 0;
var img = '/pics/cover/939l_b.jpg';
var lang = 'zh';
</script>
</body></html>
"""


def test_extract_javbus_ajax_vars():
    """gid / img / uc come from the page; lang is global constant; floor is
    generated client-side and injected by the caller, not scraped here."""
    from app.jav_search import _extract_javbus_ajax_vars
    vars_ = _extract_javbus_ajax_vars(_JAVBUS_DETAIL_PAGE)
    assert vars_ == {"gid": "51074267490", "img": "/pics/cover/939l_b.jpg", "uc": "0"}


def test_extract_javbus_ajax_vars_missing():
    from app.jav_search import _extract_javbus_ajax_vars
    assert _extract_javbus_ajax_vars("<html>no js</html>") is None


def test_parse_javbus_magnet_html():
    from app.jav_search import _parse_javbus_magnet_html
    cands = _parse_javbus_magnet_html(_JAVBUS_AJAX_HTML, "https://www.javbus.com/SSIS-001")
    assert len(cands) == 2

    first = cands[0]
    assert first["info_hash"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert first["title"] == "SSIS-001 4K高清版"
    assert first["size_str"] == "5.97GB"
    assert first["pub_date"] == "2024-08-05"
    assert first["source"] == "javbus"
    assert first["has_chinese_subs"] is True            # picked up from tag pill
    assert first["quality_score"] == 4                  # 4K detected
    assert first["view_url"] == "https://www.javbus.com/SSIS-001"

    assert cands[1]["info_hash"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert cands[1]["has_chinese_subs"] is False        # only "高清" tag, no CN subs


def test_parse_javbus_magnet_html_skips_invalid_hash():
    from app.jav_search import _parse_javbus_magnet_html
    bad = """
    <table><tr>
      <td><a href="magnet:?xt=urn:btih:tooshort">x</a></td><td>1GB</td><td>x</td>
    </tr></table>
    """
    assert _parse_javbus_magnet_html(bad, "x") == []


# ---------------------------------------------------------------------------
# JavDB
# ---------------------------------------------------------------------------

_JAVDB_SEARCH_HTML = """
<html><body>
<div class="movie-list">
  <a class="box" href="/v/abc123" title="...">
    <div class="video-title"><strong>SSIS-001</strong> Some long title</div>
  </a>
  <a class="box" href="/v/wrong456" title="...">
    <div class="video-title"><strong>OTHER-999</strong> not the one</div>
  </a>
</div>
</body></html>
"""

_JAVDB_DETAIL_HTML = """
<html><body>
<div id="magnets-content">
  <div class="item">
    <a href="magnet:?xt=urn:btih:cccccccccccccccccccccccccccccccccccccccc">SSIS-001-FHD-CHS.mkv</a>
    <span class="name">SSIS-001-FHD-CHS.mkv</span>
    <span class="meta">3.42GB, 2024-08-15</span>
    <div class="tags"><span>高清</span><span>中文字幕</span></div>
  </div>
  <div class="item">
    <a href="magnet:?xt=urn:btih:dddddddddddddddddddddddddddddddddddddddd">SSIS-001-720p.mp4</a>
    <span class="name">SSIS-001-720p.mp4</span>
    <span class="meta">1.5GB, 2024-08-01</span>
  </div>
</div>
</body></html>
"""


def test_parse_javdb_search_finds_matching():
    from app.jav_search import _parse_javdb_search
    url = _parse_javdb_search(_JAVDB_SEARCH_HTML, "SSIS-001", "https://javdb.com")
    assert url == "https://javdb.com/v/abc123"


def test_parse_javdb_search_no_match():
    from app.jav_search import _parse_javdb_search
    url = _parse_javdb_search(_JAVDB_SEARCH_HTML, "ZZZZZ-999", "https://javdb.com")
    assert url is None


def test_parse_javdb_magnets():
    from app.jav_search import _parse_javdb_magnets
    cands = _parse_javdb_magnets(_JAVDB_DETAIL_HTML, "https://javdb.com/v/abc123")
    assert len(cands) == 2
    assert cands[0]["info_hash"] == "cccccccccccccccccccccccccccccccccccccccc"
    assert cands[0]["size_str"] == "3.42GB"
    assert cands[0]["has_chinese_subs"] is True
    assert cands[0]["quality_score"] == 3              # FHD in title
    assert cands[0]["source"] == "javdb"

    assert cands[1]["info_hash"] == "dddddddddddddddddddddddddddddddddddddddd"
    assert cands[1]["quality_score"] == 2              # 720p
    assert cands[1]["has_chinese_subs"] is False


# ---------------------------------------------------------------------------
# MissAV (best-effort generic magnet finder)
# ---------------------------------------------------------------------------

_MISSAV_PAGE_WITH_MAGNET = """
<html><body>
<div class="player">...</div>
<div class="downloads">
  <span>Size: 4.2GB</span>
  <a href="magnet:?xt=urn:btih:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee">Download torrent</a>
</div>
</body></html>
"""

_MISSAV_PAGE_NO_MAGNET = """
<html><body>
<div class="player">streaming only</div>
</body></html>
"""


def test_parse_missav_magnets_extracts():
    from app.jav_search import _parse_missav_magnets
    cands = _parse_missav_magnets(_MISSAV_PAGE_WITH_MAGNET, "https://missav.com/ssis-001")
    assert len(cands) == 1
    assert cands[0]["info_hash"] == "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    assert cands[0]["size_str"] == "4.2GB"
    assert cands[0]["source"] == "missav"


def test_parse_missav_magnets_no_magnet():
    from app.jav_search import _parse_missav_magnets
    cands = _parse_missav_magnets(_MISSAV_PAGE_NO_MAGNET, "https://missav.com/ssis-001")
    assert cands == []


def test_parse_missav_magnets_dedupes_by_hash():
    """Same magnet appearing twice on the page should only produce one candidate."""
    from app.jav_search import _parse_missav_magnets
    html = _MISSAV_PAGE_WITH_MAGNET + _MISSAV_PAGE_WITH_MAGNET    # same magnet twice
    cands = _parse_missav_magnets(html, "x")
    assert len(cands) == 1


# ---------------------------------------------------------------------------
# Multi-source fanout: dedup + ranking + enabled-source filter
# ---------------------------------------------------------------------------

def test_merge_dedupe_first_source_wins():
    from app.jav_search import _merge_dedupe
    sukebei = [{"info_hash": "aaa", "source": "sukebei", "seeders": 50, "title": "via-sukebei"}]
    javbus = [{"info_hash": "aaa", "source": "javbus", "seeders": 0, "title": "via-javbus"},
              {"info_hash": "bbb", "source": "javbus", "seeders": 0, "title": "javbus-only"}]
    merged = _merge_dedupe([sukebei, javbus])
    # First wins: sukebei's "aaa" is kept (with its seeders=50), javbus's is dropped.
    assert len(merged) == 2
    assert merged[0]["info_hash"] == "aaa"
    assert merged[0]["source"] == "sukebei"
    assert merged[0]["seeders"] == 50
    assert merged[1]["info_hash"] == "bbb"
    assert merged[1]["source"] == "javbus"


def test_merge_dedupe_drops_invalid_hash():
    from app.jav_search import _merge_dedupe
    merged = _merge_dedupe([[{"info_hash": "", "source": "x", "title": "y"}]])
    assert merged == []


def test_enabled_sources_default():
    from app.jav_search import _enabled_sources
    from app.config import settings
    # The current default is all four
    assert "sukebei" in _enabled_sources()


def test_enabled_sources_csv_filter(monkeypatch):
    from app import jav_search
    from app.config import settings
    monkeypatch.setattr(settings, "jav_search_sources", "sukebei, javbus")
    assert jav_search._enabled_sources() == ["sukebei", "javbus"]


def test_enabled_sources_unknown_dropped(monkeypatch):
    from app import jav_search
    from app.config import settings
    monkeypatch.setattr(settings, "jav_search_sources", "sukebei,badsource,javdb")
    assert jav_search._enabled_sources() == ["sukebei", "javdb"]


def test_enabled_sources_empty_falls_back_to_sukebei(monkeypatch):
    from app import jav_search
    from app.config import settings
    monkeypatch.setattr(settings, "jav_search_sources", "")
    assert jav_search._enabled_sources() == ["sukebei"]


def test_info_hash_from_magnet():
    from app.jav_search import _info_hash_from_magnet
    assert _info_hash_from_magnet("magnet:?xt=urn:btih:abc123") == "abc123"
    assert _info_hash_from_magnet("magnet:?xt=urn:btih:ABC123") == "abc123"  # lowered
    assert _info_hash_from_magnet("not a magnet") is None


# ---------------------------------------------------------------------------
# Phase 1.7: search_keyword (free-text, sukebei-only, no strict code filter)
# ---------------------------------------------------------------------------

def test_search_keyword_empty_input():
    from app.jav_search import search_keyword
    import asyncio
    assert asyncio.run(search_keyword("")) == []
    assert asyncio.run(search_keyword("   ")) == []


def test_search_keyword_skips_strict_code_filter(monkeypatch):
    """Free-text searches must NOT apply the code-norm-in-title filter that
    search_jav_code uses, otherwise JP titles would get filtered out."""
    import asyncio
    from app import jav_search

    # Mock _fetch_sukebei to return one candidate whose title doesn't contain
    # the keyword as a substring — search_jav_code would reject this, but
    # search_keyword should keep it (sukebei's own search already vetted it).
    fake_results = [{
        "title": "Some Random Title",
        "info_hash": "a" * 40,
        "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
        "seeders": 5,
        "leechers": 0,
        "downloads": 10,
        "size_str": "1.4 GiB",
        "size_mib": 1400.0,
        "quality_score": 3,
        "suspicion_score": 0,
        "has_chinese_subs": False,
        "view_url": "x",
        "pub_date": "x",
        "source": "sukebei",
    }]

    async def fake_fetch(code):
        return fake_results

    monkeypatch.setattr(jav_search, "_fetch_sukebei", fake_fetch)
    out = asyncio.run(jav_search.search_keyword("漆黒のシャガ"))
    # No filter applied → the candidate survives even though title doesn't match
    assert len(out) == 1
    assert out[0]["info_hash"] == "a" * 40


def test_search_keyword_ranks_candidates(monkeypatch):
    """Candidates should still be sorted by the standard rank order."""
    import asyncio
    from app import jav_search

    async def fake_fetch(code):
        return [
            _make_stub_candidate("a", quality=2, seeders=100),   # HD high seeds
            _make_stub_candidate("b", quality=4, seeders=5),     # 4K low seeds
            _make_stub_candidate("c", quality=3, seeders=50),    # FHD mid
        ]

    monkeypatch.setattr(jav_search, "_fetch_sukebei", fake_fetch)
    out = asyncio.run(jav_search.search_keyword("anything"))
    # Quality wins over seeders → 4K(b) > FHD(c) > HD(a)
    assert [c["info_hash"] for c in out] == ["b" * 40, "c" * 40, "a" * 40]


def _make_stub_candidate(letter: str, *, quality: int, seeders: int) -> dict:
    return {
        "title": f"Title-{letter}",
        "info_hash": letter * 40,
        "magnet": f"magnet:?xt=urn:btih:{letter * 40}",
        "seeders": seeders,
        "leechers": 0,
        "downloads": 0,
        "size_str": "1 GiB",
        "size_mib": 1024.0,
        "quality_score": quality,
        "suspicion_score": 0,
        "has_chinese_subs": False,
        "view_url": "",
        "pub_date": "",
        "source": "sukebei",
    }
