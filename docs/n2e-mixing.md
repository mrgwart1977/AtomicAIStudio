# N2-E — audio mixing and export

Audio lanes render. N2-E extends the existing FFmpeg composite
exporter (`domain.timeline_export` + `adapters.timeline_export`) —
same durable job, same pinned `ade-edl/2` document, same CAS source
resolution and byte-hash verification, same supervise/cancel/restart
handling. **There is no parallel audio renderer**: the video overlay
chain and the audio mix live in one bounded `filter_complex` graph.

## What renders

Every **audible** stream becomes a full-timeline 48 kHz stereo stream
and is summed by `amix normalize=0` + `alimiter` — the same graph
shape C5-B already used for embedded audio:

```
anullsrc=r=48000:cl=stereo,atrim=0:DUR            [abase]
per stream:  [src] → trim → envelope → aresample 48000
             → leading-sample silence → concat → [am_i]
[abase][am_0]…[am_k] amix duration=first → alimiter=0.95 → AAC 160k
```

- **Dedicated audio lanes** (`kind=audio`): per-clip
  `atrim=start_sample:…end_sample` on **native-rate source units**
  (audio source units are samples), `aformat=stereo`, then the
  gain/fade/crossfade envelope chain, then `aresample=48000`.
- **Video-embedded audio**: seconds-based `atrim` (video source
  units are frames → exact in/out seconds), `aresample`, stereo,
  track-gain `volume` — fades/automation are audio-lane-only by
  domain rule and can't exist here.
- **Placement**: leading silence is prepended as
  `anullsrc+atrim=end_sample` + `concat` — integer-sample alignment,
  no millisecond quantization, no `adelay`. `amix duration=first`
  against a full-length silent base pins the output to the exact
  program length; shorter streams end honestly (no trailing-silence
  padding needed).
- **Audio-only timelines** render a fixed 1280×720 black canvas
  under the mix (the `source` profile falls back to it when no
  video lane contributes).

## Audibility (N2-C contract, unchanged)

`visible=false` → lane contributes nothing at all. `muted=true` →
no audio from that lane. A non-empty solo set (non-overlay lanes
with `solo`) silences every other lane's audio — video lanes keep
their picture; solo is never a visibility flag. Multiple solos are
all audible. Overlay lanes carry no gain/solo and contribute pixels
only.

## The gain/envelope chain (one pass, native rate)

Per the N2-D pinned contract, every multiplier is applied **exactly
once**, all in the linear domain, at the clip's **native sample
rate** (resample is last):

```
atrim segment
  → asetpts=PTS-STARTPTS   (REQUIRED — see below)
  → aformat stereo
  → aeval: val(c) × 10^((clip_db + track_db + interp_db(n))/20)
       — clip gain + track gain folded into ONE dB sum; automation
         keyframes interpolate linearly in dB over stream-local
         sample offsets (`n` is the sample index; `between`/`if`
         piecewise expr, flat outside first/last keyframes)
    or volume=10^(db_sum/20) when no automation exists
  → afade fade-in   at stream offset head_src, nb_samples=units
  → afade fade-out  at head_src + extent_src − units
  → afade crossfade envelope over the window
  → aresample=48000 → asetpts
```

Curve mapping (afade names): `linear` → `tri`,
`equal_power` → `qsin` — the sin/cos pair whose in+out product
sums to unity power. N2-F adds `s_curve` → `hsin` (the sin²/cos²
S-curve — *also* unity-power but gentler at both ends) and
`exponential` → `exp` (the afade contract g_in=e^{−11.5129(1−t)},
g_out=e^{−11.5129·t} — a −50 dB ramp across the fade; verified
against `libavfilter/af_afade.c` and decoded PCM). (`hsin` is the
sin² S-curve — **not** the sin/cos equal-power pair; don't confuse
them.)

N2-F automation interpolation: `automation_interp` is `linear`
(default — dB ramp between keyframes) or `step` (hold each
keyframe's value until the next; the aeval segment becomes a
constant — `between(n,lo,hi−1)` of the left value).

**The PTS reset is load-bearing**: afade anchors `start_sample`
against each frame's **PTS in samples**, and `atrim` keeps the
source PTS — a stream cut at source sample 36000 arrives with
pts=36000, so a `start_sample=0` fade-in is already "past" and
silently no-ops. `asetpts=PTS-STARTPTS` immediately after `atrim`
makes every envelope anchor stream-local. (Found during the N2-E
review: incoming-crossfade fade-ins never applied before the fix.)

## Master mix & limiter

```
per-stream full-timeline inputs (leading silence + content)
  → amix=inputs=N+1:duration=first:normalize=0
      (silent `abase` pins the program length; no attenuation)
  → alimiter=limit=0.95:attack=5:release=50:level=0:latency=1
      ceiling −0.45 dBFS measured at filter level (peak ≤0.95);
      level=0 disables FFmpeg's default auto-makeup so quiet mixes
      are NOT pumped up; latency=1 compensates the ~220-sample
      lookahead — measured onset unchanged, program not extended
  → aac 160 kbps @48 kHz
```

AAC roundtrip overshoots the ceiling slightly (lossy codec) —
measured <0.99, never full-scale. A program with zero audible
streams maps no audio track at all (video-only output).

## Durability

Same durable job path as video export: pinned-sha byte verification
before render, probe-stage stream checks, disk-space estimate
refusal, cancel → `JobCancelled` + partial output unlinked, nonzero
exit/timeout/empty output → failed + cleanup, post-render
`engine.probe` verification, work-dir purge, startup reconciliation
of interrupted jobs. No phantom output assets on any failure path.

## Crossfades at render time

A `satisfied` record extends the pair's streams into their **source
handles** — the same borrow N2-D validated:

- out-clip's segment end extends `win_end − out_occupancy_end`
  timeline frames into its tail handle; its xf fade-out covers the
  whole window.
- in-clip's segment start pulls back `in_start − win_start` into its
  head handle; the extended stream starts exactly at `win_start`,
  so its xf fade-in anchors at stream offset 0.
- Both streams cover the full window — even when a gap sits between
  the occupancies — so the equal-power/linear mix over the window is
  a real crossfade, not a dip and not a double-play.

**Fail closed**: a record reporting `satisfied=false` on an audible
lane refuses the export (`composite_plan` raises; the job fails with
the honest error). Records on inaudible lanes are inert. A record
referencing a video-lane clip means the document is corrupt —
refused.

## Sample-accurate sync

- Exact `Fraction` arithmetic end to end; quantization happens at
  **one** documented point per value: `q(v) = ⌊v + ½⌋` on exact
  rationals — timeline frames → 48 kHz samples for placement, and
  timeline frames → native-rate source units for envelope anchors
  and automation offsets.
- Mixed source rates (44.1 kHz, 48 kHz, others) resample into the
  single 48 kHz mix grid; fractional timeline rates (24000/1001,
  30000/1001) place clips by rational math — error is <1 source
  sample (~8 µs), never cumulative.
- Output: stereo, 48 kHz, AAC 160 kbps, duration bounded by the
  pinned program frames.

**Measured tolerance** (decoded PCM, ffmpeg 9.0.2 full build):
placement onsets land within ~4 ms of the rational expectation
across 24/25/30 fps, 24000/1001, 30000/1001, mixed 44.1/48 kHz
sources, and a 60 s timeline — after subtracting the AAC encode→
decode pipeline contribution measured by the click-control test
(priming is trimmed via the mp4 edit list, so codec Δ ≈ 0).
Program duration holds within one AAC frame (1024 samples).
We do NOT claim sub-sample end-to-end sync: the guarantee is
<1 sample of quantization error at each anchor, verified at
millisecond resolution on decoded output.

## Refusals (fail closed, clear errors)

| Condition | Behavior |
|---|---|
| Unsatisfied crossfade on audible lane | export refuses |
| Audio clip with no measured sample rate | export refuses |
| Audio source missing/corrupt/hash-mismatched | job fails at resolve/verify (unchanged) |
| Probe reports no decodable stream | job fails at probe stage |
| No visible populated lane at all | "nothing to composite" |

## UI

`exportMode` now reports `audio: none | embedded | tracks` — the
export chip and panel copy say what the mix will contain, and an
unsatisfied crossfade on an audible lane is surfaced as blocked
**before** the job is queued.

## Tests

- Plan-unit tests pin audibility, xf handle math, fail-closed
  refusals, argv shape (`tests/test_n2e_mixing.py`).
- **Real ffmpeg render tests** measure decoded PCM: onset placement
  (±samples), two-lane summing, mute/solo, clip+track gain, linear
  and equal-power fades, a real crossfade window (both tones
  present, never silent), automation segment levels, mixed sample
  rates, 30000/1001 placement, audio-only render, embedded+lane mix,
  and detached-crossfade refusal.

## Boundaries

- Per-track automation remains deferred (see docs/n2f-preview.md).
  N2-F shipped `s_curve`/`exponential` curves, `step` interpolation,
  and WebAudio audio-lane preview.
- Human listening judgment on mixes is a **[human]** check.
- Output duration bounds and cancellation/restart semantics are
  unchanged from C5-B (supervised run, `duration=first`, `-t` cap).
