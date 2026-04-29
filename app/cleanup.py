"""Post-download file triage: delete junk, dedupe multi-resolution, preserve multi-part.

Run BEFORE mdcx scrape so mdcx sees a clean directory with the canonical video file(s).
Run AGAIN AFTER mdcx scrape to remove anything mdcx didn't reference (sample images, leftover txt).

Triage outputs (FileTriage):
  - keep_videos     : main video file(s) mdcx should scrape
  - delete_junk     : .url/.lnk/.html etc. — promo / shortcut spam
  - delete_dupes    : multi-resolution coexisting copies (keep the highest-res)
  - delete_samples  : small sample / preview clips
  - extras          : 花絮 / 特典 / making-of / bonus — PRESERVED, will be moved
                       to an Extras/ subfolder by the merger pipeline
  - multipart_parts : ordered list of CD1/CD2/PartN/A/B/C parts; the merger
                       pipeline tries to concat these into a single file
  - disc_archive    : BDMV / VIDEO_TS root folder for remuxing to .mkv
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import qc

log = logging.getLogger(__name__)

# File extensions
VIDEO_EXTS: set[str] = {".mp4", ".mkv", ".avi", ".wmv", ".m4v", ".mov", ".ts", ".rmvb", ".flv", ".m2ts"}
# Junk extensions: shortcuts to porn sites + readme spam + html promo
JUNK_EXTS: set[str] = {".url", ".lnk", ".html", ".htm", ".txt", ".md", ".rtf", ".docx", ".doc"}
# Image extensions (usually fanart/poster — keep before mdcx; clean orphans after)
IMAGE_EXTS: set[str] = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# Filename keywords for true sample / promo / ad junk (case-insensitive substring).
# Deliberately narrower than before — "片花" was removed because it's ambiguous
# (literally "movie flower"; can mean either teaser-trailer OR behind-the-scenes).
SAMPLE_KEYWORDS: list[str] = [
    "sample", "preview", "trailer", "promo",
    "广告", "宣传", "预告",
]

# Keywords that mark genuine extras to preserve. These are NOT deleted; the
# pipeline moves them into an Extras/ subfolder so Jellyfin will pick them up.
EXTRAS_KEYWORDS: list[str] = [
    "花絮", "特典", "幕后", "ng",
    "making", "making-of", "makingof", "behind", "behind-the-scenes", "behindthescenes",
    "bonus", "extra", "extras", "ost", "interview",
]

# Part / CD markers — these CONFIRM a legitimate multi-file release.
# Capture: (idx_str) used to sort parts in correct order.
_PART_PATTERNS: list[tuple[re.Pattern, int]] = [
    # group index that holds the part number
    (re.compile(r"[._\-\s]CD(\d+)\b", re.I), 1),
    (re.compile(r"[._\-\s](?:PART|PT)(\d+)\b", re.I), 1),
    (re.compile(r"\b(\d+)\s*OF\s*\d+\b", re.I), 1),       # "1of3", "2of3"
    (re.compile(r"\.CD(\d+)\.[a-z0-9]+$", re.I), 1),
    (re.compile(r"-Part(\d+)", re.I), 1),
    # SSIS-001A.mp4 / SSIS-001-B.mp4 (letter-based; A→1, B→2, C→3 ...).
    # The letter must come right after a digit (so "ABCD-123.mp4" doesn't match)
    # but a separator is optional so both SSIS-001A and SSIS-001-A are caught.
    (re.compile(r"(?<=\d)[._\-\s]?([A-G])\.[a-z0-9]+$"), 1),
]


@dataclass
class FileTriage:
    keep_videos: list[Path] = field(default_factory=list)
    delete_junk: list[Path] = field(default_factory=list)
    delete_dupes: list[Path] = field(default_factory=list)
    delete_samples: list[Path] = field(default_factory=list)
    extras: list[Path] = field(default_factory=list)
    multipart_parts: list[Path] = field(default_factory=list)   # ordered by part index
    disc_archive: Optional[Path] = None                          # BDMV / VIDEO_TS root
    notes: list[str] = field(default_factory=list)

    @property
    def multipart(self) -> bool:
        return bool(self.multipart_parts)


def _part_index(name: str) -> Optional[int]:
    """Return part index (1-based) if `name` matches any _PART_PATTERNS, else None."""
    for pat, gi in _PART_PATTERNS:
        m = pat.search(name)
        if not m:
            continue
        token = m.group(gi)
        if token.isdigit():
            return int(token)
        # letter-based: A→1, B→2, ...
        if len(token) == 1 and token.isalpha():
            return ord(token.upper()) - ord("A") + 1
    return None


def _has_part_marker(name: str) -> bool:
    return _part_index(name) is not None


def _is_sample_filename(name: str) -> bool:
    lower = name.lower()
    # Reject if it actually matches an extras keyword first — extras win.
    if _is_extras_filename(name):
        return False
    return any(kw.lower() in lower for kw in SAMPLE_KEYWORDS)


def _is_extras_filename(name: str) -> bool:
    lower = name.lower()
    return any(kw.lower() in lower for kw in EXTRAS_KEYWORDS)


def _list_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    out: list[Path] = []
    try:
        for p in target.rglob("*"):
            if p.is_file():
                out.append(p)
    except (PermissionError, OSError) as e:
        log.warning("rglob failed in %s: %s", target, e)
    return out


def _detect_disc_archive(base: Path) -> Optional[Path]:
    """Detect a Blu-ray (BDMV/) or DVD (VIDEO_TS/) folder under `base`.

    Returns the *parent of* BDMV / VIDEO_TS — i.e. the disc root. The caller
    feeds this to merger.remux_disc() which knows how to enumerate playlists.
    """
    if not base.exists() or not base.is_dir():
        return None
    # Direct child match
    for marker in ("BDMV", "VIDEO_TS"):
        cand = base / marker
        if cand.is_dir():
            return base
    # One level deep (some releases nest as <code>/BDMV/...)
    try:
        for child in base.iterdir():
            if not child.is_dir():
                continue
            for marker in ("BDMV", "VIDEO_TS"):
                if (child / marker).is_dir():
                    return child
    except (PermissionError, OSError):
        pass
    return None


async def _video_metadata(path: Path) -> dict:
    """Probe height + duration via ffprobe. Returns {} if unavailable."""
    ffp = qc._ffprobe_path()
    if not ffp:
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            ffp,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=height,bit_rate:format=duration,size",
            "-of", "default=noprint_wrappers=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (asyncio.TimeoutError, FileNotFoundError, PermissionError):
        return {}
    text = stdout.decode("utf-8", errors="replace")
    out: dict = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if k == "height" and v.isdigit():
            out["height"] = int(v)
        elif k == "duration":
            try:
                out["duration"] = float(v)
            except ValueError:
                pass
        elif k == "bit_rate" and v.isdigit():
            out["bit_rate"] = int(v)
        elif k == "size" and v.isdigit():
            out["size"] = int(v)
    return out


def _group_by_similar_duration(infos: list[tuple[Path, dict]],
                                tol_seconds: float = 60) -> list[list[tuple[Path, dict]]]:
    """Greedy O(n^2) grouping. n is small (<20)."""
    groups: list[list[tuple[Path, dict]]] = []
    for path, info in infos:
        d = info.get("duration", 0)
        placed = False
        for g in groups:
            ref = g[0][1].get("duration", 0)
            if abs(d - ref) <= tol_seconds:
                g.append((path, info))
                placed = True
                break
        if not placed:
            groups.append([(path, info)])
    return groups


async def triage_dir(target: str) -> FileTriage:
    """Walk the directory, decide what to delete and what to keep before mdcx scrape."""
    triage = FileTriage()
    base = Path(target)
    if not base.exists():
        triage.notes.append(f"target not found: {target}")
        return triage

    # 1. Disc archives short-circuit normal video triage. The merger remuxes the
    #    main playlist; whatever lives outside BDMV/VIDEO_TS still gets cleaned.
    disc = _detect_disc_archive(base)
    if disc is not None:
        triage.disc_archive = disc
        triage.notes.append(f"detected disc archive at {disc.name}")

    files = _list_files(base)
    videos: list[Path] = []
    samples: list[Path] = []

    for f in files:
        # Skip files inside the disc archive — they'll be handled by remux.
        if disc is not None and (str(f).startswith(str(disc / "BDMV"))
                                  or str(f).startswith(str(disc / "VIDEO_TS"))):
            continue

        ext = f.suffix.lower()
        name = f.name

        # Extras get tagged before junk/sample so they're preserved.
        if _is_extras_filename(name) and ext in VIDEO_EXTS:
            triage.extras.append(f)
            continue

        if ext in JUNK_EXTS:
            triage.delete_junk.append(f)
            continue
        if ext in VIDEO_EXTS:
            if _is_sample_filename(name):
                samples.append(f)
            else:
                videos.append(f)
            continue
        # images / nfo / unknown — leave for now (mdcx may need them)

    # 2. Sample / promo videos: small AND looks like sample
    for s in samples:
        try:
            sz_mib = s.stat().st_size / (1024 * 1024)
        except OSError:
            sz_mib = 0.0
        if sz_mib < 200:
            triage.delete_samples.append(s)
        else:
            # Looks like a "sample" by name but is large — be conservative, keep as video
            videos.append(s)
            triage.notes.append(f"kept large 'sample' file: {s.name} ({sz_mib:.0f}MiB)")

    # If the directory IS a disc archive, no point doing further per-file triage.
    if disc is not None:
        triage.keep_videos = []
        return triage

    if not videos:
        triage.notes.append("no main video file detected")
        return triage

    # 3. Single video — done.
    if len(videos) == 1:
        triage.keep_videos = videos
        return triage

    # 4. Multi-part (CD1/Part1/A/B etc.) — keep all, sorted by part index.
    if all(_has_part_marker(v.name) for v in videos):
        ordered = sorted(videos, key=lambda p: (_part_index(p.name) or 999, p.name.lower()))
        triage.multipart_parts = ordered
        triage.keep_videos = ordered
        triage.notes.append(f"detected multi-part release ({len(videos)} parts)")
        return triage

    # If only SOME files have part markers, the others are probably random
    # extras / samples mdcx would trip over — split them: keep the parts, leave
    # the rest to dedupe.
    parted = [v for v in videos if _has_part_marker(v.name)]
    unparted = [v for v in videos if not _has_part_marker(v.name)]
    if parted and unparted:
        triage.multipart_parts = sorted(
            parted, key=lambda p: (_part_index(p.name) or 999, p.name.lower())
        )
        triage.notes.append(
            f"mixed: {len(parted)} part(s) + {len(unparted)} extra video(s); "
            f"will dedupe extras"
        )
        videos = unparted  # fall through to dedupe on the unparted set

    # 5. Multiple videos, no part markers → likely multi-resolution dupes.
    #    ffprobe each, group by similar duration, keep highest resolution per group.
    infos: list[tuple[Path, dict]] = []
    for v in videos:
        info = await _video_metadata(v)
        infos.append((v, info))

    groups = _group_by_similar_duration(infos)

    keep: list[Path] = []
    delete: list[Path] = []
    for g in groups:
        if len(g) == 1:
            keep.append(g[0][0])
            continue
        ranked = sorted(g, key=lambda x: (
            -(x[1].get("height") or 0),
            -(x[1].get("bit_rate") or 0),
            -(x[1].get("size") or 0),
        ))
        keep.append(ranked[0][0])
        for p, _ in ranked[1:]:
            delete.append(p)

    if triage.multipart_parts:
        # Keep both the multipart parts (already in keep_videos via merger) and
        # whatever survived dedupe.
        triage.keep_videos = triage.multipart_parts + sorted(keep, key=lambda p: p.name.lower())
    else:
        triage.keep_videos = sorted(keep, key=lambda p: p.name.lower())
    triage.delete_dupes = delete
    if delete:
        triage.notes.append(
            f"multi-resolution group(s): kept {len(keep)}, dropped {len(delete)}"
        )
    return triage


def execute(triage: FileTriage, dry_run: bool = False) -> list[str]:
    """Apply triage decisions. Returns list of deletion log lines."""
    log_lines: list[str] = []
    for category, files in (
        ("junk",   triage.delete_junk),
        ("dupe",   triage.delete_dupes),
        ("sample", triage.delete_samples),
    ):
        for p in files:
            line = f"DELETE [{category}] {p}"
            if dry_run:
                log_lines.append(line + " (dry-run)")
                continue
            try:
                p.unlink()
                log_lines.append(line)
            except (PermissionError, OSError, FileNotFoundError) as e:
                log_lines.append(f"FAIL [{category}] {p}: {e}")
    return log_lines


def relocate_extras(triage: FileTriage, base: str, dry_run: bool = False) -> list[str]:
    """Move extras (花絮 / 特典 / making-of / bonus) into Extras/ subfolder.

    Jellyfin & Emby pick up extras from a child folder named `Extras` (or by
    `-behindthescenes` suffix). Folder approach is simplest and survives
    re-scrape without name fights.
    """
    if not triage.extras:
        return []
    extras_dir = Path(base) / "Extras"
    log_lines: list[str] = []
    if dry_run:
        log_lines.append(f"MKDIR Extras/ (dry-run)")
        for src in triage.extras:
            log_lines.append(f"MOVE [extra] {src.name} → Extras/ (dry-run)")
        return log_lines

    try:
        extras_dir.mkdir(exist_ok=True)
    except (PermissionError, OSError) as e:
        log_lines.append(f"FAIL mkdir Extras/: {e}")
        return log_lines

    for src in triage.extras:
        dest = extras_dir / src.name
        try:
            src.rename(dest)
            log_lines.append(f"MOVE [extra] {src.name} → Extras/")
        except (PermissionError, OSError, FileNotFoundError) as e:
            log_lines.append(f"FAIL [extra] {src.name}: {e}")
    return log_lines


# ---------------------------------------------------------------------------
# Post-mdcx cleanup
# ---------------------------------------------------------------------------


def post_mdcx_cleanup(target: str, dry_run: bool = False) -> list[str]:
    """After mdcx scrape, delete any leftover junk that mdcx didn't reference.

    Conservative: only deletes files whose extension is in JUNK_EXTS or that look
    like sample. Doesn't touch nfo/images (mdcx writes those). Never touches
    files in an Extras/ subfolder.
    """
    base = Path(target)
    if not base.exists():
        return []
    log_lines: list[str] = []
    for f in _list_files(base):
        # Skip extras subfolder entirely.
        if any(part.lower() == "extras" for part in f.relative_to(base).parts):
            continue
        # Skip extras-named files that somehow ended up at the root (defensive).
        if _is_extras_filename(f.name):
            continue

        ext = f.suffix.lower()
        is_junk_ext = ext in JUNK_EXTS
        is_sample_video = (ext in VIDEO_EXTS) and _is_sample_filename(f.name)
        if not (is_junk_ext or is_sample_video):
            continue
        line = f"DELETE [post-mdcx] {f}"
        if dry_run:
            log_lines.append(line + " (dry-run)")
            continue
        try:
            f.unlink()
            log_lines.append(line)
        except (PermissionError, OSError, FileNotFoundError) as e:
            log_lines.append(f"FAIL [post-mdcx] {f}: {e}")
    return log_lines
