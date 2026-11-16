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
atrim segment → aformat stereo
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

Curve mapping (afade enum): `linear` → `tri`(0),
`equal_power` → `qsin`(1) — the sin/cos pair whose in+out product
sums to unity power. (`hsin` is the sin² S-curve — **not**
equal-power; don't confuse them.)

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

- Per-track automation, additional curves/interpolations, and
  audio-preview scrubbing remain deferred (N2-F).
- Human listening judgment on mixes is a **[human]** check.
- Output duration bounds and cancellation/restart semantics are
  unchanged from C5-B (supervised run, `duration=first`, `-t` cap).
