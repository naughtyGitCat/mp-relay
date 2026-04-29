"""Tests for merger.py — concat list construction, fallback rename, disc detection.

ffmpeg-dependent paths (the actual concat / remux subprocess) are NOT tested
here; they require ffmpeg + sample binary streams. Pure-Python helpers ARE
tested.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_strip_part_token():
    from app.merger import _strip_part_token
    assert _strip_part_token("SSIS-001-CD1.mp4") == "SSIS-001"
    assert _strip_part_token("SSIS-001.Part2.mkv") == "SSIS-001"
    assert _strip_part_token("SSIS-001-Part1.mp4") == "SSIS-001"
    assert _strip_part_token("SSIS-001.CD3.mp4") == "SSIS-001"
    # Letter suffix: "SSIS-001A" → "SSIS-001"
    assert _strip_part_token("SSIS-001 A.mp4").startswith("SSIS-001")
    # No part marker — keep stem
    assert _strip_part_token("SSIS-001.mp4") == "SSIS-001"


def test_rename_parts_jellyfin(tmp_path):
    from app.merger import rename_parts_jellyfin
    parts: list[Path] = []
    for i in (1, 2, 3):
        p = tmp_path / f"SSIS-001-CD{i}.mp4"
        p.write_bytes(b"\0")
        parts.append(p)
    logs = rename_parts_jellyfin(parts)
    # Expect Jellyfin-style names
    expected = {f"SSIS-001-cd{i}.mp4" for i in (1, 2, 3)}
    actual = {p.name for p in tmp_path.iterdir()}
    assert expected == actual
    assert all("RENAME" in line for line in logs)


def test_rename_parts_jellyfin_empty():
    from app.merger import rename_parts_jellyfin
    assert rename_parts_jellyfin([]) == []


def test_largest_m2ts_picks_main(tmp_path):
    from app.merger import _largest_m2ts
    stream = tmp_path / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    # 3 fake .m2ts: small / large / medium
    sizes = [(stream / "00001.m2ts", 1_000_000),
             (stream / "00002.m2ts", 5_000_000_000),
             (stream / "00003.m2ts", 3_000_000_000)]
    for p, sz in sizes:
        with p.open("wb") as f:
            f.seek(sz - 1)
            f.write(b"\0")
    largest = _largest_m2ts(tmp_path)
    assert largest is not None
    assert largest.name == "00002.m2ts"


def test_largest_m2ts_no_stream_dir(tmp_path):
    from app.merger import _largest_m2ts
    assert _largest_m2ts(tmp_path) is None


def test_vob_chain_picks_largest_group(tmp_path):
    from app.merger import _vob_chain
    video_ts = tmp_path / "VIDEO_TS"
    video_ts.mkdir()
    # Two VTS groups: VTS_01 (small), VTS_02 (large)
    files: list[tuple[Path, int]] = [
        (video_ts / "VTS_01_0.VOB", 1_000_000),
        (video_ts / "VTS_01_1.VOB", 100_000_000),
        (video_ts / "VTS_02_0.VOB", 1_000_000),
        (video_ts / "VTS_02_1.VOB", 800_000_000),
        (video_ts / "VTS_02_2.VOB", 800_000_000),
    ]
    for p, sz in files:
        with p.open("wb") as f:
            f.seek(sz - 1)
            f.write(b"\0")
    chain = _vob_chain(video_ts)
    # Should pick VTS_02_*; menus (_0) excluded, ordered numerically
    names = [p.name for p in chain]
    assert names == ["VTS_02_1.VOB", "VTS_02_2.VOB"]


def test_vob_chain_empty(tmp_path):
    from app.merger import _vob_chain
    # Missing dir
    assert _vob_chain(tmp_path / "nope") == []
    # Empty dir
    (tmp_path / "VIDEO_TS").mkdir()
    assert _vob_chain(tmp_path / "VIDEO_TS") == []
