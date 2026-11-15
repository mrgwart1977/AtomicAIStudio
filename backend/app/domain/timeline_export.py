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


# ---------------- N1-D — multitrack composite ----------------
#
# Compositing rules (deterministic, documented):
# - Only `visible` lanes contribute pixels; only lanes that are BOTH
#   visible and unmuted contribute audio. Lanes holding no clips are
#   inert (nothing is silently dropped — they simply have no frames).
# - Lane stacking follows editor order: `position` ascending renders
#   top-to-bottom in the lane stack, so the LOWEST position composites
#   LAST (topmost picture). Chain order is position descending.
# - Each clip is scaled to FIT inside the canvas (aspect preserved)
#   and centered; surrounding canvas pixels from lower lanes show
#   through — there is no per-clip transform model yet, so nothing
#   invents geometry.
# - Timeline gaps and uncovered canvas render black (color= base).
# - Audio: each audio-bearing clip's source audio is trimmed to its
#   source range, delayed to its timeline start, and summed with a
#   full-length silent base via amix normalize=0 + limiter, so gaps
#   are silent, overlaps are mixed (never duplicated), and loud
#   overlaps limit rather than clip.
# - Output duration is exact: program_end_frames / timeline_fps as a
#   rational second count; the fps filter quantizes every input.

def composite_plan(edl: dict, probes: dict[str, dict]) -> dict:
    """Resolve an `ade-edl/2` document into render instructions.

    Returns {"clips": [{clip_id, sha256, track_position, tl_start_s,
    tl_end_s, in_s, dur_s, has_audio}], "program_frames": int,
    "fps_num": int, "fps_den": int}.
    `probes` maps clip_id -> probe metadata (for has_audio)."""
    tb = edl.get("timebase") or {"num": 24, "den": 1}
    tl_fps = Fraction(int(tb["num"]), int(tb["den"]))
    tracks = {t["id"]: t for t in edl.get("tracks") or []}
    out_clips: list[dict[str, Any]] = []
    end_frames = Fraction(0)
    for c in edl.get("clips") or []:
        t = tracks.get(c.get("track_id"))
        if t is None or not t.get("visible", True):
            continue  # hidden lanes contribute nothing
        src = c.get("source") or {}
        src_fps = Fraction(int(src.get("fps_num") or tb["num"]),
                           int(src.get("fps_den") or tb["den"]))
        in_f, out_f = int(c["in_frame"]), int(c["out_frame"])
        in_s = Fraction(in_f) / src_fps
        dur_s = Fraction(out_f - in_f) / src_fps
        extent = Fraction(c["timeline_frames"]["num"],
                          c["timeline_frames"]["den"])
        start = Fraction(int(c["start_frames"]))
        end_frames = max(end_frames, start + extent)
        out_clips.append({
            "clip_id": c["clip_id"], "sha256": c["sha256"],
            "track_position": int(t["position"]),
            "muted": bool(t.get("muted")),
            "tl_start_s": start / tl_fps,
            "tl_end_s": (start + extent) / tl_fps,
            "in_s": in_s, "dur_s": dur_s,
            "has_audio": bool((probes.get(c["clip_id"]) or {})
                              .get("audio")),
        })
    # Bottom lane (highest position) first; same-lane ties by start.
    out_clips.sort(key=lambda c: (-c["track_position"],
                                  c["tl_start_s"], c["clip_id"]))
    return {"clips": out_clips, "program_frames": end_frames,
            "fps_num": tl_fps.numerator, "fps_den": tl_fps.denominator}


def composite_canvas(plan: dict, profile: str,
                     probes: dict[str, dict]) -> dict[str, Any]:
    """Canvas geometry for a composite. Fixed profiles keep their
    dims; 'source' = max source dims across contributing clips
    (even-rounded), timeline timebase for fps either way."""
    if profile not in PROFILES:
        raise ValueError(f"unknown export profile '{profile}'")
    num, den = plan["fps_num"], plan["fps_den"]
    if profile == "source":
        w = h = 0
        for c in plan["clips"]:
            meta = probes.get(c["clip_id"]) or {}
            w = max(w, int(meta.get("width") or 0))
            h = max(h, int(meta.get("height") or 0))
    else:
        w, h = PROFILES[profile]["width"], PROFILES[profile]["height"]
    w -= w % 2
    h -= h % 2
    if w < 2 or h < 2:
        raise ValueError("export geometry is too small")
    return {"width": w, "height": h, "fps_num": num, "fps_den": den,
            "profile": profile}


def build_composite_argv(ffmpeg: str, plan: dict, paths: dict[str, str],
                         canvas: dict, crf: int, out_path: str,
                         encoder: str = "libx264") -> list[str]:
    """One-pass multitrack render. `paths` maps clip_id -> resolved
    source file (pinned-sha CAS path). Graph:

      color=black:WxH:fps:d=DUR [base]
      per clip: [i:v] trim→setpts→fps→scale(fit) [c{i}]
      chain:    [base][c0]overlay=center+eof_action=pass [o0]
                [o_{k-1}][c{k}]overlay=... [v]
      audio:    anullsrc base + per-clip atrim→adelay → amix
                (duration=first, normalize=0) → alimiter [a]
    """
    W, H = canvas["width"], canvas["height"]
    fps = f"{canvas['fps_num']}/{canvas['fps_den']}"
    dur = plan["program_frames"] / Fraction(canvas["fps_num"],
                                            canvas["fps_den"])
    dur_s = _flt(float(dur))
    clips = plan["clips"]
    argv = [ffmpeg, "-y", "-hide_banner", "-v", "error"]
    for c in clips:
        argv += ["-i", paths[c["clip_id"]]]
    parts = [
        f"color=c=black:s={W}x{H}:r={fps}:d={dur_s},format=yuv420p[base]"]
    cur = "base"
    for i, c in enumerate(clips):
        in_s = float(c["in_s"])
        end_s = float(c["in_s"] + c["dur_s"])
        parts.append(
            f"[{i}:v]trim=start={_flt(in_s)}:end={_flt(end_s)},"
            f"setpts=PTS-STARTPTS,"
            f"fps={fps},"
            f"scale=w={W}:h={H}:force_original_aspect_ratio=decrease"
            f"[c{i}]")
        nxt = "v" if i == len(clips) - 1 else f"o{i}"
        parts.append(
            f"[{cur}][c{i}]overlay=x=(main_w-overlay_w)/2:"
            f"y=(main_h-overlay_h)/2:eof_action=pass:"
            f"enable='between(t,{_flt(float(c['tl_start_s']))},"
            f"{_flt(float(c['tl_end_s']))})'[{nxt}]")
        cur = nxt
    audio_inputs = [i for i, c in enumerate(clips)
                    if c["has_audio"] and not c["muted"]]
    if audio_inputs:
        a_parts = [f"anullsrc=r=48000:cl=stereo,atrim=0:{dur_s}[abase]"]
        mix_in = "[abase]"
        for j, i in enumerate(audio_inputs):
            c = clips[i]
            in_s = float(c["in_s"])
            end_s = float(c["in_s"] + c["dur_s"])
            delay_ms = int(c["tl_start_s"] * 1000)
            a_parts.append(
                f"[{i}:a]atrim=start={_flt(in_s)}:end={_flt(end_s)},"
                f"asetpts=PTS-STARTPTS,aresample=48000,"
                f"aformat=channel_layouts=stereo,"
                f"adelay={delay_ms}:all=1[am{j}]")
            mix_in += f"[am{j}]"
        a_parts.append(
            f"{mix_in}amix=inputs={len(audio_inputs) + 1}:"
            f"duration=first:normalize=0,alimiter=limit=0.95[a]")
        parts += a_parts
    argv += ["-filter_complex", ";".join(parts)]
    argv += ["-map", f"[{cur}]"]
    if audio_inputs:
        argv += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    argv += ["-c:v", encoder, "-preset", "fast", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-t", dur_s, out_path]
    return argv


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
