"""Merge multi-part releases into a single file, and remux disc archives.

Two responsibilities, one module because they share ffmpeg/ffprobe machinery.

1) ``merge_parts``
   Multi-part JAV (CD1+CD2, Part1+Part2, A+B+C) is a real headache for
   downstream tools — older mdcx versions only archived part of a multi-CD
   release. Cleanest fix: concat the parts upstream so mdcx & Jellyfin both
   see one canonical file. We use ffmpeg's concat demuxer with ``-c copy``
   (lossless, fast) when codecs/containers match across parts. If they don't
   match (rare), we fall back to leaving them on disk with Jellyfin-friendly
   naming so at least nothing is lost.

2) ``remux_disc``
   BDMV / VIDEO_TS folders are awkward (Jellyfin handles them, but mdcx scrape
   often can't find the "main" video). We pick the largest playlist (m2ts /
   biggest VOB chain), remux to .mkv with ``-c copy`` — no re-encode — and
   delete the disc folder afterwards. ISOs are flagged but NOT auto-mounted
   (would need elevation on Windows); they get a note for manual handling.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import qc

log = logging.getLogger(__name__)


def _ffmpeg_path() -> Optional[str]:
    """Find ffmpeg — first on PATH, then under common Windows install locations."""
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    if shutil.which("ffmpeg.exe"):
        return "ffmpeg.exe"
    for candidate in (
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Multi-part merging
# ---------------------------------------------------------------------------


@dataclass
class MergeResult:
    merged_path: Optional[Path] = None
    merged_via: str = ""           # "concat-copy" | "rename-only" | ""
    deleted_parts: list[Path] = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.deleted_parts is None:
            self.deleted_parts = []


async def _stream_signature(path: Path) -> Optional[tuple]:
    """Probe the audio+video codec/profile signature so we can decide whether
    parts are concat-copy compatible.

    Returns a tuple (vcodec, vprofile, w, h, acodec, aprofile, sample_rate,
    channels) or None if probe failed.
    """
    ffp = qc._ffprobe_path()
    if not ffp:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffp,
            "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,profile,width,height,sample_rate,channels",
            "-of", "default=noprint_wrappers=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (asyncio.TimeoutError, FileNotFoundError, PermissionError):
        return None

    text = stdout.decode("utf-8", errors="replace")
    # ffprobe emits one block per stream, separated by blank lines.
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if cur:
                blocks.append(cur)
                cur = {}
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            cur[k.strip()] = v.strip()
    if cur:
        blocks.append(cur)

    v = next((b for b in blocks if b.get("codec_type") == "video"), {})
    a = next((b for b in blocks if b.get("codec_type") == "audio"), {})
    if not v:
        return None
    return (
        v.get("codec_name", ""),
        v.get("profile", ""),
        v.get("width", ""),
        v.get("height", ""),
        a.get("codec_name", ""),
        a.get("profile", ""),
        a.get("sample_rate", ""),
        a.get("channels", ""),
    )


async def _parts_are_compatible(parts: list[Path]) -> bool:
    """All parts share container ext + matching codec signatures = concat-copy safe."""
    if len(parts) < 2:
        return False
    exts = {p.suffix.lower() for p in parts}
    if len(exts) != 1:
        log.info("multipart concat blocked: mixed containers %s", exts)
        return False
    sigs: list[Optional[tuple]] = []
    for p in parts:
        sigs.append(await _stream_signature(p))
    if any(s is None for s in sigs):
        log.info("multipart concat blocked: ffprobe failed on at least one part")
        return False
    if len(set(sigs)) != 1:
        log.info("multipart concat blocked: codec/profile mismatch among parts")
        return False
    return True


def _strip_part_token(name: str) -> str:
    """Best-effort: remove the CDx/PartN/letter suffix from a filename to get
    the merged base name. Keeps the original stem otherwise."""
    stem = Path(name).stem
    patterns = [
        r"[._\-\s]CD\d+\b",
        r"[._\-\s](?:PART|PT)\d+\b",
        r"\b\d+\s*OF\s*\d+\b",
        r"-Part\d+",
        r"[._\-\s]\.CD\d+",
        r"[._\-\s][A-G]$",
    ]
    out = stem
    for pat in patterns:
        out = re.sub(pat, "", out, flags=re.I)
    out = out.rstrip(" -._")
    return out or stem


async def merge_parts(parts: list[Path], *, dry_run: bool = False) -> MergeResult:
    """Concat ``parts`` (in given order) into a single file.

    On success the original parts are deleted and the merged file is returned.
    On failure (codec mismatch / ffmpeg unavailable / error) the originals are
    untouched — caller can fall back to the multi-file Jellyfin convention via
    :func:`rename_parts_jellyfin`.
    """
    result = MergeResult()
    if len(parts) < 2:
        result.note = "merge_parts called with <2 parts; nothing to do"
        return result

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        result.note = "ffmpeg not found; cannot merge"
        return result

    if not await _parts_are_compatible(parts):
        result.note = "parts not codec-copy compatible; not merging"
        return result

    parent = parts[0].parent
    ext = parts[0].suffix
    base_name = _strip_part_token(parts[0].name)
    merged = parent / f"{base_name}{ext}"
    # Avoid clobbering: if a file with that name already exists, suffix it.
    if merged.exists():
        merged = parent / f"{base_name}.merged{ext}"

    # Build the concat list file in the same dir.
    list_file = parent / f".{base_name}.concat.txt"
    try:
        with list_file.open("w", encoding="utf-8") as f:
            for p in parts:
                # ffmpeg concat demuxer requires escaped single quotes in paths.
                safe = str(p.resolve()).replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
    except OSError as e:
        result.note = f"failed to write concat list: {e}"
        return result

    if dry_run:
        result.merged_path = merged
        result.merged_via = "concat-copy"
        result.note = f"would merge {len(parts)} parts → {merged.name}"
        try:
            list_file.unlink()
        except OSError:
            pass
        return result

    cmd = [
        ffmpeg,
        "-hide_banner", "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-map", "0",
        str(merged),
    ]
    log.info("ffmpeg concat: %d parts → %s", len(parts), merged.name)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60 * 30)
    except (asyncio.TimeoutError, FileNotFoundError, PermissionError) as e:
        result.note = f"ffmpeg invoke failed: {e}"
        try:
            list_file.unlink()
        except OSError:
            pass
        return result
    finally:
        try:
            list_file.unlink()
        except OSError:
            pass

    if proc.returncode != 0 or not merged.exists():
        result.note = (
            f"ffmpeg rc={proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace')[:300]}"
        )
        # Clean up the empty/partial output if any.
        if merged.exists():
            try:
                merged.unlink()
            except OSError:
                pass
        return result

    # Sanity-check: merged size should be close to sum of parts (allow 5% slack).
    try:
        sum_parts = sum(p.stat().st_size for p in parts)
        merged_size = merged.stat().st_size
        if merged_size < sum_parts * 0.90:
            result.note = (
                f"merged size suspicious: {merged_size} vs sum {sum_parts}; "
                f"keeping originals, deleting bad merge"
            )
            try:
                merged.unlink()
            except OSError:
                pass
            return result
    except OSError:
        pass

    # Delete parts only after merge succeeded and looks healthy.
    deleted: list[Path] = []
    for p in parts:
        try:
            p.unlink()
            deleted.append(p)
        except (PermissionError, OSError, FileNotFoundError) as e:
            log.warning("could not delete part %s after merge: %s", p, e)

    result.merged_path = merged
    result.merged_via = "concat-copy"
    result.deleted_parts = deleted
    result.note = f"merged {len(parts)} parts via concat-copy"
    return result


def rename_parts_jellyfin(parts: list[Path]) -> list[str]:
    """Fallback: rename multi-part files to Jellyfin's `<base>-cd1.ext` pattern
    so a single .nfo (written by mdcx) can serve all parts.

    Used when codecs differ and concat-copy isn't safe.
    """
    log_lines: list[str] = []
    if not parts:
        return log_lines
    base_name = _strip_part_token(parts[0].name)
    for idx, p in enumerate(parts, start=1):
        ext = p.suffix
        new_name = f"{base_name}-cd{idx}{ext}"
        new_path = p.parent / new_name
        if new_path == p:
            continue
        try:
            p.rename(new_path)
            log_lines.append(f"RENAME {p.name} → {new_name}")
        except (PermissionError, OSError, FileNotFoundError) as e:
            log_lines.append(f"FAIL rename {p.name}: {e}")
    return log_lines


# ---------------------------------------------------------------------------
# Disc archive remuxing
# ---------------------------------------------------------------------------


@dataclass
class RemuxResult:
    output_path: Optional[Path] = None
    note: str = ""
    cleaned_disc_root: bool = False


def _largest_m2ts(bdmv_root: Path) -> Optional[Path]:
    """Find the largest .m2ts file under <root>/BDMV/STREAM/.

    For most JAV BDMV releases the main feature is a single big .m2ts (>4GiB).
    This is good enough — proper playlist analysis would need libbluray.
    """
    stream_dir = bdmv_root / "BDMV" / "STREAM"
    if not stream_dir.is_dir():
        return None
    largest: Optional[Path] = None
    largest_sz = 0
    try:
        for p in stream_dir.iterdir():
            if p.suffix.lower() != ".m2ts" or not p.is_file():
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz > largest_sz:
                largest_sz = sz
                largest = p
    except (PermissionError, OSError):
        pass
    return largest


def _vob_chain(video_ts: Path) -> list[Path]:
    """Return VTS_NN_*.VOB files in order, biggest VTS group only.

    DVDs split a feature across VTS_01_1.VOB, VTS_01_2.VOB, ... We pick the VTS
    group whose total size is largest (usually the main feature) and return its
    parts in numeric order.
    """
    if not video_ts.is_dir():
        return []
    groups: dict[str, list[Path]] = {}
    pat = re.compile(r"VTS_(\d{2})_(\d+)\.VOB$", re.I)
    try:
        for p in video_ts.iterdir():
            m = pat.search(p.name)
            if not m:
                continue
            try:
                _ = p.stat().st_size
            except OSError:
                continue
            groups.setdefault(m.group(1), []).append(p)
    except (PermissionError, OSError):
        return []
    if not groups:
        return []
    # Pick largest group by total size.
    best_key = max(groups, key=lambda k: sum(p.stat().st_size for p in groups[k]))
    parts = sorted(groups[best_key], key=lambda p: p.name.lower())
    # Drop _0.VOB which is just menus.
    return [p for p in parts if not p.name.upper().endswith("_0.VOB")] or parts


async def remux_disc(disc_root: Path, *, dry_run: bool = False) -> RemuxResult:
    """Remux a Blu-ray (BDMV) or DVD (VIDEO_TS) into a single .mkv.

    Lossless: ``-c copy``, no re-encode. Output goes alongside ``disc_root``
    (same parent), named after the disc folder. On success the BDMV/VIDEO_TS
    subtree is deleted to free space; the parent directory is left intact for
    mdcx to scrape.
    """
    result = RemuxResult()
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        result.note = "ffmpeg not found; cannot remux disc"
        return result

    # Resolve which kind of disc we're dealing with.
    bdmv = disc_root / "BDMV"
    video_ts = disc_root / "VIDEO_TS"

    if bdmv.is_dir():
        src = _largest_m2ts(disc_root)
        if src is None:
            result.note = "no .m2ts found under BDMV/STREAM"
            return result
        sources = [src]
        kind = "bdmv"
    elif video_ts.is_dir():
        sources = _vob_chain(video_ts)
        if not sources:
            result.note = "no VOB chain found under VIDEO_TS"
            return result
        kind = "dvd"
    else:
        result.note = "no BDMV/ or VIDEO_TS/ under disc_root"
        return result

    out_path = disc_root / f"{disc_root.name}.mkv"
    if out_path.exists():
        out_path = disc_root / f"{disc_root.name}.remuxed.mkv"

    if dry_run:
        result.output_path = out_path
        result.note = f"would remux {kind} ({len(sources)} src) → {out_path.name}"
        return result

    if len(sources) == 1:
        cmd = [
            ffmpeg,
            "-hide_banner", "-loglevel", "error",
            "-i", str(sources[0]),
            "-c", "copy",
            "-map", "0",
            str(out_path),
        ]
    else:
        # Concat list for VOB chain
        list_file = disc_root / ".vob.concat.txt"
        try:
            with list_file.open("w", encoding="utf-8") as f:
                for p in sources:
                    safe = str(p.resolve()).replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")
        except OSError as e:
            result.note = f"failed to write concat list: {e}"
            return result
        cmd = [
            ffmpeg,
            "-hide_banner", "-loglevel", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            "-map", "0",
            str(out_path),
        ]

    log.info("ffmpeg remux disc (%s): %d src → %s", kind, len(sources), out_path.name)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60 * 60)
    except (asyncio.TimeoutError, FileNotFoundError, PermissionError) as e:
        result.note = f"ffmpeg invoke failed: {e}"
        return result
    finally:
        if len(sources) > 1:
            try:
                (disc_root / ".vob.concat.txt").unlink()
            except OSError:
                pass

    if proc.returncode != 0 or not out_path.exists():
        result.note = (
            f"ffmpeg rc={proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace')[:300]}"
        )
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        return result

    # Tear down the disc subtree to reclaim space.
    cleaned = False
    for sub in (bdmv, video_ts, disc_root / "CERTIFICATE", disc_root / "AACS"):
        if sub.is_dir():
            try:
                shutil.rmtree(sub, ignore_errors=False)
                cleaned = True
            except OSError as e:
                log.warning("rmtree %s failed: %s", sub, e)

    result.output_path = out_path
    result.cleaned_disc_root = cleaned
    result.note = f"remuxed {kind} → {out_path.name} ({len(sources)} source(s))"
    return result
