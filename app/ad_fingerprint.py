"""Audio-fingerprint based advertisement detection + removal.

Pirate JAV/AV releases frequently splice a short promo / ad clip onto the
**head** (occasionally tail) of the real video — and the *same* clip is reused
across many releases from the same source (manko.fun / 1024 / one2048 / uunXX
families). This module fingerprints known ad clips and scans new downloads for
a matching run, then cuts it losslessly with ``ffmpeg -c copy``.

Design notes
------------
* **Audio, not video** — the pirate source reuses the same audio bed even when
  the picture is re-encoded / re-watermarked / re-scaled, so an audio hash is far
  more robust than a video hash.
* **Haitsma-Kalker robust hash** — 32-bit-per-frame sub-band-energy sign hash
  (Philips 2002). Computed from an 8 kHz mono downmix so it survives bitrate /
  container / resolution changes. Matching is by bit-error-rate (BER); the paper
  puts the "same content" boundary around BER ~0.35 (unrelated content sits near
  the random 0.5). Measured margin on a heavily triple-transcoded clip: true
  match ~0.33, unrelated ~0.48.
* **Pure Python** (no numpy/scipy) — keeps the service's embedded interpreter
  dependency-free. The only external tool is ffmpeg, which the pipeline already
  requires. A 1024-pt iterative radix-2 FFT + a 16-bit popcount table keep it
  fast enough for a background step (~1 s per 8 s ad, <1 s to scan a short head).

* **Lossless cut** — trims are keyframe-snapped (first keyframe at/after the ad
  end) and executed with ``-c copy`` (no re-encode, no quality loss). For a real
  spliced ad the splice point *is* a keyframe, so the cut is exact; for a fully
  re-encoded continuous stream it snaps forward to the next keyframe (drops the
  whole ad plus at most one GOP of content). Original is kept as ``.preadcut.bak``.

Fingerprint DB is a JSON file (``ad_fingerprints.json``) resolved next to
``state.db`` — i.e. relative to the service CWD (the install dir).

CLI
---
    python -m app.ad_fingerprint add  <name> <video> [--start S] [--dur D] [--note ...]
    python -m app.ad_fingerprint list
    python -m app.ad_fingerprint del  <name>
    python -m app.ad_fingerprint scan <video> [--head SECONDS] [--tail SECONDS]
    python -m app.ad_fingerprint cut  <video> [--head SECONDS] [--apply] [--no-backup]
"""
from __future__ import annotations

import argparse
import array
import cmath
import json
import logging
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fingerprint parameters (must stay constant — DB entries are keyed to them)
# ---------------------------------------------------------------------------
SR = 8000            # downmix sample rate (Hz)
FRAME = 1024         # FFT window (power of 2)
HOP = 512            # 50% overlap
BANDS = 33           # → 32-bit hash per frame
FMIN, FMAX = 300.0, 2000.0   # log-spaced band range (speech/music energy)

DEFAULT_HEAD_SEC = 150       # how much of the file head to scan by default
MATCH_BER = 0.36             # accept a match below this bit-error-rate (paper ~0.35)
STRONG_BER = 0.28            # "confident" threshold
MIN_AD_SEC = 3.0             # ignore matches shorter than this
HEAD_TOLERANCE_SEC = 8.0     # an ad must start within this of t=0 to be head-cut


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe location (mirrors qc.py's fallback strategy)
# ---------------------------------------------------------------------------
def _bin(name: str) -> str:
    for cand in (name, f"{name}.exe"):
        p = shutil.which(cand)
        if p:
            return p
    for cand in (
        rf"C:\Program Files\Jellyfin\Server\{name}.exe",
        rf"C:\Program Files\ffmpeg\bin\{name}.exe",
        rf"C:\ffmpeg\bin\{name}.exe",
    ):
        if os.path.exists(cand):
            return cand
    return name  # let it fail loudly if truly absent


FFMPEG = _bin("ffmpeg")
FFPROBE = _bin("ffprobe")


# ---------------------------------------------------------------------------
# DB location
# ---------------------------------------------------------------------------
def _db_path() -> Path:
    try:
        from .config import settings  # type: ignore
        state = Path(settings.state_db)
        base = state.parent if state.is_absolute() else Path.cwd()
    except Exception:
        base = Path.cwd()
    return (base / "ad_fingerprints.json").resolve()


# ---------------------------------------------------------------------------
# Iterative radix-2 FFT (fixed size, precomputed twiddles + bit reversal)
# ---------------------------------------------------------------------------
def _make_fft(n: int):
    levels = n.bit_length() - 1
    assert 1 << levels == n, "FFT size must be a power of 2"
    rev = [0] * n
    for i in range(n):
        r, x = 0, i
        for _ in range(levels):
            r = (r << 1) | (x & 1)
            x >>= 1
        rev[i] = r
    tw = [cmath.exp(-2j * cmath.pi * k / n) for k in range(n // 2)]

    def fft(a: list[complex]) -> list[complex]:
        out = [a[rev[i]] for i in range(n)]
        size = 2
        while size <= n:
            half = size // 2
            step = n // size
            for start in range(0, n, size):
                k = 0
                for i in range(start, start + half):
                    t = tw[k] * out[i + half]
                    u = out[i]
                    out[i] = u + t
                    out[i + half] = u - t
                    k += step
            size <<= 1
        return out

    return fft


_FFT = _make_fft(FRAME)
_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (FRAME - 1)) for i in range(FRAME)]


def _band_edges() -> list[int]:
    edges: list[int] = []
    for i in range(BANDS + 1):
        f = FMIN * (FMAX / FMIN) ** (i / BANDS)
        edges.append(int(round(f * FRAME / SR)))
    for i in range(1, len(edges)):
        if edges[i] <= edges[i - 1]:
            edges[i] = edges[i - 1] + 1
    return edges


_EDGES = _band_edges()

# 16-bit popcount table for fast Hamming distance
_POP = bytes(bin(i).count("1") for i in range(1 << 16))


def _popcount32(x: int) -> int:
    return _POP[x & 0xFFFF] + _POP[(x >> 16) & 0xFFFF]


# ---------------------------------------------------------------------------
# Audio extraction + fingerprint
# ---------------------------------------------------------------------------
def _extract_pcm(video: str, start: float, dur: Optional[float]) -> array.array:
    """Decode a mono 8 kHz s16le slice of ``video`` → signed-16 sample array."""
    cmd = [FFMPEG, "-v", "error", "-ss", f"{start}"]
    if dur is not None:
        cmd += ["-t", f"{dur}"]
    cmd += ["-i", video, "-vn", "-ac", "1", "-ar", str(SR),
            "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    samples = array.array("h")
    samples.frombytes(proc.stdout)
    return samples


def _fingerprint(samples: array.array) -> list[int]:
    """Haitsma-Kalker 32-bit-per-frame robust hash sequence."""
    n = len(samples)
    if n < FRAME:
        return []
    band_seq: list[list[float]] = []
    pos = 0
    edges = _EDGES
    fft = _FFT
    hann = _HANN
    while pos + FRAME <= n:
        frame = [samples[pos + i] * hann[i] for i in range(FRAME)]
        spec = fft([complex(v, 0.0) for v in frame])
        bands = []
        for b in range(BANDS):
            lo, hi = edges[b], edges[b + 1]
            e = 0.0
            for k in range(lo, hi):
                c = spec[k]
                e += c.real * c.real + c.imag * c.imag
            bands.append(e)
        band_seq.append(bands)
        pos += HOP
    hashes: list[int] = []
    for t in range(1, len(band_seq)):
        cur, prev = band_seq[t], band_seq[t - 1]
        h = 0
        for m in range(BANDS - 1):
            d = (cur[m] - cur[m + 1]) - (prev[m] - prev[m + 1])
            if d > 0:
                h |= (1 << m)
        hashes.append(h)
    return hashes


def fingerprint_video(video: str, start: float = 0.0, dur: Optional[float] = None) -> list[int]:
    return _fingerprint(_extract_pcm(video, start, dur))


def _frames_to_sec(frames: int) -> float:
    return frames * HOP / SR


def _sec_to_frames(sec: float) -> int:
    return int(round(sec * SR / HOP))


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
@dataclass
class Match:
    name: str
    ber: float
    start_sec: float
    end_sec: float


def _best_offset(ad_fp: list[int], hay: list[int]) -> Optional[tuple[int, float]]:
    la, lh = len(ad_fp), len(hay)
    if la == 0 or lh < la:
        return None
    bits = la * 32
    best_off, best_err = -1, bits + 1
    pc = _popcount32
    for o in range(0, lh - la + 1):
        err = 0
        limit = best_err
        for i in range(la):
            err += pc(ad_fp[i] ^ hay[o + i])
            if err >= limit:
                break
        else:
            best_off, best_err = o, err
    if best_off < 0:
        return None
    return best_off, best_err / bits


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
@dataclass
class AdSample:
    name: str
    fp: list[int]
    duration_sec: float
    note: str = ""
    source: str = ""


def _load_db() -> dict[str, AdSample]:
    path = _db_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    meta = raw.get("_meta", {})
    if meta.get("sr") not in (None, SR) or meta.get("frame") not in (None, FRAME):
        log.warning("ad_fingerprint DB was built with different params — ignoring")
        return {}
    out: dict[str, AdSample] = {}
    for name, d in raw.get("ads", {}).items():
        out[name] = AdSample(name=name, fp=d["fp"], duration_sec=d.get("duration_sec", 0.0),
                             note=d.get("note", ""), source=d.get("source", ""))
    return out


def _save_db(db: dict[str, AdSample]) -> None:
    path = _db_path()
    payload = {
        "_meta": {"sr": SR, "frame": FRAME, "hop": HOP, "bands": BANDS,
                  "fmin": FMIN, "fmax": FMAX, "version": 1},
        "ads": {a.name: {"fp": a.fp, "duration_sec": a.duration_sec,
                          "note": a.note, "source": a.source} for a in db.values()},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def add_ad(name: str, video: str, start: float = 0.0, dur: Optional[float] = None,
           note: str = "", source: str = "") -> AdSample:
    fp = fingerprint_video(video, start, dur)
    if len(fp) < _sec_to_frames(MIN_AD_SEC):
        raise ValueError(f"ad sample too short to fingerprint ({_frames_to_sec(len(fp)):.1f}s)")
    sample = AdSample(name=name, fp=fp, duration_sec=_frames_to_sec(len(fp)),
                      note=note, source=source)
    db = _load_db()
    db[name] = sample
    _save_db(db)
    log.info("added ad sample %r (%.1fs, %d frames)", name, sample.duration_sec, len(fp))
    return sample


def del_ad(name: str) -> bool:
    db = _load_db()
    if name in db:
        del db[name]
        _save_db(db)
        return True
    return False


def list_ads() -> list[AdSample]:
    return list(_load_db().values())


# ---------------------------------------------------------------------------
# Duration + keyframe probing
# ---------------------------------------------------------------------------
def _probe_duration(video: str) -> Optional[float]:
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", video],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
        return float(proc.stdout.strip())
    except Exception:
        return None


def _keyframes(video: str, a: float, b: float) -> list[float]:
    """Keyframe timestamps (seconds) in [a, b], via ffprobe -read_intervals."""
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0",
         "-read_intervals", f"{max(0.0, a)}%{b}", video],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace")
    out: list[float] = []
    for line in proc.stdout.splitlines():
        s = line.strip().rstrip(",")
        if s and s != "N/A":
            try:
                out.append(float(s))
            except ValueError:
                pass
    return sorted(out)


def _snap_keyframe(video: str, t: float) -> float:
    """First keyframe at/after ``t`` (within a small back-tolerance). Falls back
    to ``t`` if none found — so we never *keep* ad by snapping backwards."""
    if t <= 0.05:
        return t
    kfs = _keyframes(video, t - 12, t + 20)
    after = [k for k in kfs if k >= t - 0.25]
    return after[0] if after else t


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def scan(video: str, head_sec: float = DEFAULT_HEAD_SEC,
         tail_sec: float = 0.0) -> list[Match]:
    db = _load_db()
    if not db:
        return []
    matches: list[Match] = []
    head_fp = fingerprint_video(video, 0.0, head_sec)
    for ad in db.values():
        res = _best_offset(ad.fp, head_fp)
        if not res:
            continue
        off, ber = res
        if ber > MATCH_BER:
            continue
        s = _frames_to_sec(off)
        e = _frames_to_sec(off + len(ad.fp))
        if e - s < MIN_AD_SEC:
            continue
        matches.append(Match(name=ad.name, ber=ber, start_sec=s, end_sec=e))

    if tail_sec > 0:
        total = _probe_duration(video)
        if total and total > tail_sec:
            base = total - tail_sec
            tail_fp = fingerprint_video(video, base, tail_sec)
            for ad in db.values():
                res = _best_offset(ad.fp, tail_fp)
                if not res:
                    continue
                off, ber = res
                if ber > MATCH_BER:
                    continue
                matches.append(Match(name=f"{ad.name}(tail)", ber=ber,
                                     start_sec=base + _frames_to_sec(off),
                                     end_sec=base + _frames_to_sec(off + len(ad.fp))))

    matches.sort(key=lambda m: m.start_sec)
    return matches


# ---------------------------------------------------------------------------
# Cut
# ---------------------------------------------------------------------------
@dataclass
class CutPlan:
    cut_from: float = 0.0
    cut_to: Optional[float] = None
    reasons: list[str] = field(default_factory=list)

    @property
    def has_cut(self) -> bool:
        return self.cut_from > 0 or self.cut_to is not None


def plan_cut(video: str, head_sec: float = DEFAULT_HEAD_SEC) -> tuple[CutPlan, list[Match]]:
    """Decide a lossless head trim from detected ads. Only trims contiguous ads
    anchored at the head (t≈0) — never carves the middle (would need a re-encode
    and risks eating real content)."""
    matches = scan(video, head_sec=head_sec, tail_sec=0.0)
    plan = CutPlan()
    changed = True
    while changed:
        changed = False
        for m in matches:
            if m.start_sec <= plan.cut_from + HEAD_TOLERANCE_SEC and m.end_sec > plan.cut_from:
                plan.cut_from = m.end_sec
                plan.reasons.append(
                    f"head ad {m.name} @ {m.start_sec:.1f}-{m.end_sec:.1f}s (BER {m.ber:.2f})")
                changed = True
    return plan, matches


def apply_cut(video: str, plan: CutPlan, backup: bool = True) -> Optional[str]:
    """Execute the trim losslessly (-c copy), keyframe-snapped. Verifies the
    output duration before replacing. Returns new path or None."""
    if not plan.has_cut:
        return None
    src = Path(video)
    out = src.with_name(src.stem + ".deadv" + src.suffix)
    orig_dur = _probe_duration(video) or 0.0

    cut_from = _snap_keyframe(video, plan.cut_from)
    cmd = [FFMPEG, "-v", "error", "-y", "-ss", f"{cut_from}"]
    if plan.cut_to is not None:
        cmd += ["-to", f"{plan.cut_to}"]
    cmd += ["-i", video, "-c", "copy", "-map", "0",
            "-avoid_negative_ts", "make_zero", str(out)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out.exists():
        log.error("ad cut failed: %s", proc.stderr[:300])
        if out.exists():
            out.unlink()
        return None

    new_dur = _probe_duration(str(out)) or 0.0
    expected = orig_dur - cut_from - (0 if plan.cut_to is None else max(0.0, orig_dur - plan.cut_to))
    if new_dur < expected - 15 or new_dur > orig_dur + 2 or new_dur < 5:
        log.error("ad cut sanity fail: orig=%.0f new=%.0f expected≈%.0f — keeping original",
                  orig_dur, new_dur, expected)
        out.unlink()
        return None

    if backup:
        bak = src.with_suffix(src.suffix + ".preadcut.bak")
        shutil.move(str(src), str(bak))
    else:
        src.unlink()
    shutil.move(str(out), str(src))
    log.info("trimmed %.1fs of ads from head of %s (%.0f→%.0f s)",
             cut_from, src.name, orig_dur, new_dur)
    return str(src)


def scan_and_cut(video: str, head_sec: float = DEFAULT_HEAD_SEC,
                 apply: bool = False, backup: bool = True) -> dict:
    """High-level entry for the post-download pipeline."""
    plan, matches = plan_cut(video, head_sec=head_sec)
    result = {
        "video": video,
        "matches": [{"name": m.name, "ber": round(m.ber, 3),
                     "start": round(m.start_sec, 1), "end": round(m.end_sec, 1)} for m in matches],
        "cut_from": round(plan.cut_from, 1),
        "reasons": plan.reasons,
        "applied": False,
        "new_path": None,
    }
    if plan.has_cut and apply:
        newp = apply_cut(video, plan, backup=backup)
        result["applied"] = newp is not None
        result["new_path"] = newp
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(prog="ad_fingerprint")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("video")
    p_add.add_argument("--start", type=float, default=0.0)
    p_add.add_argument("--dur", type=float, default=None)
    p_add.add_argument("--note", default="")
    p_add.add_argument("--source", default="")

    sub.add_parser("list")

    p_del = sub.add_parser("del")
    p_del.add_argument("name")

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("video")
    p_scan.add_argument("--head", type=float, default=DEFAULT_HEAD_SEC)
    p_scan.add_argument("--tail", type=float, default=0.0)

    p_cut = sub.add_parser("cut")
    p_cut.add_argument("video")
    p_cut.add_argument("--head", type=float, default=DEFAULT_HEAD_SEC)
    p_cut.add_argument("--apply", action="store_true")
    p_cut.add_argument("--no-backup", action="store_true")

    args = ap.parse_args()

    if args.cmd == "add":
        s = add_ad(args.name, args.video, args.start, args.dur, args.note, args.source)
        print(f"OK added {s.name}: {s.duration_sec:.1f}s, {len(s.fp)} frames -> {_db_path()}")
    elif args.cmd == "list":
        ads = list_ads()
        print(f"{len(ads)} ad sample(s) in {_db_path()}:")
        for a in ads:
            print(f"  {a.name}: {a.duration_sec:.1f}s ({len(a.fp)} frames) "
                  f"src={a.source or '-'} note={a.note or '-'}")
    elif args.cmd == "del":
        print("deleted" if del_ad(args.name) else "not found")
    elif args.cmd == "scan":
        ms = scan(args.video, args.head, args.tail)
        print(f"{len(ms)} match(es):")
        for m in ms:
            tag = "STRONG" if m.ber <= STRONG_BER else "match"
            print(f"  [{tag}] {m.name}: {m.start_sec:.1f}-{m.end_sec:.1f}s  BER={m.ber:.3f}")
    elif args.cmd == "cut":
        res = scan_and_cut(args.video, args.head, apply=args.apply, backup=not args.no_backup)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
