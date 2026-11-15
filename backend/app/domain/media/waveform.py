"""N2-B — waveform envelope generation (pure logic + ffmpeg decode).

A waveform is DERIVED data: deterministic min/max peaks per bucket per
channel, computed from the pinned source bytes and stored as CAS blobs
next to — never instead of — the original audio.

Design contract:
- BUCKET GRID IS ABSOLUTE: bucket b covers source samples
  [b*spb, (b+1)*spb) at the source sample rate — a fixed lattice, so
  any clip's [in_frame, out_frame) trims map to bucket ranges with
  plain arithmetic. No per-clip offsets, no drift.
- LEVELS: samples-per-bucket grows ×4 from a base chosen so the
  coarsest useful level has ≥64 buckets and the finest has ≤ 250k
  (storage bound ~7 MB JSON worst case, typically far less).
- PEAKS: signed 16-bit min/max pairs per channel per bucket — the
  smallest lossless rendering primitive for s16 sources; decoded
  float formats are quantized to s16 by the decoder command.
- DETERMINISTIC: same source bytes → same bytes out (fixed JSON
  serialization, no floats, no timestamps).
- BOUNDED MEMORY: ffmpeg decodes to a temp s16le file; the envelope
  pass streams it in fixed chunks — a 3-hour file never enters RAM.
"""
from __future__ import annotations

import array
import json
import threading
from pathlib import Path
from typing import Any

from app.domain.media import binaries, supervise

#: Level lattice. Base samples-per-bucket; each next level is ×4.
BASE_SPB = 256
LEVEL_RATIO = 4
MAX_LEVELS = 6
#: Skip levels that would hold fewer buckets than this (a coarser
#: level already shows everything on one screen).
MIN_USEFUL_BUCKETS = 64
#: And cap the finest level so a very long source can't produce an
#: unbounded blob — finer detail is still available by re-bucketing,
#: but the stored doc stays bounded.
MAX_FINEST_BUCKETS = 250_000

_PCM_CHUNK = 1 << 20  # 1 MiB streaming reads


def level_ladder(samples: int, *, base_spb: int = BASE_SPB) -> list[int]:
    """Samples-per-bucket ladder for a source of `samples` samples.

    Deterministic: the finest level is the smallest power-of-2-scaled
    `base_spb` that keeps its bucket count under MAX_FINEST_BUCKETS;
    coarser levels multiply by LEVEL_RATIO while the previous level
    still had ≥ MIN_USEFUL_BUCKETS buckets."""
    if samples <= 0:
        return []
    spb = base_spb
    while (samples + spb - 1) // spb > MAX_FINEST_BUCKETS:
        spb *= LEVEL_RATIO
    ladder = [spb]
    while len(ladder) < MAX_LEVELS:
        prev_buckets = (samples + spb - 1) // spb
        if prev_buckets < MIN_USEFUL_BUCKETS:
            break
        spb *= LEVEL_RATIO
        ladder.append(spb)
    return ladder


def decode_pcm_s16le(path: Path, out_path: Path, channels: int, *,
                     timeout_s: float = 600.0,
                     cancel: threading.Event | None = None
                     ) -> dict[str, Any]:
    """Decode the FIRST audio stream to raw little-endian s16 at
    `channels` — matches probe_audio's authoritative stream. Output is
    a temp file (no giant stdout capture); the envelope pass streams
    it. `-xerror` fails on any decode error."""
    tool = binaries.ffmpeg()
    if tool is None:
        return {"ok": False, "reason": "ffmpeg unavailable"}
    r = supervise.run(
        [tool["path"], "-nostdin", "-v", "error", "-xerror",
         "-i", str(path), "-map", "0:a:0", "-ac", str(channels),
         "-f", "s16le", "-acodec", "pcm_s16le", "-y", str(out_path)],
        timeout_s=timeout_s, cancel=cancel)
    if r["timed_out"]:
        return {"ok": False, "reason": "decode timed out"}
    if r["cancelled"]:
        return {"ok": False, "reason": "decode cancelled",
                "cancelled": True}
    if r["exit"] != 0:
        tail = (r["output_tail"] or "").strip()
        return {"ok": False,
                "reason": f"decode failed (exit {r['exit']}): "
                          f"{tail[-300:]}" if tail
                else f"decode failed (exit {r['exit']})"}
    return {"ok": True, "bytes": out_path.stat().st_size}


def envelope_level(pcm_path: Path, *, samples: int, channels: int,
                   spb: int,
                   cancel: threading.Event | None = None
                   ) -> dict[str, Any]:
    """Stream `pcm_path` (s16le interleaved) and emit the bucket grid:
    {"spb", "buckets", "channels", "peaks": [[ [mn,mx], ... ] per ch]}.

    Memory-bounded: the file is consumed in windows of whole buckets
    (~1 MiB); within a window each channel is de-interleaved once via
    strided array slicing and min/max are C-speed — no per-sample
    Python loop. `samples` is the authoritative count — trailing
    partial frames in the pcm file (shouldn't exist) are ignored, and
    missing tail samples pad the last bucket with zeros only to the
    bucket's own bound (the count is never invented).
    """
    frame_size = channels * 2
    # Window = a whole number of buckets around _PCM_CHUNK bytes.
    bpt = spb * frame_size                     # bytes per bucket
    win_buckets = max(1, _PCM_CHUNK // bpt)
    win_bytes = win_buckets * bpt
    peaks: list[list[list[int]]] = [
        [] for _ in range(channels)]
    idx = 0  # absolute sample index consumed so far
    with open(pcm_path, "rb") as f:
        while idx < samples:
            if cancel is not None and cancel.is_set():
                return {"ok": False, "reason": "envelope cancelled",
                        "cancelled": True}
            block = f.read(win_bytes)
            if not block:
                break
            # Whole frames only; a ragged tail belongs to the next read
            # — but reads are frame-aligned by construction, so any
            # ragged tail here is genuinely corrupt data: drop it.
            n = min(len(block) // frame_size, samples - idx)
            if n <= 0:
                break
            data = block[: n * frame_size]
            arr = array.array("h")
            arr.frombytes(data)
            chans = [arr[ch::channels] for ch in range(channels)]
            done = 0
            while done < n:
                take = min(spb, n - done)
                for ch in range(channels):
                    seg = chans[ch][done: done + take]
                    peaks[ch].append([min(seg), max(seg)])
                done += take
            idx += n
    buckets = (samples + spb - 1) // spb
    # Decoded stream shorter than `samples` — pad trailing buckets as
    # silence rather than inventing peaks.
    for ch in range(channels):
        while len(peaks[ch]) < buckets:
            peaks[ch].append([0, 0])
    return {"ok": True, "spb": spb, "buckets": buckets,
            "channels": channels, "peaks": peaks}


def decimate_level(level: dict[str, Any], *, ratio: int = LEVEL_RATIO
                   ) -> dict[str, Any]:
    """The next-coarser level: groups of `ratio` fine buckets collapse
    to one coarse bucket by min-of-mins / max-of-maxes. Bucket edges
    are absolute, so a partial trailing fine group is exactly the
    coarse bucket's true range — no padding needed."""
    out_peaks = []
    for ch in level["peaks"]:
        coarse = []
        for i in range(0, len(ch), ratio):
            grp = ch[i:i + ratio]
            coarse.append([
                min(p[0] for p in grp), max(p[1] for p in grp)])
        out_peaks.append(coarse)
    return {"ok": True, "spb": level["spb"] * ratio,
            "buckets": len(out_peaks[0]) if out_peaks else 0,
            "channels": level["channels"], "peaks": out_peaks}


def level_document(level: dict[str, Any], *, rate: int,
                   source_sha256: str) -> bytes:
    """Deterministic JSON serialization of one level — same inputs,
    byte-identical output (no floats, sorted keys)."""
    doc = {
        "format": "ade-wave-level/1",
        "spb": level["spb"],
        "buckets": level["buckets"],
        "channels": level["channels"],
        "sample_rate": rate,
        "source_sha256": source_sha256,
        "peaks": level["peaks"],
    }
    return json.dumps(doc, separators=(",", ":"), sort_keys=True
                      ).encode("utf-8")


def build_waveform(source_path: Path, *, rate: int, channels: int,
                  samples: int, work_dir: Path,
                  source_sha256: str, stem: str = "wave",
                  cancel: threading.Event | None = None
                  ) -> dict[str, Any]:
    """Full pipeline: decode → per-level envelopes → level documents.
    Returns {"ok", "levels": [{spb, buckets, sha256?, data(bytes)}]}
    or {"ok": False, "reason"}. Caller ingests the docs into CAS so
    this stays I/O-pure apart from the temp decode file."""
    work_dir.mkdir(parents=True, exist_ok=True)
    pcm = work_dir / f"{stem}-{source_sha256[:16]}.s16le"
    try:
        dec = decode_pcm_s16le(source_path, pcm, channels,
                               cancel=cancel)
        if not dec.get("ok"):
            return {"ok": False,
                    "reason": dec.get("reason") or "decode failed",
                    "cancelled": dec.get("cancelled", False)}
        levels_out = []
        ladder = level_ladder(samples)
        if not ladder:
            return {"ok": False, "reason": "zero-length audio"}
        # Envelope once at the finest spb, then pyramid down — each
        # coarser level is a 4-way min/max fold of the level below,
        # so the PCM is scanned exactly once regardless of LOD count.
        env = envelope_level(pcm, samples=samples,
                             channels=channels, spb=ladder[0],
                             cancel=cancel)
        if not env.get("ok"):
            return {"ok": False,
                    "reason": env.get("reason") or "envelope failed",
                    "cancelled": env.get("cancelled", False)}
        for spb in ladder:
            if env["spb"] != spb:
                env = decimate_level(env)
            doc = level_document(env, rate=rate,
                                 source_sha256=source_sha256)
            levels_out.append({"spb": spb, "buckets": env["buckets"],
                               "data": doc})
        return {"ok": True, "levels": levels_out}
    finally:
        pcm.unlink(missing_ok=True)
