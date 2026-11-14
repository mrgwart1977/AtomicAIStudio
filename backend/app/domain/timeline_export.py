"""C5-B — timeline export planning (pure logic, no process I/O).

Path decision, export profiles, and FFmpeg argv construction live
here so the adapter in adapters/timeline_export.py stays a thin
supervised-execution wrapper.

Path rules (deliberately strict):

- STREAM COPY only when every clip is byte-compatible: same codec,
  width, height, fps (rational), pix_fmt, stream timebase, no VFR,
  and identical audio layout — AND no clip is trimmed. Trimmed cuts
  land on packet boundaries under concat-copy, not frame edges, so
  any trim forces the encode path. Copy never "normalizes" anything.
- RE-ENCODE for everything else: per-segment trim → scale → pad →
  setsar → fps-normalize, then filter-concat at the timeline
  timebase. Audio: all-silent timelines stay silent; partial audio
  gets lavfi silence fill so concat a=1 is well-formed.

Frame-accurate trims only exist on the encode path.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

#: Validated initial profile set.
PROFILES: dict[str, dict[str, Any]] = {
    "source": {"width": None, "height": None,
               "note": "native dims/fps per source — copy if compatible"},
    "1080p": {"width": 1920, "height": 1080,
              "note": "fit inside 1920x1080, pad to exact even dims"},
    "720p": {"width": 1280, "height": 720,
             "note": "fit inside 1280x720, pad to exact even dims"},
}
CRF_MIN, CRF_MAX, CRF_DEFAULT = 14, 32, 23
#: Rough output-size estimates for disk preflight (bits/sec × dur).
_PROFILE_BITRATE = {"source": 8_000_000, "1080p": 8_000_000,
                    "720p": 5_000_000}


@dataclass(frozen=True)
class ClipSpec:
    """Normalized copy-compat descriptor for one clip's pinned source."""
    codec: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    pix_fmt: str
    time_base: str
    vfr: bool
    audio: tuple  # ((codec, rate, channels, layout, tbase), ...)


def clip_spec(probe: dict[str, Any]) -> ClipSpec:
    audio = tuple(
        (a.get("codec", ""), a.get("sample_rate", 0), a.get("channels", 0),
         a.get("channel_layout", ""), a.get("time_base", ""))
        for a in (probe.get("audio") or []))
    num, den = _fps_rational(probe.get("fps") or 0)
    return ClipSpec(
        codec=probe.get("codec") or "", width=probe.get("width") or 0,
        height=probe.get("height") or 0, fps_num=num, fps_den=den,
        pix_fmt=probe.get("pix_fmt") or "",
        time_base=probe.get("time_base") or "",
        vfr=bool(probe.get("vfr")), audio=audio)


def _fps_rational(fps: float) -> tuple[int, int]:
    f = Fraction(float(fps)).limit_denominator(1001)
    return (f.numerator or 1, f.denominator or 1)


def untrimmed(clip: dict) -> bool:
    return (clip["in_frame"] == 0
            and clip["out_frame"] == (clip.get("source") or {})
            .get("frames"))


def choose_path(edl: dict, probes: list[dict]) -> tuple[str, str]:
    """(path, reason) — 'copy' only under the strict rules above."""
    clips = edl.get("clips") or []
    if not clips:
        return "encode", "empty timeline"
    if len(clips) != len(probes):
        return "encode", "missing probe data"
    specs = [clip_spec(p) for c, p in zip(clips, probes)]
    if any(not s.codec for s in specs):
        return "encode", "a source has no decodable video stream"
    first = specs[0]
    if any(s != first for s in specs):
        return "encode", "sources differ in codec/dims/fps/pixfmt/timebase/audio"
    if first.vfr:
        return "encode", "variable-frame-rate source needs normalization"
    if not all(untrimmed(c) for c in clips):
        return "copy-not-exact", \
            "trims requested — re-encoding for frame-exact cuts"
    return "copy", "all sources share one stream spec and are untrimmed"


def resolve_profile(edl: dict, profile: str) -> dict[str, Any]:
    """Concrete target geometry: {width, height, fps_num, fps_den}.
    'source' adopts the first clip's own dims + fps."""
    if profile not in PROFILES:
        raise ValueError(f"unknown export profile '{profile}'")
    tb = edl.get("timebase") or {"num": 24, "den": 1}
    spec = PROFILES[profile]
    first = (edl.get("clips") or [{}])[0].get("source") or {}
    if profile == "source":
        w = first.get("width") or 0
        h = first.get("height") or 0
        num = first.get("fps_num") or tb["num"]
        den = first.get("fps_den") or tb["den"]
    else:
        w, h = spec["width"], spec["height"]
        num, den = tb["num"], tb["den"]
    # h264 needs even dims — odd values round DOWN, never stretch.
    w -= w % 2
    h -= h % 2
    if w < 2 or h < 2:
        raise ValueError("export geometry is too small")
    return {"width": w, "height": h, "fps_num": int(num),
            "fps_den": int(den), "profile": profile}


def est_output_bytes(edl: dict, profile: str) -> int:
    """Conservative size estimate for disk preflight: bitrate × total
    duration + 20% container overhead + 64 MiB floor."""
    dur = float(edl.get("total_duration_s") or 0)
    rate = _PROFILE_BITRATE.get(profile, _PROFILE_BITRATE["source"])
    return int(dur * rate / 8 * 1.2) + 64 * 1024 * 1024


def _flt(v: float | int) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".") or "0"


_ENCODER_CACHE: dict[str, str] = {}

#: Software H.264 encoders in preference order — libx264 on full dev
#: builds, libopenh264 in the bundled LGPL ffmpeg. Hardware encoders
#: (nvenc/amf/mf) are deliberately NOT picked: output must be
#: deterministic across machines.
_H264_CANDIDATES = ("libx264", "libopenh264")


def h264_encoder(ffmpeg: str, runner) -> str:
    """First available software H.264 encoder for THIS binary.
    `ffmpeg -h encoder=X` exits 0 only when X is compiled in — bounded
    output, unlike `-encoders` whose listing overflows the capture
    tail."""
    if ffmpeg in _ENCODER_CACHE:
        return _ENCODER_CACHE[ffmpeg]
    for cand in _H264_CANDIDATES:
        r = runner([ffmpeg, "-hide_banner", "-h", f"encoder={cand}"],
                   timeout_s=15.0)
        # `-h encoder=X` exits 0 even when X isn't compiled — the
        # failure text is short and lands in the tail; a real
        # encoder's help is long (its "Encoder X" header is truncated
        # away) but never contains the rejection string.
        out = r.get("output_tail") or ""
        if "is not recognized" not in out:
            _ENCODER_CACHE[ffmpeg] = cand
            return cand
    raise ValueError(
        "no software H.264 encoder in the resolved ffmpeg "
        f"({ffmpeg}) — need libx264 or libopenh264")


def build_copy_argv(ffmpeg: str, list_path: str, out_path: str) -> list[str]:
    return [ffmpeg, "-y", "-hide_banner", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", out_path]


def concat_list(paths: list[str]) -> str:
    """Concat-demuxer list. CAS paths are sha-addressed (hex), so no
    quoting tricks can appear — escape ' anyway per the format."""
    lines = []
    for p in paths:
        esc = p.replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{esc}'")
    return "\n".join(lines) + "\n"


def build_encode_argv(ffmpeg: str, segments: list[dict],
                      target: dict, crf: int, out_path: str,
                      encoder: str = "libx264") -> list[str]:
    """One-pass filter_complex export. Each segment:
      [i:v] trim→setpts→scale(fit)→pad→setsar→fps  [i:a|anull] atrim→aformat
    then concat v=1 a=(0|1).

    `segments` items: {path, in_s, dur_s, has_audio}. Trims arrive as
    SECONDS computed from exact source-frame rationals — trim filter
    takes start/end times so the cut is frame-precise.
    """
    n = len(segments)
    any_audio = any(s["has_audio"] for s in segments)
    argv = [ffmpeg, "-y", "-hide_banner", "-v", "error"]
    for s in segments:
        argv += ["-i", s["path"]]
    silent_inputs: dict[int, int] = {}  # segment idx -> input idx
    if any_audio:
        for i, s in enumerate(segments):
            if not s["has_audio"]:
                idx = n + len(silent_inputs)
                silent_inputs[i] = idx
                argv += ["-f", "lavfi", "-t", _flt(s["dur_s"]),
                         "-i", "anullsrc=r=48000:cl=stereo"]
    parts = []
    for i, s in enumerate(segments):
        end = s["in_s"] + s["dur_s"]
        parts.append(
            f"[{i}:v]trim=start={_flt(s['in_s'])}:end={_flt(end)},"
            f"setpts=PTS-STARTPTS,"
            f"scale=w={target['width']}:h={target['height']}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={target['width']}:{target['height']}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={target['fps_num']}/{target['fps_den']}[v{i}]")
        if any_audio:
            a_in = silent_inputs.get(i, i)
            parts.append(
                f"[{a_in}:a]atrim=0:{_flt(s['dur_s'])},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
    if any_audio:
        pair = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{pair}concat=n={n}:v=1:a=1[v][a]")
    else:
        vin = "".join(f"[v{i}]" for i in range(n))
        parts.append(f"{vin}concat=n={n}:v=1:a=0[v]")
    argv += ["-filter_complex", ";".join(parts)]
    argv += ["-map", "[v]"]
    if any_audio:
        argv += ["-map", "[a]"]
    argv += ["-c:v", encoder, "-preset", "fast", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if any_audio:
        argv += ["-c:a", "aac", "-b:a", "160k"]
    argv += [out_path]
    return argv
