"""Tests for cover_refill — NFO parsing, URL building, refill end-to-end.

httpx is mocked because (a) we don't want CI hitting JavDB and (b) the
real failure modes (Cloudflare gate, throttling) are exactly what this
module is designed to compensate for, so we exercise the *fallback path*,
not the live network.
"""
from __future__ import annotations

import asyncio
import io as _io
import os as _os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# Real, decodable JPEG fixtures (noise-filled so they clear the module's
# _MIN_IMAGE_BYTES floor). Used where we need _has_image / _make_poster to act
# on a genuine image rather than the header-only _FAKE_JPG below.
try:
    from PIL import Image as _PILImage

    def _jpg(w: int, h: int) -> bytes:
        buf = _io.BytesIO()
        _PILImage.frombytes("RGB", (w, h), _os.urandom(w * h * 3)).save(buf, "JPEG", quality=85)
        return buf.getvalue()

    _REAL_SQUARE_JPG = _jpg(320, 320)   # ratio 1.0 → _make_poster leaves it alone
    _REAL_WIDE_JPG = _jpg(800, 538)     # standard wide cover → cropped to portrait
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REAL_SQUARE_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 4000
    _REAL_WIDE_JPG = _REAL_SQUARE_JPG
    _PIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# NFO parsing helpers
# ---------------------------------------------------------------------------

def test_javdb_cover_url_lowercases_prefix():
    from app.cover_refill import _javdb_cover_url
    # Verified shape against c0.jdbstatic.com on 2026-05-05
    assert _javdb_cover_url("1ABZQ4") == "https://c0.jdbstatic.com/covers/1a/1ABZQ4.jpg"
    assert _javdb_cover_url("Rdq7Ap") == "https://c0.jdbstatic.com/covers/rd/Rdq7Ap.jpg"
    # Lowercase id stays lowercase
    assert _javdb_cover_url("k45zxp") == "https://c0.jdbstatic.com/covers/k4/k45zxp.jpg"


def test_extract_ids_javdbid_and_num(tmp_path):
    from app.cover_refill import _extract_ids
    nfo = """<movie>
        <num>APAA-443</num>
        <javdbid>1ABZQ4</javdbid>
    </movie>"""
    jid, num = _extract_ids(nfo)
    assert jid == "1ABZQ4"
    assert num == "APAA-443"


def test_extract_ids_num_only():
    from app.cover_refill import _extract_ids
    nfo = "<movie><num>SVS-081</num></movie>"
    jid, num = _extract_ids(nfo)
    assert jid == ""
    assert num == "SVS-081"


def test_extract_ids_neither():
    from app.cover_refill import _extract_ids
    assert _extract_ids("<movie><title>x</title></movie>") == ("", "")


def test_safe_code_prefers_num():
    from app.cover_refill import _safe_code
    assert _safe_code("APAA-443 actor1,actor2", "APAA-443") == "APAA-443"
    # Num strips weird chars
    assert _safe_code("anything", "snos/073") == "snos_073"
    # No num → first folder token
    assert _safe_code("APAA-443 actor1,actor2", "") == "APAA-443"
    # No num and no whitespace in folder → whole name
    assert _safe_code("UMSO-550", "") == "UMSO-550"


# ---------------------------------------------------------------------------
# Folder helpers — using tmp_path so no network and no real library
# ---------------------------------------------------------------------------

def test_has_image_detects_jpg(tmp_path: Path):
    from app.cover_refill import _has_image
    (tmp_path / "x.mp4").write_bytes(b"x")
    assert not _has_image(tmp_path)
    (tmp_path / "poster.jpg").write_bytes(_REAL_SQUARE_JPG)
    assert _has_image(tmp_path)


def test_has_image_ignores_subfolder_images(tmp_path: Path):
    """Only top-level images count — subfolder extras shouldn't trick us."""
    from app.cover_refill import _has_image
    sub = tmp_path / "extras"
    sub.mkdir()
    (sub / "deep.jpg").write_bytes(b"x")
    assert not _has_image(tmp_path)


def test_read_nfo(tmp_path: Path):
    from app.cover_refill import _read_nfo
    assert _read_nfo(tmp_path) is None
    (tmp_path / "movie.nfo").write_text("<movie/>", encoding="utf-8")
    assert _read_nfo(tmp_path) == "<movie/>"


def test_enumerate_movie_folders_requires_nfo(tmp_path: Path):
    """Folders without an NFO (e.g. actor headshot dirs) are skipped."""
    from app.cover_refill import _enumerate_movie_folders
    studio = tmp_path / "studio_a"
    studio.mkdir()
    (studio / "MOVIE-001").mkdir()
    (studio / "MOVIE-001" / "x.nfo").write_text("<movie/>")
    (studio / "no-nfo-here").mkdir()
    (studio / "no-nfo-here" / "x.txt").write_text("x")

    found = _enumerate_movie_folders(tmp_path)
    names = sorted(p.name for p in found)
    assert names == ["MOVIE-001"]


def test_write_covers_writes_four_filenames(tmp_path: Path):
    from app.cover_refill import _write_covers
    written = _write_covers(tmp_path, "MOVIE-001", b"\xff\xd8jpg-bytes", dry_run=False)
    assert sorted(written) == sorted([
        "MOVIE-001-poster.jpg",
        "MOVIE-001-fanart.jpg",
        "MOVIE-001-thumb.jpg",
        "folder.jpg",
    ])
    for name in written:
        assert (tmp_path / name).read_bytes() == b"\xff\xd8jpg-bytes"


def test_write_covers_skips_existing(tmp_path: Path):
    from app.cover_refill import _write_covers
    (tmp_path / "MOVIE-001-poster.jpg").write_bytes(b"old")
    written = _write_covers(tmp_path, "MOVIE-001", b"new", dry_run=False)
    # poster.jpg already existed → not in written list, original preserved
    assert "MOVIE-001-poster.jpg" not in written
    assert (tmp_path / "MOVIE-001-poster.jpg").read_bytes() == b"old"
    # The other three were written
    assert "folder.jpg" in written


def test_write_covers_dry_run_writes_nothing(tmp_path: Path):
    from app.cover_refill import _write_covers
    written = _write_covers(tmp_path, "MOVIE-001", b"x", dry_run=True)
    assert len(written) == 4
    # …but no files exist on disk
    for name in written:
        assert not (tmp_path / name).exists()


# ---------------------------------------------------------------------------
# Poster cropping + corrupt-image detection
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _PIL_AVAILABLE, reason="needs Pillow")
def test_make_poster_crops_standard_wide():
    from app.cover_refill import _make_poster
    out = _make_poster(_REAL_WIDE_JPG, "ABP-123")
    assert out != _REAL_WIDE_JPG                       # re-encoded, not the original
    w, h = _PILImage.open(_io.BytesIO(out)).size
    assert h == 538 and w < 800                        # right portion only
    assert 0.6 < w / h < 0.75                          # ~2:3 portrait


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="needs Pillow")
def test_make_poster_leaves_square_and_vr_untouched():
    from app.cover_refill import _make_poster
    assert _make_poster(_REAL_SQUARE_JPG) == _REAL_SQUARE_JPG          # already portrait-ish
    assert _make_poster(_REAL_WIDE_JPG, "SIVR-123") == _REAL_WIDE_JPG  # VR → keep full frame


def test_make_poster_passes_through_undecodable():
    # The header-only _FAKE_JPG can't be opened → returned unchanged, never raises.
    from app.cover_refill import _make_poster
    assert _make_poster(_FAKE_JPG, "ABP-123") == _FAKE_JPG


@pytest.mark.skipif(not _PIL_AVAILABLE, reason="needs Pillow")
def test_has_image_rejects_corrupt(tmp_path: Path):
    from app.cover_refill import _has_image
    folder = tmp_path / "x"
    folder.mkdir()
    (folder / "stub.jpg").write_bytes(b"\xff\xd8")                 # 2-byte placeholder
    (folder / "junk.jpg").write_bytes(b"<html>not an image</html>" * 200)  # big but junk
    assert _has_image(folder) is False
    (folder / "real.jpg").write_bytes(_REAL_SQUARE_JPG)
    assert _has_image(folder) is True


def test_write_covers_splits_poster_and_fanart(tmp_path: Path):
    # poster + folder get the (cropped) front cover; fanart + thumb keep the wide image.
    from app.cover_refill import _write_covers, _make_poster
    _write_covers(tmp_path, "ABP-123", _REAL_WIDE_JPG, dry_run=False, raw_code="ABP-123")
    expected_poster = _make_poster(_REAL_WIDE_JPG, "ABP-123")
    assert (tmp_path / "ABP-123-poster.jpg").read_bytes() == expected_poster
    assert (tmp_path / "folder.jpg").read_bytes() == expected_poster
    assert (tmp_path / "ABP-123-fanart.jpg").read_bytes() == _REAL_WIDE_JPG
    assert (tmp_path / "ABP-123-thumb.jpg").read_bytes() == _REAL_WIDE_JPG


# ---------------------------------------------------------------------------
# refill_one — end-to-end flow with mocked httpx
# ---------------------------------------------------------------------------

def _setup_folder(tmp_path: Path, *, nfo_content: str | None = "<movie><javdbid>1ABZQ4</javdbid><num>APAA-443</num></movie>",
                  with_image: bool = False) -> Path:
    folder = tmp_path / "APAA-443 actor"
    folder.mkdir()
    (folder / "APAA-443.mp4").write_bytes(b"video")
    if nfo_content is not None:
        (folder / "APAA-443.nfo").write_text(nfo_content, encoding="utf-8")
    if with_image:
        (folder / "existing.jpg").write_bytes(_REAL_SQUARE_JPG)
    return folder


class _FakeResp:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")


class _FakeClient:
    """Stand-in for httpx.AsyncClient. ``responses`` maps URL substring →
    canned _FakeResp."""
    def __init__(self, responses: dict[str, _FakeResp]):
        self._responses = responses

    async def get(self, url, headers=None):
        for substr, resp in self._responses.items():
            if substr in url:
                return resp
        return _FakeResp(404)


# A valid-looking image: ≥2000 bytes + JPEG magic, so cover_refill's
# _looks_like_image() accepts it (it rejects tiny bodies + HTML 200s).
_FAKE_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 2200


def test_refill_one_javdbid_writes_covers(tmp_path: Path):
    from app import cover_refill
    folder = _setup_folder(tmp_path)
    fake = _FakeClient({"/covers/1a/1ABZQ4.jpg": _FakeResp(200, _FAKE_JPG)})
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "refilled"
    assert res.javdbid == "1ABZQ4"
    assert res.code == "APAA-443"
    assert (folder / "APAA-443-poster.jpg").read_bytes() == _FAKE_JPG
    assert (folder / "folder.jpg").exists()


def test_refill_one_skips_folder_with_image(tmp_path: Path):
    from app import cover_refill
    folder = _setup_folder(tmp_path, with_image=True)
    fake = _FakeClient({})
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "skip_has_img"


def test_refill_one_no_nfo(tmp_path: Path):
    from app import cover_refill
    folder = _setup_folder(tmp_path, nfo_content=None)
    fake = _FakeClient({})
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "error"
    assert "no NFO" in res.reason


def test_refill_one_no_id_at_all(tmp_path: Path):
    from app import cover_refill
    folder = _setup_folder(tmp_path, nfo_content="<movie><title>X</title></movie>")
    fake = _FakeClient({})
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "skip_no_id"


def test_refill_one_falls_back_to_search_by_num(tmp_path: Path):
    """NFO has <num> but no <javdbid> → search JavDB to find the id."""
    from app import cover_refill
    folder = _setup_folder(tmp_path, nfo_content="<movie><num>APAA-443</num></movie>")
    # Search hits return HTML containing /v/1ABZQ4; cover fetch returns JPEG.
    search_html = '<a href="/v/1ABZQ4" class="box">APAA-443 ...</a>'
    fake = _FakeClient({
        "/search?": _FakeResp(200, search_html.encode("utf-8")),
        "/covers/1a/1ABZQ4.jpg": _FakeResp(200, _FAKE_JPG),
    })
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "refilled"
    assert res.javdbid == "1ABZQ4"
    assert (folder / "APAA-443-poster.jpg").exists()


def test_refill_one_search_no_match(tmp_path: Path):
    from app import cover_refill
    folder = _setup_folder(tmp_path, nfo_content="<movie><num>NOPE-999</num></movie>")
    fake = _FakeClient({"/search?": _FakeResp(200, b"<html>nothing here</html>")})
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "skip_no_id"


def test_refill_one_cover_fetch_404(tmp_path: Path):
    """No source yields a cover (JavBus/AVSOX miss, JavDB CDN 404s on a stale
    id) → skip_no_id, not a crash."""
    from app import cover_refill
    folder = _setup_folder(tmp_path)
    fake = _FakeClient({"/covers/": _FakeResp(404)})
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "skip_no_id"


def test_refill_one_dry_run_writes_nothing(tmp_path: Path):
    from app import cover_refill
    folder = _setup_folder(tmp_path)
    fake = _FakeClient({"/covers/1a/1ABZQ4.jpg": _FakeResp(200, _FAKE_JPG)})
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=True))
    assert res.status == "dry_run"
    assert len(res.files_written) == 4
    for name in res.files_written:
        assert not (folder / name).exists()


def test_refill_one_javbus_path(tmp_path: Path):
    """No <javdbid>, just a 番号 → JavBus detail page → a.bigImage cover."""
    from app import cover_refill
    folder = _setup_folder(tmp_path, nfo_content="<movie><num>CAWD-368</num></movie>")
    detail = '<a class="bigImage" href="/pics/cover/8w76_b.jpg"><img src="/pics/cover/8w76_b.jpg"></a>'
    fake = _FakeClient({
        "javbus.com/CAWD-368": _FakeResp(200, detail.encode("utf-8")),
        "/pics/cover/8w76_b.jpg": _FakeResp(200, _FAKE_JPG),
    })
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "refilled"
    assert res.source == "javbus"
    assert (folder / "CAWD-368-poster.jpg").read_bytes() == _FAKE_JPG


def test_refill_one_avsox_fallback(tmp_path: Path):
    """JavBus misses → AVSOX search → first movie-box → detail → bigImage."""
    from app import cover_refill
    folder = _setup_folder(tmp_path, nfo_content="<movie><num>KV-233</num></movie>")
    search = '<a class="movie-box" href="https://avsox.click/cn/movie/abc123">x</a>'
    detail = '<a class="bigImage" href="https://jp.netcdn.space/cover.jpg"><img></a>'
    fake = _FakeClient({
        "avsox.click/cn/search/KV-233": _FakeResp(200, search.encode("utf-8")),
        "avsox.click/cn/movie/abc123": _FakeResp(200, detail.encode("utf-8")),
        "jp.netcdn.space/cover.jpg": _FakeResp(200, _FAKE_JPG),
    })
    res = asyncio.run(cover_refill.refill_one(fake, folder, dry_run=False))
    assert res.status == "refilled"
    assert res.source == "avsox"


# ---------------------------------------------------------------------------
# refill_root — multi-folder walk
# ---------------------------------------------------------------------------

def test_refill_root_summarizes(tmp_path: Path, monkeypatch):
    from app import cover_refill

    # Three folders: one refillable (javdbid), one skip_has_img, one skip_no_id
    studio = tmp_path / "studio"
    studio.mkdir()
    f1 = studio / "A-1"; f1.mkdir()
    (f1 / "x.nfo").write_text("<movie><javdbid>1ABZQ4</javdbid><num>A-1</num></movie>")
    f2 = studio / "A-2"; f2.mkdir()
    (f2 / "x.nfo").write_text("<movie><javdbid>1ABZQ4</javdbid></movie>")
    (f2 / "x.jpg").write_bytes(_REAL_SQUARE_JPG)
    f3 = studio / "A-3"; f3.mkdir()
    (f3 / "x.nfo").write_text("<movie><title>x</title></movie>")

    fake = _FakeClient({"/covers/1a/1ABZQ4.jpg": _FakeResp(200, _FAKE_JPG)})

    class _CtxFakeClient:
        async def __aenter__(self): return fake
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(cover_refill.httpx, "AsyncClient", lambda *a, **kw: _CtxFakeClient())

    summary = asyncio.run(cover_refill.refill_root(str(tmp_path), dry_run=False))
    assert summary["scanned_folders"] == 3
    # Only A-1 and A-3 are missing images. A-2 is filtered out before refill_one runs.
    assert summary["missing_image_candidates"] == 2
    counts = summary["summary"]
    assert counts.get("refilled") == 1
    assert counts.get("skip_no_id") == 1
