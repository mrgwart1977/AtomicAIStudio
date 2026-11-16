# N3-A — per-track gain automation

N3-A implements the per-track automation deferred from N2
(architecture sketched in `docs/n2f-preview.md` §"per-track
automation"). Lane-level gain keyframes, persisted on the track,
evaluated at **absolute timeline time**, folded into the same single
dB sum at render and preview — audio lanes and video-embedded audio
alike. No parallel subsystem: same command/undo/audit/EDL machinery,
same stored representation as clip automation, same interpolation
evaluator, same `_lane_chain`/`_embedded_chain` render stages.

## Model (A1)

`timeline_tracks.automation_json` (migration `0019_n3a`, additive +
idempotent) — `[{id, at_tl_frames, gain_db}]`:

- **`at_tl_frames` is an ABSOLUTE timeline-frame position** —
  unlike clip automation's clip-local `at_frames`, a lane keyframe
  is a property of the lane, so clip moves/trims/splits can never
  orphan it. Valid range `0 … 2³⁰−1`; points past the program end
  are valid and inert (flat tail).
- **Stable identity** — missing `id`s are assigned `p{i}` at write;
  stored sorted by position (deterministic serialization).
- **Duplicate positions refuse** — same policy as clip automation.
- **Bounded** — `TRACK_AUTOMATION_MAX_POINTS = 256` (4× the clip
  bound: a lane spans the whole program, not one clip's occupancy).
- **Gain bounds** — same `check_gain_db` contract (−60…+12 dB).
- **Lane kinds** — `audio` and `video` lanes only; `overlay`
  refuses (no audio output — the same rule as `gain_db`).

## Command (A1)

`timeline.track.automation` — `{track_id, points[], interp?}` —
replace-all, one commit, one undo step. Full machinery: preview →
commit → audit (`"Set track automation"`), expected-revision stale
write protection (409), idempotency keys, project-scoped
authorization, undo/redo via `track_snapshot`/`_restore_snapshot`
(both extended to carry the two fields).

## Interpolation (A2)

Track automation reuses `AUTOMATION_INTERPS` verbatim — `linear`
(dB-domain ramp between neighbors) and `step` (hold each value until
the next point), evaluated by the same `automation_gain_db` core via
`track_automation_gain_db` (a field-name adapter — one
implementation, two position domains).

**`s_curve`/`exponential` are NOT automation interps.** They are
fade *amplitude* curves — an envelope shape over a bounded fade
window, a different semantic than keyframe interpolation. Reusing
the names for a different meaning would be ambiguous in the
document and require new piecewise math in both the aeval exporter
and the preview. Deliberately not added; both interps we ship are
implemented identically in both engines (required by the parity
rule).

## Gain contract (A3)

The effective gain model, applied **once** in the linear domain:

```
g(t) = 10^(( clip_db + track_db
           + clipAuto_db(clipLocalT) + trackAuto_db(absoluteT) )/20)
     × fadeEnv(t) × xfEnv(t)
```

- **All factors multiply in linear amplitude** — the dB-domain sum
  happens inside one `pow(10,x/20)` in `_auto_exprs`: base dB sum +
  clip-automation piecewise + track-automation piecewise. There is
  exactly one envelope pass per channel; fades and crossfades stay
  as `afade` stages (linear multipliers, never double-applied).
- **Track automation reads absolute timeline time.** A stream-local
  sample `n` maps to timeline frame `(start_tl − head_tl) +
  n·tl_fps/rate`. Head-borrowed crossfade streams evaluate the lane
  curve at positions *before* the clip's occupancy — the correct
  reading of an absolute-time envelope.
- Mute/solo/visibility precedence, embedded semantics, clip gain,
  clip automation, fades, and crossfades are all unchanged.

## Embedded video audio (A4)

`_embedded_chain` gains the same aeval — track automation is a lane
envelope of *position*, not a clip edit, so it applies to embedded
audio while clip fades/automation/crossfades still don't exist
there. The aeval runs post-`aresample` at 48 kHz: `n` counts 48 kHz
samples from stream start = the clip's timeline start; offsets are
`(at_tl_frames − start_tl) × 48000 / tl_fps` via the same `_q`
quantize-half-up point. Unautomated lanes keep the cheap `volume`
stage — aeval only appears when an envelope exists.

## EDL v2

Track entries gain `"automation": [{id, at_tl_frames, gain_db}]`
and `"automation_interp"`. Deterministic like the rest of v2;
v1 unchanged.

## Preview

`previewPlan.ts` parses each lane's keyframes once and folds
`trackAutomationGainDb(startTl + clipLocalT)` into the same dB sum
for audio-lane clips — and `trackAutomationGainDb(startFrames +
tS·tlFps)` for embedded entries (their env is no longer a constant
when the lane is automated). Same `env(t)` → 64 Hz
`setValueCurveAtTime` contract; no engine change needed.

## Shared parity fixtures

The 7 N2-F fixtures are unchanged; `track-automation.json` (8th)
exercises linear lane ramp on an audio clip + step lane automation
on an embedded video clip — consumed by BOTH
`backend/tests/test_n2f_parity.py` (real `_lane_chain` /
`_embedded_chain` rendered through ffmpeg on a constant signal;
measured gain at each checkpoint) and
`previewPlan.parity.test.ts` (`buildPreviewPlan` env at the same
checkpoints). One fixture file, two implementations.

## UI

Track header mix row gains an `ƒ` toggle expanding a keyframe
editor: point list (timecode position + gain slider + delete),
`linear|step` select, "+ keyframe at playhead" (dedupes to the
nearest free absolute frame). Positions display as absolute
timecode. Same bounded point-list editing surface as clip
automation — no on-curve dragging (consistent with N2-D's
documented surface).

## Migration + rollback

`0019_n3a` is additive and idempotent: two `NOT NULL` columns with
server defaults on `timeline_tracks` only; verified against a
populated `0018_n2f` database (project/timeline/track/clip rows and
their values — gain, mute, solo, visibility — survive unchanged,
new columns land at the neutral `'[]'`/`'linear'`). No backfill, no
rewrites, no table rebuilds.

Rollback (`downgrade()`) drops both columns: **all stored track
automation is destroyed** — the same one-way shape as every prior
migration here. Rolling the app back without downgrading keeps the
columns harmlessly (pre-N3-A code ignores them); downgrading then
re-upgrading restores empty automation, not the user's keyframes.
Backup/export before downgrade if automation data matters.

## Hardening verification (post-review)

- `fixtures/audio-parity/track-automation-combined.json` (9th
  fixture; the original 7 + the N3-A fixture untouched) exercises
  combination envelopes at PCM level through the real
  `_lane_chain`/`_embedded_chain` ffmpeg renders AND
  `buildPreviewPlan`: lane linear ramp across a satisfied
  equal-power crossfade (both directions, 11 checkpoints — each
  measured gain is ramp × xf-env product), step lane automation ×
  clip fade-in, step lane × clip automation on an adjacent
  (split-shaped) clip, linear lane ramp on embedded video audio.
- Absolute-time semantics under edits: clip move keeps keyframes
  AND remaps offsets to the new absolute span (a pre-stream point
  emits a negative between() bound; mid-ramp starts measured at
  PCM level); trim + split leave `automation_json` byte-identical
  and EDL re-serializes.
- Boundaries: points at tl 0, at stream start mid-ramp, at/past
  program end (flat tail — inert, not refused).

## Boundaries

- Lane automation applies to EVERY contributing stream on the lane —
  there is no per-clip opt-out beyond clip gain/automation.
- Points beyond the program end are inert, not refused.
- Preview remains editorial: the measured contract is the render
  (64 Hz sampling, decode/resample differences documented in
  `docs/n2f-preview.md`).
- Keyframe positions are set at the playhead (frame-exact,
  deduped to the nearest free absolute frame) — there is no
  direct numeric position entry; same bounded editing surface as
  clip automation, documented as the established UX shape.
- Human checks outstanding: lane-automation feel + audible
  judgment **[human]**.
