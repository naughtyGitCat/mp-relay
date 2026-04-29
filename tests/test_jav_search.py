"""Tests for jav_search.py — XML parsing, ranking, magnet construction."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


_RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:nyaa="https://sukebei.nyaa.si/xmlns/nyaa" version="2.0">
  <channel>
    <item>
      <title>[H265 1080p] SSIS-500 中文字幕</title>
      <link>https://sukebei.nyaa.si/download/4356544.torrent</link>
      <guid isPermaLink="true">https://sukebei.nyaa.si/view/4356544</guid>
      <pubDate>Wed, 06 Aug 2025 06:28:28 -0000</pubDate>
      <nyaa:seeders>5</nyaa:seeders>
      <nyaa:leechers>1</nyaa:leechers>
      <nyaa:downloads>125</nyaa:downloads>
      <nyaa:size>1.4 GiB</nyaa:size>
      <nyaa:infoHash>431238a04a432238598e8c3244c55744c27cef53</nyaa:infoHash>
    </item>
    <item>
      <title>[4K] SSIS-500 高清版本</title>
      <link>https://sukebei.nyaa.si/download/3716059.torrent</link>
      <guid isPermaLink="true">https://sukebei.nyaa.si/view/3716059</guid>
      <pubDate>Wed, 06 Aug 2025 06:28:28 -0000</pubDate>
      <nyaa:seeders>2</nyaa:seeders>
      <nyaa:leechers>0</nyaa:leechers>
      <nyaa:downloads>50</nyaa:downloads>
      <nyaa:size>15.5 GiB</nyaa:size>
      <nyaa:infoHash>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</nyaa:infoHash>
    </item>
    <item>
      <title>UNRELATED-XXX random torrent</title>
      <link>https://sukebei.nyaa.si/download/0.torrent</link>
      <guid isPermaLink="true">https://sukebei.nyaa.si/view/0</guid>
      <pubDate>Wed, 06 Aug 2025 06:28:28 -0000</pubDate>
      <nyaa:seeders>99</nyaa:seeders>
      <nyaa:leechers>0</nyaa:leechers>
      <nyaa:downloads>1</nyaa:downloads>
      <nyaa:size>1 GiB</nyaa:size>
      <nyaa:infoHash>bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</nyaa:infoHash>
    </item>
  </channel>
</rss>"""


def test_quality_score():
    from app.jav_search import _quality_score
    assert _quality_score("[4K] something") == 4
    assert _quality_score("[FHD/6.86GB] something") == 3
    assert _quality_score("[H265 1080p]") == 3
    assert _quality_score("720p HD") == 2
    assert _quality_score("plain title no quality") == 0


def test_chinese_subs_detection():
    from app.jav_search import _has_chinese_subs
    assert _has_chinese_subs("SSIS-500 中文字幕")
    assert _has_chinese_subs("SSIS-500 CHS-CHT")
    assert not _has_chinese_subs("SSIS-500 plain")


def test_size_parser():
    from app.jav_search import _parse_size_to_mib
    assert _parse_size_to_mib("1.4 GiB") == 1.4 * 1024
    assert _parse_size_to_mib("650 MiB") == 650.0
    assert _parse_size_to_mib("5250MB") == 5250.0
    assert _parse_size_to_mib("") == 0.0


def test_magnet_construction():
    from app.jav_search import _make_magnet
    m = _make_magnet("abc123def456", "Title with spaces")
    assert m.startswith("magnet:?xt=urn:btih:abc123def456")
    assert "dn=Title%20with%20spaces" in m
    assert "tr=" in m


def test_parse_sukebei_rss():
    from app.jav_search import _parse_sukebei_rss
    items = _parse_sukebei_rss(_RSS_FIXTURE)
    assert len(items) == 3
    assert items[0]["info_hash"] == "431238a04a432238598e8c3244c55744c27cef53"
    assert items[0]["seeders"] == 5
    assert items[0]["has_chinese_subs"] is True
    assert items[0]["quality_score"] == 3
    # Magnet should embed the hash
    assert "431238a04a432238598e8c3244c55744c27cef53" in items[0]["magnet"]


def test_best_candidate_prefers_chinese_subs():
    from app.jav_search import best_candidate
    cands = [
        {"has_chinese_subs": False, "seeders": 100, "quality_score": 4, "size_mib": 1000, "magnet": "a"},
        {"has_chinese_subs": True, "seeders": 1, "quality_score": 2, "size_mib": 500, "magnet": "b"},
    ]
    best = best_candidate(cands)
    assert best["magnet"] == "b"   # chinese subs wins despite lower seeders/quality


def test_best_candidate_falls_back_to_seeders():
    from app.jav_search import best_candidate
    cands = [
        {"has_chinese_subs": False, "seeders": 5, "quality_score": 4, "size_mib": 1000, "magnet": "a"},
        {"has_chinese_subs": False, "seeders": 50, "quality_score": 2, "size_mib": 500, "magnet": "b"},
    ]
    best = best_candidate(cands)
    assert best["magnet"] == "b"   # higher seeders wins


def test_best_candidate_empty():
    from app.jav_search import best_candidate
    assert best_candidate([]) is None
