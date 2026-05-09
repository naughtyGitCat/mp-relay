"""Tests for post_download.py — path sanitization + mdcx summary parsing.

These cover the two real bugs found in production on 2026-04-30/05-01:

  1. The 115 sync produced a folder ``SNOS-073.[4K]@R90s`` containing a file
     ``169bbs.com@SNOS-073_[4K].mkv``. mdcx's Path.glob treated ``[4K]`` as
     a character class, scanned 0 files, returned ``rc=0 total=0``. mp-relay
     marked the task ``scraped`` and reported success — silent failure.

  2. Even after manually renaming, mdcx returned ``total=1, success=0,
     failed=1`` because SNOS isn't in mdcx's official 番号前缀 whitelist.
     mp-relay treated ``rc=0`` as success regardless of summary.

The sanitize helpers prevent class (1); the summary parser distinguishes
``scraped`` / ``scrape_no_match`` / ``scrape_failed_items`` for class (2).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# _sanitize_name — pure function, easy to nail down
# ---------------------------------------------------------------------------

def test_sanitize_name_strips_brackets():
    from app.post_download import _sanitize_name
    assert _sanitize_name("SNOS-073.[4K]@R90s") == "SNOS-073._4K_R90s"


def test_sanitize_name_strips_atsign_and_parens():
    from app.post_download import _sanitize_name
    assert _sanitize_name("169bbs.com@SSIS-001(noads)") == "169bbs.com_SSIS-001_noads"


def test_sanitize_name_collapses_runs_and_trims():
    from app.post_download import _sanitize_name
    assert _sanitize_name("[[a]]") == "a"
    assert _sanitize_name("[]@()") == "unnamed"   # everything stripped → fallback
    assert _sanitize_name("") == "unnamed"


def test_sanitize_name_preserves_chinese_and_japanese():
    from app.post_download import _sanitize_name
    assert _sanitize_name("葵つかさ-SNOS001") == "葵つかさ-SNOS001"
    assert _sanitize_name("乌鸦不择主") == "乌鸦不择主"


def test_sanitize_name_preserves_dots_and_dashes():
    from app.post_download import _sanitize_name
    # Common JAV-code / version separators stay
    assert _sanitize_name("SSIS-001.4K-CHS") == "SSIS-001.4K-CHS"


# ---------------------------------------------------------------------------
# _sanitize_target_dir — disk-touching, uses tmp_path fixtures
# ---------------------------------------------------------------------------

def test_sanitize_target_dir_renames_when_unsafe(tmp_path):
    from app.post_download import _sanitize_target_dir
    src = tmp_path / "SNOS-073.[4K]@R90s"
    src.mkdir()
    (src / "x.mkv").write_bytes(b"\0")
    new = _sanitize_target_dir(str(src))
    assert new != str(src)
    assert Path(new).is_dir()
    assert "[4K]" not in Path(new).name
    assert (Path(new) / "x.mkv").exists()


def test_sanitize_target_dir_noop_when_safe(tmp_path):
    from app.post_download import _sanitize_target_dir
    src = tmp_path / "SNOS-073-clean"
    src.mkdir()
    new = _sanitize_target_dir(str(src))
    assert new == str(src)


def test_sanitize_target_dir_handles_collision(tmp_path):
    """If the safe name already exists (left over from an earlier run),
    append a numeric suffix instead of clobbering."""
    from app.post_download import _sanitize_target_dir
    (tmp_path / "SNOS-073_4K").mkdir()  # collision target
    src = tmp_path / "SNOS-073.[4K]"
    src.mkdir()
    new = _sanitize_target_dir(str(src))
    assert Path(new).is_dir()
    assert Path(new).name not in ("SNOS-073_4K", src.name)


def test_sanitize_target_dir_missing_returns_input(tmp_path):
    from app.post_download import _sanitize_target_dir
    nope = tmp_path / "does_not_exist"
    assert _sanitize_target_dir(str(nope)) == str(nope)


# ---------------------------------------------------------------------------
# _sanitize_video_filenames
# ---------------------------------------------------------------------------

def test_sanitize_video_filenames_renames_brackets(tmp_path):
    from app.post_download import _sanitize_video_filenames
    (tmp_path / "169bbs.com@SNOS-073_[4K].mkv").write_bytes(b"\0")
    (tmp_path / "poster.jpg").write_bytes(b"\0")     # not a video; left alone
    notes = _sanitize_video_filenames(str(tmp_path))
    names = {p.name for p in tmp_path.iterdir()}
    # Video file got cleaned, non-video unchanged
    assert "169bbs.com@SNOS-073_[4K].mkv" not in names
    assert "poster.jpg" in names
    assert any(n.endswith(".mkv") and "[" not in n and "@" not in n for n in names)
    assert any("renamed for mdcx" in note for note in notes)


def test_sanitize_video_filenames_skips_non_videos(tmp_path):
    """Don't rename .url / .nfo / .jpg even if they have unsafe chars."""
    from app.post_download import _sanitize_video_filenames
    (tmp_path / "promo.[ad].url").write_bytes(b"\0")
    (tmp_path / "metadata.[v1].nfo").write_bytes(b"\0")
    notes = _sanitize_video_filenames(str(tmp_path))
    # nothing changed
    names = {p.name for p in tmp_path.iterdir()}
    assert "promo.[ad].url" in names
    assert "metadata.[v1].nfo" in names
    assert notes == []


def test_sanitize_video_filenames_noop_on_clean(tmp_path):
    from app.post_download import _sanitize_video_filenames
    (tmp_path / "SNOS-073.mkv").write_bytes(b"\0")
    notes = _sanitize_video_filenames(str(tmp_path))
    assert notes == []
    assert (tmp_path / "SNOS-073.mkv").exists()


def test_sanitize_video_filenames_avoids_clobber(tmp_path):
    """Two dirty files with chars that map to the same safe stem must not
    overwrite each other. The sanitizer is intentionally local (only strips
    unsafe chars; doesn't reorder or extract codes), so collision is rare —
    typically only happens when the user already manually cleaned one copy.
    """
    from app.post_download import _sanitize_video_filenames
    # Both stems sanitize to "a_b"
    (tmp_path / "a_b.mkv").write_bytes(b"first")    # already-safe, will collide
    (tmp_path / "a[b].mkv").write_bytes(b"second")  # would rename to a_b.mkv
    notes = _sanitize_video_filenames(str(tmp_path))
    # The first file is untouched; the colliding rename is skipped
    assert (tmp_path / "a_b.mkv").read_bytes() == b"first"
    assert (tmp_path / "a[b].mkv").exists()
    assert any("target exists" in n for n in notes)


# ---------------------------------------------------------------------------
# _parse_mdcx_summary — the JSON shape mdcx writes to stdout
# ---------------------------------------------------------------------------

def test_parse_mdcx_summary_genuine_success():
    from app.post_download import _parse_mdcx_summary
    stdout = '{"total": 1, "success": 1, "failed": 0, "failed_items": []}\n'
    s = _parse_mdcx_summary(stdout)
    assert s == {"total": 1, "success": 1, "failed": 0, "failed_items": []}


def test_parse_mdcx_summary_silent_skip():
    """The original SNOS-073 [4K] case: rc=0 but mdcx scanned 0 files."""
    from app.post_download import _parse_mdcx_summary
    stdout = '{"total": 0, "success": 0, "failed": 0, "failed_items": []}'
    s = _parse_mdcx_summary(stdout)
    assert s["total"] == 0 and s["success"] == 0


def test_parse_mdcx_summary_failed_with_reasons():
    """Single-file mode with SNOS-073 prefix-not-whitelisted failure."""
    from app.post_download import _parse_mdcx_summary
    stdout = ('{"total": 1, "success": 0, "failed": 1, '
              '"failed_items": [{"path": "/jav/SNOS-073.mkv", "reason": "不在官网番号前缀列表中"}]}')
    s = _parse_mdcx_summary(stdout)
    assert s["success"] == 0
    assert s["failed"] == 1
    assert s["failed_items"][0]["reason"] == "不在官网番号前缀列表中"


def test_parse_mdcx_summary_handles_interleaved_output():
    """If mdcx printed warnings before the JSON, our brace-match still finds it."""
    from app.post_download import _parse_mdcx_summary
    stdout = (
        "Warning: pkg_resources deprecated\n"
        "Some other log line\n"
        '{"total": 1, "success": 1, "failed": 0, "failed_items": []}\n'
        "Trailing junk\n"
    )
    s = _parse_mdcx_summary(stdout)
    assert s["success"] == 1


def test_parse_mdcx_summary_empty_or_invalid():
    from app.post_download import _parse_mdcx_summary
    # Empty stdout
    s = _parse_mdcx_summary("")
    assert s == {"total": 0, "success": 0, "failed": 0, "failed_items": []}
    # Garbage
    s = _parse_mdcx_summary("not json at all 12345 {invalid}")
    assert s == {"total": 0, "success": 0, "failed": 0, "failed_items": []}


def test_parse_mdcx_summary_caps_failed_items():
    """mdcx may report 1000+ failed items; we keep first 5 to avoid huge state rows."""
    from app.post_download import _parse_mdcx_summary
    items = [{"path": f"/x/{i}.mkv", "reason": "x"} for i in range(20)]
    import json as _json
    stdout = _json.dumps({"total": 20, "success": 0, "failed": 20, "failed_items": items})
    s = _parse_mdcx_summary(stdout)
    assert len(s["failed_items"]) == 5


# ---------------------------------------------------------------------------
# _move_to_failed_holding — sibling-collector pattern + override config
# ---------------------------------------------------------------------------

def test_move_to_failed_holding_default_pattern(tmp_path, monkeypatch):
    """Default (no failed_output_dir override): collect under
    <staging-parent>/scrapefailed/<basename>/."""
    from app import post_download
    from app.config import settings
    monkeypatch.setattr(settings, "failed_output_dir", "")

    staging = tmp_path / "staging" / "SNOS-073"
    staging.mkdir(parents=True)
    (staging / "video.mp4").write_bytes(b"fake")

    moved = post_download._move_to_failed_holding(str(staging), kind="scrape")
    assert moved is not None
    assert not staging.exists()                                      # original gone
    assert (tmp_path / "staging" / "scrapefailed" / "SNOS-073").is_dir()
    assert (tmp_path / "staging" / "scrapefailed" / "SNOS-073" / "video.mp4").is_file()


def test_move_to_failed_holding_qc_kind(tmp_path, monkeypatch):
    """kind='qc' picks the qcfailed/ subdir, not scrapefailed/."""
    from app import post_download
    from app.config import settings
    monkeypatch.setattr(settings, "failed_output_dir", "")

    staging = tmp_path / "staging" / "FAKE-001"
    staging.mkdir(parents=True)
    (staging / "manko.fun.mp4").write_bytes(b"x" * 1024)

    moved = post_download._move_to_failed_holding(str(staging), kind="qc")
    assert moved is not None
    assert (tmp_path / "staging" / "qcfailed" / "FAKE-001").is_dir()
    # scrapefailed/ should NOT have been touched
    assert not (tmp_path / "staging" / "scrapefailed").exists()


def test_move_to_failed_holding_uses_override(tmp_path, monkeypatch):
    """When failed_output_dir is set, target = <override>/<kind>/<basename>/."""
    from app import post_download
    from app.config import settings
    override = tmp_path / "central-failed"
    monkeypatch.setattr(settings, "failed_output_dir", str(override))

    staging = tmp_path / "staging" / "ABC-001"
    staging.mkdir(parents=True)
    (staging / "v.mp4").write_bytes(b"x")

    moved = post_download._move_to_failed_holding(str(staging), kind="scrape")
    assert moved is not None
    assert (override / "scrapefailed" / "ABC-001").is_dir()
    assert (override / "scrapefailed" / "ABC-001" / "v.mp4").is_file()


def test_move_to_failed_holding_empty_dir_noop(tmp_path):
    """If staging is empty (mdcx already moved files out), we no-op."""
    from app import post_download
    staging = tmp_path / "staging" / "MOVED-BY-MDCX"
    staging.mkdir(parents=True)
    # No files inside

    moved = post_download._move_to_failed_holding(str(staging), kind="scrape")
    assert moved is None
    assert staging.exists()                                          # left as-is
    assert not (tmp_path / "staging" / "scrapefailed").exists()


def test_move_to_failed_holding_missing_dir_noop(tmp_path):
    """If staging dir is gone entirely (e.g. mdcx + del_empty_folder),
    no-op cleanly without raising."""
    from app import post_download
    moved = post_download._move_to_failed_holding(
        str(tmp_path / "never-existed"), kind="scrape",
    )
    assert moved is None


def test_move_to_failed_holding_conflict_suffix(tmp_path, monkeypatch):
    """If destination already exists (e.g. retry of same code that
    failed once before), append a timestamp suffix instead of clobbering."""
    from app import post_download
    from app.config import settings
    monkeypatch.setattr(settings, "failed_output_dir", "")

    # Simulate a prior failure already moved
    prior = tmp_path / "staging" / "scrapefailed" / "X-001"
    prior.mkdir(parents=True)
    (prior / "old.mp4").write_bytes(b"old")

    # New failure of same code
    staging = tmp_path / "staging" / "X-001"
    staging.mkdir(parents=True)
    (staging / "new.mp4").write_bytes(b"new")

    moved = post_download._move_to_failed_holding(str(staging), kind="scrape")
    assert moved is not None
    assert moved != str(prior)                                       # different path
    # Original prior still has its old content
    assert (prior / "old.mp4").read_bytes() == b"old"
    # New move has the new content
    assert Path(moved, "new.mp4").read_bytes() == b"new"
