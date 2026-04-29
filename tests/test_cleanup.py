"""Tests for cleanup.py — file triage + extras preservation + disc detection.

ffprobe-dependent paths are exercised separately in integration tests; here we
mock them or use synthetic file trees with stub sizes.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# helpers — keyword classification
# ---------------------------------------------------------------------------

def test_extras_keywords_beat_sample():
    from app.cleanup import _is_extras_filename, _is_sample_filename
    # "trailer" alone is sample
    assert _is_sample_filename("SSIS-001-trailer.mp4")
    assert not _is_extras_filename("SSIS-001-trailer.mp4")

    # 花絮 is extras (NOT sample)
    assert _is_extras_filename("SSIS-001-花絮.mp4")
    assert not _is_sample_filename("SSIS-001-花絮.mp4")

    # 特典 is extras
    assert _is_extras_filename("SSIS-001-特典.mp4")
    assert not _is_sample_filename("SSIS-001-特典.mp4")

    # making-of is extras
    assert _is_extras_filename("SSIS-001-making-of.mp4")
    assert not _is_sample_filename("SSIS-001-making-of.mp4")

    # Plain main file is neither
    assert not _is_extras_filename("SSIS-001.mp4")
    assert not _is_sample_filename("SSIS-001.mp4")


def test_part_index_extraction():
    from app.cleanup import _part_index, _has_part_marker
    assert _part_index("SSIS-001-CD1.mp4") == 1
    assert _part_index("SSIS-001-CD2.mp4") == 2
    assert _part_index("SSIS-001.Part1.mp4") == 1
    assert _part_index("SSIS-001-Part3.mp4") == 3
    assert _part_index("SSIS-001A.mp4") == 1
    assert _part_index("SSIS-001-B.mp4") == 2
    assert _part_index("1of3.SSIS-001.mp4") == 1
    assert _part_index("SSIS-001.mp4") is None
    assert _has_part_marker("SSIS-001-CD1.mp4")
    assert not _has_part_marker("SSIS-001.mp4")


def test_junk_extensions_classified():
    from app.cleanup import JUNK_EXTS, VIDEO_EXTS
    # Sanity: extensions don't overlap
    assert not (JUNK_EXTS & VIDEO_EXTS)
    # Common spam files are JUNK
    assert ".url" in JUNK_EXTS
    assert ".lnk" in JUNK_EXTS
    assert ".html" in JUNK_EXTS
    # Common videos are VIDEO
    assert ".mp4" in VIDEO_EXTS
    assert ".mkv" in VIDEO_EXTS
    assert ".m2ts" in VIDEO_EXTS    # disc archives


# ---------------------------------------------------------------------------
# triage_dir over synthetic trees
# ---------------------------------------------------------------------------

def _make_file(p: Path, size_mib: float = 0.001) -> None:
    """Create a sparse file of approximately size_mib MiB."""
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as f:
        if size_mib > 0:
            f.seek(int(size_mib * 1024 * 1024) - 1)
            f.write(b"\0")


def test_triage_single_video(tmp_path):
    from app import cleanup
    _make_file(tmp_path / "SSIS-001.mp4", size_mib=300)
    _make_file(tmp_path / "promo-website.url", size_mib=0)
    _make_file(tmp_path / "readme.txt", size_mib=0)

    triage = asyncio.run(cleanup.triage_dir(str(tmp_path)))
    assert len(triage.keep_videos) == 1
    assert triage.keep_videos[0].name == "SSIS-001.mp4"
    junk_names = {p.name for p in triage.delete_junk}
    assert "promo-website.url" in junk_names
    assert "readme.txt" in junk_names
    assert not triage.multipart


def test_triage_multipart_keeps_all(tmp_path):
    from app import cleanup
    _make_file(tmp_path / "SSIS-001-CD1.mp4", size_mib=300)
    _make_file(tmp_path / "SSIS-001-CD2.mp4", size_mib=300)
    _make_file(tmp_path / "SSIS-001-CD3.mp4", size_mib=300)

    triage = asyncio.run(cleanup.triage_dir(str(tmp_path)))
    assert triage.multipart
    assert len(triage.multipart_parts) == 3
    # Sorted by part index
    assert [p.name for p in triage.multipart_parts] == [
        "SSIS-001-CD1.mp4", "SSIS-001-CD2.mp4", "SSIS-001-CD3.mp4",
    ]
    assert triage.delete_dupes == []


def test_triage_extras_preserved(tmp_path):
    from app import cleanup
    _make_file(tmp_path / "SSIS-001.mp4", size_mib=300)
    _make_file(tmp_path / "SSIS-001-花絮.mp4", size_mib=50)
    _make_file(tmp_path / "SSIS-001-特典.mp4", size_mib=80)
    _make_file(tmp_path / "SSIS-001-trailer.mp4", size_mib=10)  # this IS sample

    triage = asyncio.run(cleanup.triage_dir(str(tmp_path)))
    extras_names = {p.name for p in triage.extras}
    assert "SSIS-001-花絮.mp4" in extras_names
    assert "SSIS-001-特典.mp4" in extras_names
    sample_names = {p.name for p in triage.delete_samples}
    assert "SSIS-001-trailer.mp4" in sample_names
    assert "SSIS-001.mp4" in {p.name for p in triage.keep_videos}


def test_triage_disc_archive_detected(tmp_path):
    from app import cleanup
    bdmv = tmp_path / "BDMV"
    (bdmv / "STREAM").mkdir(parents=True)
    _make_file(bdmv / "STREAM" / "00001.m2ts", size_mib=5000)
    _make_file(tmp_path / "promo.url", size_mib=0)

    triage = asyncio.run(cleanup.triage_dir(str(tmp_path)))
    assert triage.disc_archive is not None
    assert triage.disc_archive == tmp_path
    # Junk outside disc archive still slated for deletion
    junk_names = {p.name for p in triage.delete_junk}
    assert "promo.url" in junk_names
    # Disc files NOT in delete lists
    keep_paths = (
        triage.keep_videos + triage.delete_dupes + triage.delete_samples
    )
    for p in keep_paths:
        assert "BDMV" not in p.parts


def test_relocate_extras_creates_subfolder(tmp_path):
    from app import cleanup
    _make_file(tmp_path / "SSIS-001.mp4", size_mib=300)
    _make_file(tmp_path / "SSIS-001-花絮.mp4", size_mib=50)

    triage = asyncio.run(cleanup.triage_dir(str(tmp_path)))
    logs = cleanup.relocate_extras(triage, str(tmp_path))

    extras_dir = tmp_path / "Extras"
    assert extras_dir.is_dir()
    assert (extras_dir / "SSIS-001-花絮.mp4").exists()
    assert not (tmp_path / "SSIS-001-花絮.mp4").exists()
    assert any("MOVE" in line for line in logs)


def test_post_mdcx_skips_extras_subfolder(tmp_path):
    from app import cleanup
    _make_file(tmp_path / "SSIS-001.mp4", size_mib=300)
    extras_dir = tmp_path / "Extras"
    extras_dir.mkdir()
    _make_file(extras_dir / "SSIS-001-花絮.mp4", size_mib=50)
    _make_file(tmp_path / "leftover.url", size_mib=0)
    _make_file(tmp_path / "trailer-promo.mp4", size_mib=10)

    logs = cleanup.post_mdcx_cleanup(str(tmp_path))
    # leftover .url and the promo trailer should be deleted
    deleted_paths = [line.split(" ")[-1] for line in logs if line.startswith("DELETE")]
    deleted_basenames = {Path(p).name for p in deleted_paths}
    assert "leftover.url" in deleted_basenames
    assert "trailer-promo.mp4" in deleted_basenames
    # Extras file untouched
    assert (extras_dir / "SSIS-001-花絮.mp4").exists()


def test_execute_dry_run_doesnt_delete(tmp_path):
    from app import cleanup
    junk = tmp_path / "spam.url"
    _make_file(junk, size_mib=0)
    triage = cleanup.FileTriage(delete_junk=[junk])
    logs = cleanup.execute(triage, dry_run=True)
    assert junk.exists()
    assert any("dry-run" in line for line in logs)
