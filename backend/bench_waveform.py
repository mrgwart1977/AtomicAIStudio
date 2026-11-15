"""N2-B waveform benchmark — decode+envelope timings, level sizes,
peak RSS. Run: .venv\\Scripts\\python.exe bench_waveform.py [out.json]
"""
import json
import os
import struct
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.domain.media import engine  # noqa: E402
from app.domain.media.waveform import (  # noqa: E402
    build_waveform, level_ladder)


def _synth_wav(path: Path, *, rate: int, channels: int,
               seconds: float) -> Path:
    """Deterministic tone without ffmpeg: square ±8192, period 128."""
    n = int(rate * seconds)
    block = (struct.pack("<h", 8192) * channels * 64
             + struct.pack("<h", -8192) * channels * 64)
    with open(path, "wb") as f:
        data_len = n * channels * 2
        f.write(struct.pack(
            "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_len, b"WAVE",
            b"fmt ", 16, 1, channels, rate, rate * channels * 2,
            channels * 2, 16, b"data", data_len))
        full, rem = divmod(n, 128)
        for _ in range(full):
            f.write(block)
        for i in range(rem):
            v = 8192 if (i // 64) % 2 == 0 else -8192
            f.write(struct.pack("<h", v) * channels)
    return path


def bench_one(tmp: Path, label: str, *, rate: int, channels: int,
              seconds: float) -> dict:
    wav = _synth_wav(tmp / f"{label}.wav", rate=rate,
                     channels=channels, seconds=seconds)
    wav_mb = wav.stat().st_size / (1024 * 1024)
    probe = engine.probe_audio(wav)
    assert probe["ok"], probe

    # Two passes: the first is clean-timed (no instrumentation);
    # the second runs under tracemalloc to report Python-side peak
    # allocation — tracing inflates wall time heavily, so it never
    # feeds the timing numbers. ffmpeg's RSS is external (bounded by
    # its demux/decode buffers, not file length).
    t0 = time.perf_counter()
    out = build_waveform(
        wav, rate=probe["sample_rate"], channels=probe["channels"],
        samples=probe["samples"], work_dir=tmp / "w",
        source_sha256="bench")
    wall = time.perf_counter() - t0
    assert out["ok"], out

    tracemalloc.start()
    out2 = build_waveform(
        wav, rate=probe["sample_rate"], channels=probe["channels"],
        samples=probe["samples"], work_dir=tmp / "w2",
        source_sha256="bench")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert out2["ok"], out2

    levels = [
        {"spb": l["spb"], "buckets": l["buckets"],
         "bytes": len(l["data"])}
        for l in out["levels"]]
    total_blob = sum(l["bytes"] for l in levels)
    return {
        "label": label, "rate": rate, "channels": channels,
        "seconds": seconds, "samples": probe["samples"],
        "wav_mb": round(wav_mb, 2),
        "pcm_mb": round(wav_mb, 2) if probe["codec"].startswith("pcm")
        else None,
        "wall_s": round(wall, 3),
        "mb_per_s": round(wav_mb / wall, 1) if wall > 0 else None,
        "py_peak_mb": round(peak / (1024 * 1024), 2),
        "levels": levels,
        "total_blob_bytes": total_blob,
        "blob_vs_wav_pct": round(100 * total_blob /
                                 wav.stat().st_size, 1),
    }


def main() -> None:
    cases = [
        ("short-2s-stereo", dict(rate=48000, channels=2, seconds=2)),
        ("short-2s-44k-mono", dict(rate=44100, channels=1, seconds=2)),
        ("med-5min-stereo", dict(rate=48000, channels=2, seconds=300)),
        ("long-1h-stereo", dict(rate=48000, channels=2, seconds=3600)),
        ("long-3h-stereo", dict(rate=48000, channels=2, seconds=10800)),
        ("med-96k-stereo", dict(rate=96000, channels=2, seconds=300)),
    ]
    only = sys.argv[2:] if len(sys.argv) > 2 else None
    results = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for label, kw in cases:
            if only and label not in only:
                continue
            r = bench_one(tmp, label, **kw)
            results.append(r)
            print(f"{label}: {r['wall_s']}s, "
                  f"{len(r['levels'])} levels, "
                  f"blob {r['total_blob_bytes']/1024:.0f} KiB "
                  f"({r['blob_vs_wav_pct']}% of wav), "
                  f"peak {r['py_peak_mb']} MB", flush=True)
            # level_ladder sanity for the report
            r["ladder"] = level_ladder(r["samples"])
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "waveform-bench.json")
    out_path.write_text(json.dumps(
        {"kind": "n2b-waveform-bench", "cases": results}, indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
