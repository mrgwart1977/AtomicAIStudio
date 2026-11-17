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
from math import ceil as _ceil
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
# - Lane stacking follows the documented track model: `position` is
#   the stack order — 0 is the BOTTOM layer, higher positions
#   composite on top. Chain order is position ascending (bottom
#   first). The editor draws position 0 highest on screen — it is
#   the base layer, not the top picture.
# - Each clip is scaled to FIT inside the canvas (aspect preserved)
#   and centered; surrounding canvas pixels from lower lanes show
#   through — there is no per-clip transform model yet, so nothing
#   invents geometry.
# - Timeline gaps and uncovered canvas render black (color= base).
# - Audio: each audio-bearing clip's source audio is trimmed to its
#   source range and positioned at its timeline start by prepending
#   integer-sample silence (atrim=end_sample + concat — sample-exact,
#   no millisecond quantization), then summed with a full-length
#   silent base via amix normalize=0 + limiter, so gaps are silent,
#   overlaps are mixed once (never duplicated), and loud overlaps
#   limit rather than clip.
# - Output duration is exact: program_end_frames / timeline_fps as a
#   rational second count; the fps filter quantizes every input.
#
# N2-E — dedicated audio lanes render through the same graph:
# - Audibility (N2-C contract): visible + unmuted + in the solo set
#   when any lane is soloed; hidden/muted/non-solo lanes contribute
#   no audio (video lanes still show picture — solo is audio-only).
# - The effective-gain contract (docs/n2d-fades.md) is applied as one
#   multiplicative envelope per clip: clip gain + track gain folded
#   into a single dB sum, automation interpolated in dB at native
#   sample rate, fades and crossfade envelopes as afade anchors in
#   source units — nothing is applied twice.
# - Satisfied crossfades extend the pair's streams into their source
#   handles (out-clip tail → win_end, in-clip head → win_start) and
#   apply complementary fade envelopes over the window. A record
#   reporting satisfied=false on an AUDIBLE lane fails closed.
# - Quantization happens at exactly one documented point per value:
#   timeline frames → 48 kHz samples (or native-rate source units for
#   envelope anchors) via round-half-up on exact rationals.

AUDIO_RATE = 48000  # output sample rate (contract: initially 48 kHz)

#: afade curve names for the pinned fade-curve vocabulary: tri =
#: linear slope; qsin = quarter-sine — the sin/cos pair whose in+out
#: envelopes sum to unity power (the equal-power contract); hsin =
#: the sin² S-curve (gentle at both ends); exp = a fixed exponential
#: ramp. N2-F adds s_curve → hsin and exponential → exp.
_FADE_CURVE = {"linear": "tri", "equal_power": "qsin",
               "s_curve": "hsin", "exponential": "exp"}


def _q(v: Fraction) -> int:
    """Round-half-up of an exact rational — the single documented
    quantization rule for frame→sample conversions."""
    return int(v + Fraction(1, 2))


def _audible(track: dict, solo_set: set[str]) -> bool:
    """N2-C precedence: hidden → silent; muted → silent; a non-empty
    solo set silences everything outside it."""
    if not track.get("visible", True):
        return False
    if track.get("muted"):
        return False
    return not solo_set or track["id"] in solo_set


def composite_plan(edl: dict, probes: dict[str, dict]) -> dict:
    """Resolve an `ade-edl/2` document into render instructions.

    Returns {"clips": [video-composite entries],
             "audio_clips": [mixed-audio entries],
             "program_frames": Fraction, "fps_num": int, "fps_den": int}.
    `probes` maps clip_id -> probe metadata (has_audio, sample rates)."""
    tb = edl.get("timebase") or {"num": 24, "den": 1}
    tl_fps = Fraction(int(tb["num"]), int(tb["den"]))
    tracks = {t["id"]: t for t in edl.get("tracks") or []}
    solo_set = {t["id"] for t in tracks.values()
                if t.get("solo") and t.get("kind") != "overlay"}
    out_clips: list[dict[str, Any]] = []
    audio_clips: dict[str, dict[str, Any]] = {}
    end_frames = Fraction(0)
    for c in edl.get("clips") or []:
        t = tracks.get(c.get("track_id"))
        if t is None or not t.get("visible", True):
            continue  # hidden lanes contribute nothing at all
        src = c.get("source") or {}
        src_fps = Fraction(int(src.get("fps_num") or tb["num"]),
                           int(src.get("fps_den") or tb["den"]))
        in_f, out_f = int(c["in_frame"]), int(c["out_frame"])
        extent = Fraction(c["timeline_frames"]["num"],
                          c["timeline_frames"]["den"])
        start = Fraction(int(c["start_frames"]))
        end_frames = max(end_frames, start + extent)
        audible = _audible(t, solo_set)
        if t.get("kind") == "audio":
            probe = probes.get(c["clip_id"]) or {}
            astreams = probe.get("audio") or []
            # Fresh probe wins; validate-time falls back to the rate
            # pinned in the document at EDL generation.
            rate = int(probe.get("sample_rate") or 0) \
                or (int(astreams[0].get("sample_rate") or 0)
                    if astreams else 0) \
                or int(src.get("sample_rate") or 0)
            if audible:
                if rate <= 0:
                    raise ValueError(
                        f"audio clip {c['clip_id']} has no measured "
                        "sample rate — cannot place it sample-exactly")
                audio_clips[c["clip_id"]] = _audio_entry(
                    c, t, start, extent, rate)
            continue
        # video lane — picture composites; embedded audio joins the mix
        in_s = Fraction(in_f) / src_fps
        dur_s = Fraction(out_f - in_f) / src_fps
        occ_end = _ceil(start + extent)  # lane occupancy rule (ceil)
        probe = probes.get(c["clip_id"]) or {}
        has_audio = bool(probe.get("audio"))
        out_clips.append({
            "clip_id": c["clip_id"], "sha256": c["sha256"],
            "track_position": int(t["position"]),
            "muted": bool(t.get("muted")),
            "start_frame": int(start),
            "occ_end_frame": occ_end,
            "tl_start_s": start / tl_fps,
            "tl_end_s": (start + extent) / tl_fps,
            "in_s": in_s, "dur_s": dur_s,
            "has_audio": has_audio,
        })
        if has_audio and audible:
            audio_clips[c["clip_id"]] = {
                "clip_id": c["clip_id"], "sha256": c["sha256"],
                "embedded": True,
                "in_s": in_s, "dur_s": dur_s, "tl_start_s":
                    start / tl_fps,
                "start_tl": start,
                "track_db": float(t.get("gain_db") or 0.0),
                "clip_db": 0.0, "automation": [], "xf": None,
                "fade_in": None, "fade_out": None,
                # N3-A — lane automation DOES apply to embedded audio
                # (it's a lane-level envelope of timeline position,
                # not a clip edit); clip fades/automation/xf still
                # don't exist here.
                "track_auto": _track_pts(t),
                "track_auto_interp": str(t.get("automation_interp")
                                         or "linear"),
            }
    # Crossfades — only satisfied records on audible lanes extend the
    # pair's streams; an unsatisfied record there fails closed.
    for x in edl.get("crossfades") or []:
        out_c = audio_clips.get(x.get("out_clip_id"))
        in_c = audio_clips.get(x.get("in_clip_id"))
        if out_c is None and in_c is None:
            continue  # pair's lane is inaudible — nothing renders
        if out_c is None or in_c is None or not x.get("satisfied"):
            raise ValueError(
                "crossfade on an audible lane is no longer satisfied "
                "— undo the move or clear the record before export")
        if out_c["embedded"] or in_c["embedded"]:
            raise ValueError(
                "crossfade references a video-lane clip — the "
                "document is inconsistent")
        _apply_xf(x, out_c, in_c, tl_fps)
    # Bottom lane (lowest position) first; same-lane ties by start.
    out_clips.sort(key=lambda c: (c["track_position"],
                                  c["tl_start_s"], c["clip_id"]))
    return {"clips": out_clips,
            "audio_clips": list(audio_clips.values()),
            "program_frames": end_frames,
            "fps_num": tl_fps.numerator, "fps_den": tl_fps.denominator}


def _track_pts(t: dict) -> list[dict[str, Any]]:
    """N3-A — lane automation keyframes [{at_tl (Fraction timeline
    frames, ABSOLUTE), gain_db}]."""
    return [{"at_tl": Fraction(int(p["at_tl_frames"])),
             "gain_db": float(p["gain_db"])}
            for p in (t.get("automation") or [])]


def _audio_entry(c: dict, t: dict, start: Fraction, extent: Fraction,
                 rate: int) -> dict:
    """One dedicated-lane audio input: source segment + timeline
    placement + envelope params, all in exact units. `start`/`extent`
    are the clip's occupancy in timeline frames (Fractions)."""
    in_f, out_f = int(c["in_frame"]), int(c["out_frame"])
    fades = c.get("fades") or {}
    fin, fout = fades.get("in") or {}, fades.get("out") or {}
    auto = [{
        # keyframe offsets are clip-local occupancy tl-frames — the
        # stream-local source offset is resolved at argv build time
        # (after any xf head extension shifts the stream start).
        "off_tl": Fraction(int(p["at_frames"])),
        "gain_db": float(p["gain_db"])}
        for p in (c.get("automation") or [])]
    return {
        "clip_id": c["clip_id"], "sha256": c["sha256"],
        "embedded": False,
        "rate": rate,
        "seg_start_src": in_f, "seg_end_src": out_f,
        "extent_src": out_f - in_f,  # occupancy in source units
        "start_tl": start, "extent_tl": extent,  # occupancy (Fractions)
        "head_tl": Fraction(0), "head_src": 0,
        "tail_src": 0,  # xf handle extension mutates these
        "clip_db": float(c.get("gain_db") or 0.0),
        "track_db": float(t.get("gain_db") or 0.0),
        "fade_in": {"n": int(fin.get("units") or 0),
                    "curve": fin.get("curve") or "linear"},
        "fade_out": {"n": int(fout.get("units") or 0),
                     "curve": fout.get("curve") or "linear"},
        "automation": auto,
        "automation_interp": str(c.get("automation_interp")
                                 or "linear"),
        # N3-A — lane automation folded into the same dB sum, keyed
        # at absolute timeline frames.
        "track_auto": _track_pts(t),
        "track_auto_interp": str(t.get("automation_interp")
                                 or "linear"),
        "xf": None,
    }


def _apply_xf(x: dict, out_c: dict, in_c: dict, tl_fps: Fraction) -> None:
    """Extend a satisfied pair's streams into their source handles and
    anchor the complementary window envelopes (docs/n2d-fades.md)."""
    S, E = Fraction(int(x["tl_start"])), Fraction(
        int(x["tl_start"]) + int(x["tl_frames"]))
    curve = _FADE_CURVE.get(x.get("curve")) or "tri"
    # Source-unit conversion: tl frames → this clip's native samples.
    def src(tl_f: Fraction, rate: int) -> int:
        return _q(tl_f * rate / tl_fps)
    # out-clip: render end extends to E via tail handle; fade-out over
    # the window — anchored in stream-local source units.
    out_tail_tl = E - (out_c["start_tl"] + out_c["extent_tl"])
    out_c["tail_src"] = src(out_tail_tl, out_c["rate"])
    out_c["seg_end_src"] += out_c["tail_src"]
    out_c["xf"] = {
        "role": "out", "curve": curve,
        "off": src(S - out_c["start_tl"], out_c["rate"]),
        "n": src(E - S, out_c["rate"])}
    # in-clip: render start pulls back to S via head handle; fade-in
    # starts at stream offset 0 (the extended stream begins at S).
    in_head_tl = in_c["start_tl"] - S
    in_c["head_tl"] = in_head_tl
    in_c["head_src"] = src(in_head_tl, in_c["rate"])
    in_c["seg_start_src"] -= in_c["head_src"]
    in_c["xf"] = {
        "role": "in", "curve": curve,
        "off": 0, "n": src(E - S, in_c["rate"])}


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
    if not plan["clips"] and plan.get("audio_clips"):
        w = w or 1280  # audio-only render: fixed black canvas
        h = h or 720
    w -= w % 2
    h -= h % 2
    if w < 2 or h < 2:
        raise ValueError("export geometry is too small")
    return {"width": w, "height": h, "fps_num": num, "fps_den": den,
            "profile": profile}


def build_composite_argv(ffmpeg: str, plan: dict, paths: dict[str, str],
                         canvas: dict, crf: int, out_path: str,
                         encoder: str = "libx264",
                         script_path: str | None = None):
    """One-pass multitrack render. `paths` maps clip_id -> resolved
    source file (pinned-sha CAS path). Graph:

      color=black:WxH:fps:d=DUR [base]
      per clip: [i:v] trim→setpts→fps→scale(fit) [c{i}]
      chain:    [base][c0]overlay=center+eof_action=pass [o0]
                [o_{k-1}][c{k}]overlay=... [v]
      audio:    anullsrc base + per-clip atrim→adelay → amix
                (duration=first, normalize=0) → alimiter [a]

    N3-D argv bound: with ``script_path`` the graph is returned for the
    caller to write to the job work dir and ``-/filter_complex`` —
    FFmpeg ≥7 reads an option's argument from a file (the
    ``-filter_complex_script`` option itself was removed in 8.0) —
    is used instead of an inline graph (Windows' ~32k command-line
    limit fails ~240-clip timelines otherwise). Inputs are deduplicated
    by resolved path — N clips on one asset produce ONE ``-i`` plus
    ``split``/``asplit`` fan-out in the graph — so input count is
    bounded by unique sources, not clip count. Returns the argv list,
    or ``(argv, script_text)`` when ``script_path`` is set.
    """
    W, H = canvas["width"], canvas["height"]
    fps = f"{canvas['fps_num']}/{canvas['fps_den']}"
    dur = plan["program_frames"] / Fraction(canvas["fps_num"],
                                            canvas["fps_den"])
    dur_s = _flt(float(dur))
    clips = plan["clips"]
    a_inputs = plan.get("audio_clips") or []
    # dedupe -i by resolved path; count stream consumers per input
    need: dict[str, dict[str, int]] = {}
    for c in clips:
        need.setdefault(paths[c["clip_id"]], {"v": 0, "a": 0})["v"] += 1
    for a in a_inputs:
        need.setdefault(paths[a["clip_id"]], {"v": 0, "a": 0})["a"] += 1
    inputs = list(need)
    uidx = {p: i for i, p in enumerate(inputs)}
    argv = [ffmpeg, "-y", "-hide_banner", "-v", "error"]
    for p in inputs:
        argv += ["-i", p]
    vc: dict[str, int] = {p: 0 for p in inputs}
    ac: dict[str, int] = {p: 0 for p in inputs}
    parts: list[str] = []
    for p in inputs:
        i, u = uidx[p], need[p]
        if u["v"] > 1:
            parts.append(f"[{i}:v]split={u['v']}" + "".join(
                f"[sv{i}_{k}]" for k in range(u["v"])))
        if u["a"] > 1:
            parts.append(f"[{i}:a]asplit={u['a']}" + "".join(
                f"[sa{i}_{k}]" for k in range(u["a"])))

    def vspec(p: str) -> str:
        if need[p]["v"] == 1:
            return f"{uidx[p]}:v"
        k = vc[p]; vc[p] += 1
        return f"sv{uidx[p]}_{k}"

    def aspec(p: str) -> str:
        if need[p]["a"] == 1:
            return f"{uidx[p]}:a"
        k = ac[p]; ac[p] += 1
        return f"sa{uidx[p]}_{k}"

    parts.append(
        f"color=c=black:s={W}x{H}:r={fps}:d={dur_s},format=yuv420p[base]")
    cur = "base"
    for i, c in enumerate(clips):
        in_s = float(c["in_s"])
        end_s = float(c["in_s"] + c["dur_s"])
        parts.append(
            f"[{vspec(paths[c['clip_id']])}]trim="
            f"start={_flt(in_s)}:end={_flt(end_s)},"
            f"setpts=PTS-STARTPTS,"
            f"fps={fps},"
            f"scale=w={W}:h={H}:force_original_aspect_ratio=decrease"
            f"[c{i}]")
        nxt = "v" if i == len(clips) - 1 else f"o{i}"
        # Frame-index window [S, E): n is the output frame number at the
        # timeline rate — exact, no float-second boundary leak.
        parts.append(
            f"[{cur}][c{i}]overlay=x=(main_w-overlay_w)/2:"
            f"y=(main_h-overlay_h)/2:eof_action=pass:"
            f"enable='gte(n,{c['start_frame']})"
            f"*lt(n,{c['occ_end_frame']})'[{nxt}]")
        cur = nxt
    # Audio — every audible stream (video-embedded + dedicated lanes)
    # becomes a full-timeline stream via sample-exact leading silence,
    # then amix duration=first against a full-length silent base.
    a_inputs = plan.get("audio_clips") or []
    tl_fps = Fraction(plan["fps_num"], plan["fps_den"])
    for a in a_inputs:
        a["stream"] = aspec(paths[a["clip_id"]])
    if a_inputs:
        a_parts = [f"anullsrc=r={AUDIO_RATE}:cl=stereo,"
                   f"atrim=0:{dur_s}[abase]"]
        mix_in = "[abase]"
        for j, a in enumerate(a_inputs):
            if a["embedded"]:
                proc = _embedded_chain(a["stream"], a, tl_fps)
                lead = round(float(a["tl_start_s"]) * AUDIO_RATE)
            else:
                proc = _lane_chain(a["stream"], a, tl_fps)
                lead = _q((a["start_tl"] - a["head_tl"])
                          * AUDIO_RATE / tl_fps)
            if lead:
                a_parts.append(
                    f"anullsrc=r={AUDIO_RATE}:cl=stereo,"
                    f"atrim=end_sample={lead}[sil{j}]")
                a_parts.append(f"{proc}[as{j}]")
                a_parts.append(
                    f"[sil{j}][as{j}]concat=n=2:v=0:a=1[am{j}]")
            else:
                a_parts.append(f"{proc}[am{j}]")
            mix_in += f"[am{j}]"
        a_parts.append(
            f"{mix_in}amix=inputs={len(a_inputs) + 1}:"
            f"duration=first:normalize=0,"
            # Explicit limiter contract — never rely on defaults:
            # ceiling −0.45 dBFS, 5 ms attack / 50 ms release,
            # level=0 disables auto-makeup (default would boost
            # quiet mixes), latency=1 compensates the lookahead
            # delay so the mix stays sample-aligned.
            f"alimiter=limit=0.95:attack=5:release=50:"
            f"level=0:latency=1[a]")
        parts += a_parts
    graph = ";".join(parts)
    if script_path is not None:
        argv += ["-/filter_complex", script_path]
    else:
        argv += ["-filter_complex", graph]
    argv += ["-map", f"[{cur}]"]
    if a_inputs:
        argv += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k",
                 "-ar", str(AUDIO_RATE)]
    argv += ["-c:v", encoder, "-preset", "fast", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-t", dur_s, out_path]
    if script_path is not None:
        return argv, graph
    return argv


def _lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _embedded_chain(spec: str, a: dict, tl_fps: Fraction) -> str:
    """Video-embedded audio: seconds-based trim (the clip's source
    units are video frames), resample, stereo, track gain —
    fades/clip-automation/crossfades are audio-lane-only by domain
    rule. N3-A: TRACK automation still applies — it's a lane-level
    envelope keyed on absolute timeline frames, not a clip edit; the
    aeval runs at 48 kHz (post-resample), so `n` counts 48 k samples
    from stream start = the clip's timeline start."""
    in_s = float(a["in_s"])
    end_s = float(a["in_s"] + a["dur_s"])
    chain = (f"[{spec}]atrim=start={_flt(in_s)}:end={_flt(end_s)},"
             f"aresample={AUDIO_RATE},aformat=channel_layouts=stereo")
    tpts = a.get("track_auto") or []
    if tpts:
        tstep = a.get("track_auto_interp") == "step"
        offs = [_q((p["at_tl"] - a["start_tl"]) * AUDIO_RATE / tl_fps)
                for p in tpts]
        expr = (f"{_flt(a['track_db'])}"
                f"+({_seg_expr(offs, [p['gain_db'] for p in tpts],
                                tstep)})")
        env = f"pow(10,({expr})/20)"
        chain += f",aeval=exprs='val(0)*({env})|val(1)*({env})'"
    elif a["track_db"]:
        chain += f",volume={_flt(_lin(a['track_db']))}"
    return chain + ",asetpts=PTS-STARTPTS"


def _lane_chain(spec: str, a: dict, tl_fps: Fraction) -> str:
    """Dedicated-lane audio — the full N2-C/N2-D gain contract in one
    deterministic order, all envelope anchors in stream-local source
    units at the clip's native rate (resample LAST):

      atrim source segment (incl. borrowed xf handles)
      → asetpts=PTS-STARTPTS — REQUIRED: afade anchors start_sample
        on the frame PTS, which atrim leaves in source units; without
        the reset a trimmed or head-borrowed stream starts at a
        nonzero pts and its fade-in/out windows are already "past"
      → aformat stereo (2-channel layout before aeval's val(c))
      → gain envelope: aeval clip+track dB + automation interpolated
        linearly in dB over stream-local sample offsets — or a plain
        volume when no automation exists
      → afade fade-in / fade-out (edge-anchored, source units)
      → afade crossfade envelope (window-local)
      → aresample 48000 → asetpts.

    Everything multiplies the program signal exactly once; the dB sum
    lives in a single pow(10,x/20) so nothing double-applies."""
    rate = a["rate"]
    chain = (f"[{spec}]atrim=start_sample={a['seg_start_src']}"
             f":end_sample={a['seg_end_src']},"
             "asetpts=PTS-STARTPTS,"
             "aformat=channel_layouts=stereo")
    db_sum = a["clip_db"] + a["track_db"]
    if a["automation"] or a.get("track_auto"):
        chain += f",aeval=exprs='{_auto_exprs(a, db_sum, tl_fps)}'"
    elif db_sum:
        chain += f",volume={_flt(_lin(db_sum))}"
    fin, fout, xf = a["fade_in"], a["fade_out"], a["xf"]
    if fin["n"] > 0:
        chain += (f",afade=t=in:start_sample={a['head_src']}"
                  f":nb_samples={fin['n']}"
                  f":curve={_FADE_CURVE.get(fin['curve'], 'tri')}")
    if fout["n"] > 0:
        off = a["head_src"] + a["extent_src"] - fout["n"]
        chain += (f",afade=t=out:start_sample={off}"
                  f":nb_samples={fout['n']}"
                  f":curve={_FADE_CURVE.get(fout['curve'], 'tri')}")
    if xf is not None:
        kind = "in" if xf["role"] == "in" else "out"
        chain += (f",afade=t={kind}:start_sample={xf['off']}"
                  f":nb_samples={xf['n']}:curve={xf['curve']}")
    return chain + f",aresample={AUDIO_RATE},asetpts=PTS-STARTPTS"


def _seg_expr(offs: list[int], gains: list[float],
              step: bool) -> str:
    """aeval fragment evaluating automation dB at stream-local sample
    n — linear-in-dB ramps between neighbors ('linear') or holds
    ('step'), flat outside the end points. `offs` are pre-quantized
    sample positions; identical positions collapse to a
    deterministic hold (the left value wins)."""
    expr = _flt(gains[-1])
    for i in range(len(offs) - 1, 0, -1):
        lo, hi, g_lo, g_hi = offs[i - 1], offs[i], gains[i - 1], gains[i]
        if hi <= lo:  # same quantized sample — deterministic step
            seg, cond = _flt(g_lo), f"eq(n,{lo})"
        elif step:  # step mode — hold lo's value up to hi
            seg, cond = _flt(g_lo), f"between(n,{lo},{hi - 1})"
        else:
            seg = (f"{_flt(g_lo)}+({_flt(g_hi - g_lo)})"
                   f"*(n-{lo})/{hi - lo}")
            cond = f"between(n,{lo},{hi - 1})"
        expr = f"if({cond},{seg},{expr})"
    expr = f"if(lt(n,{offs[0]}),{_flt(gains[0])},{expr})"
    return expr


def _auto_exprs(a: dict, db_sum: float, tl_fps: Fraction) -> str:
    """aeval `exprs` value: one interpolation per channel applying
    (clip dB + track dB + clip automation + track automation) as a
    single pow(10,x/20) — the A3 gain contract in one expression, so
    every factor multiplies exactly once. `n` is the stream-local
    sample index at native rate; all keyframe positions land there
    via the single quantize point.

    Clip points are clip-local tl-frame offsets from `head_tl`; N3-A
    track points are ABSOLUTE timeline frames — the stream begins at
    (start_tl − head_tl), so a head-borrowed crossfade stream
    evaluates the lane curve at positions before the clip's own
    occupancy — the correct absolute-time reading."""
    terms = [_flt(db_sum)]
    pts = a["automation"]
    if pts:
        step = a.get("automation_interp") == "step"
        offs = [_q((a["head_tl"] + p["off_tl"]) * a["rate"] / tl_fps)
                for p in pts]
        terms.append(
            f"({_seg_expr(offs, [p['gain_db'] for p in pts], step)})")
    tpts = a.get("track_auto") or []
    if tpts:
        tstep = a.get("track_auto_interp") == "step"
        stream0 = a["start_tl"] - a["head_tl"]
        toffs = [_q((p["at_tl"] - stream0) * a["rate"] / tl_fps)
                 for p in tpts]
        terms.append(
            f"({_seg_expr(toffs, [p['gain_db'] for p in tpts], tstep)})")
    env = f"pow(10,({'+'.join(terms)})/20)"
    return f"val(0)*({env})|val(1)*({env})"


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
                      encoder: str = "libx264",
                      script_path: str | None = None):
    """One-pass filter_complex export. Each segment:
      [i:v] trim→setpts→scale(fit)→pad→setsar→fps  [i:a|anull] atrim→aformat
    then concat v=1 a=(0|1).

    `segments` items: {path, in_s, dur_s, has_audio}. Trims arrive as
    SECONDS computed from exact source-frame rationals — trim filter
    takes start/end times so the cut is frame-precise.

    N3-D argv bound: ``-i`` inputs are deduplicated by resolved path —
    segments split from one source share ONE input and fan out via
    split/asplit, so input count is bounded by unique sources. With
    ``script_path`` the graph is returned for the caller to write to
    the job work dir and ``-/filter_complex`` replaces the inline
    graph (Windows' ~32k command-line limit fails large timelines
    otherwise; ``-filter_complex_script`` was removed in FFmpeg 8.0).
    Remaining bound: segments without audio still add one anullsrc
    lavfi input each — bounded by the silent-segment count.
    Returns the argv list, or ``(argv, script_text)`` when
    ``script_path`` is set.
    """
    n = len(segments)
    any_audio = any(s["has_audio"] for s in segments)
    need: dict[str, dict[str, int]] = {}
    for s in segments:
        u = need.setdefault(s["path"], {"v": 0, "a": 0})
        u["v"] += 1
        if s["has_audio"]:
            u["a"] += 1
    inputs = list(need)
    uidx = {p: i for i, p in enumerate(inputs)}
    argv = [ffmpeg, "-y", "-hide_banner", "-v", "error"]
    for p in inputs:
        argv += ["-i", p]
    silent_inputs: dict[int, int] = {}  # segment idx -> lavfi input idx
    if any_audio:
        for i, s in enumerate(segments):
            if not s["has_audio"]:
                idx = len(inputs) + len(silent_inputs)
                silent_inputs[i] = idx
                argv += ["-f", "lavfi", "-t", _flt(s["dur_s"]),
                         "-i", "anullsrc=r=48000:cl=stereo"]
    vc: dict[str, int] = {p: 0 for p in inputs}
    ac: dict[str, int] = {p: 0 for p in inputs}
    parts: list[str] = []
    for p in inputs:
        i, u = uidx[p], need[p]
        if u["v"] > 1:
            parts.append(f"[{i}:v]split={u['v']}" + "".join(
                f"[sv{i}_{k}]" for k in range(u["v"])))
        if u["a"] > 1:
            parts.append(f"[{i}:a]asplit={u['a']}" + "".join(
                f"[sa{i}_{k}]" for k in range(u["a"])))
    for i, s in enumerate(segments):
        p = s["path"]
        u = need[p]
        if u["v"] == 1:
            v_in = f"{uidx[p]}:v"
        else:
            v_in = f"sv{uidx[p]}_{vc[p]}"; vc[p] += 1
        end = s["in_s"] + s["dur_s"]
        parts.append(
            f"[{v_in}]trim=start={_flt(s['in_s'])}:end={_flt(end)},"
            f"setpts=PTS-STARTPTS,"
            f"scale=w={target['width']}:h={target['height']}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={target['width']}:{target['height']}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={target['fps_num']}/{target['fps_den']}[v{i}]")
        if any_audio:
            if i in silent_inputs:
                a_in = f"{silent_inputs[i]}:a"
            elif u["a"] == 1:
                a_in = f"{uidx[p]}:a"
            else:
                a_in = f"sa{uidx[p]}_{ac[p]}"; ac[p] += 1
            parts.append(
                f"[{a_in}]atrim=0:{_flt(s['dur_s'])},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
    if any_audio:
        pair = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{pair}concat=n={n}:v=1:a=1[v][a]")
    else:
        vin = "".join(f"[v{i}]" for i in range(n))
        parts.append(f"{vin}concat=n={n}:v=1:a=0[v]")
    graph = ";".join(parts)
    if script_path is not None:
        argv += ["-/filter_complex", script_path]
    else:
        argv += ["-filter_complex", graph]
    argv += ["-map", "[v]"]
    if any_audio:
        argv += ["-map", "[a]"]
    argv += ["-c:v", encoder, "-preset", "fast", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if any_audio:
        argv += ["-c:a", "aac", "-b:a", "160k"]
    argv += [out_path]
    if script_path is not None:
        return argv, graph
    return argv
