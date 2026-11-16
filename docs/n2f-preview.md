# N2-F — audio preview, remaining features, acceptance

The last N2 milestone: local audio preview against the existing
timeline model, the bounded remaining audio features, and the
end-to-end acceptance + completion report.

## Scope decision (reported before implementation)

**In scope (as built)**

- **Audio preview engine** — WebAudio (`AudioContext`) scheduling of
  every *audible clip on a dedicated audio lane* (N2-C audibility:
  hidden/muted/non-soloed lanes excluded), fed by authenticated
  `fetchAssetObjectUrl` → `decodeAudioData`. Per-clip gain envelope
  folded into ONE `setValueCurveAtTime` curve using the same
  effective-gain math as the render (`docs/n2e-mixing.md`): clip+track
  dB sum, automation (`linear`|`step`), fade-in/out and crossfade
  envelopes — multiplication happens once, in the same order.
  `previewPlan.ts` ports `composite_plan`'s audio section + `_apply_xf`
  handle extension + `fade_gain`/`automation_gain_db` verbatim; unit
  tests pin parity.
- **Transport** — play / pause / seek / stop synced to the existing
  `playheadF`; click-to-seek on the ruler; playhead follows the
  AudioContext clock via rAF; ruler seeks during playback re-schedule
  (short-range scrub). Mute/solo/gain/fade edits take effect on the
  next play (the plan rebuilds from `TimelineDetail`).
- **Honesty boundary** — preview is an editorial aid, not a
  measurement: resampling, AAC decode, and limiter behavior differ
  from the render path. We do NOT claim sample-perfect preview;
  correctness evidence remains the exported file.
- **Extra fade curves** — `s_curve` (afade `hsin`) and `exponential`
  (afade `exp`) added to the domain whitelist, EDL, render mapping,
  preview envelopes, and inspector select.
- **Automation interpolation** — per-clip `interp` field
  (`linear` | `step`): `step` holds each keyframe's value until the
  next. Command, EDL field, render (aeval step segments), preview,
  UI toggle.
- **UX + accessibility** — transport buttons with aria labels,
  Space/arrow-key seek shortcuts, an "audio unavailable" clip badge
  when a preview source can't decode, disabled-transport reason.

**Added in the final review**

- **Embedded video audio in preview** — clips on video lanes whose
  `src_has_audio` flag (persisted at place-time from the import
  probe; migration `0018_n2f` backfills + audio-free sources set it
  false) contribute a constant-gain embedded entry at clip+track
  gain. Fades/automation/crossfades remain audio-lane concepts —
  matching `_embedded_chain`, which applies track gain only.
  Visibility, mute, and solo honour the same audibility matrix;
  video rendering is independent of audio muting by construction
  (preview only adds a GainNode branch; it never gates picture).
  Same decoded source assets and authorization as audio lanes —
  no separate model.
- **Shared parity fixtures** — `fixtures/audio-parity/*.json` define
  canonical timelines + expected envelope checkpoints in a neutral
  format. `tests/test_n2f_parity.py` runs the REAL `_lane_chain` /
  `_embedded_chain` through ffmpeg and asserts measured gain at each
  checkpoint; `previewPlan.parity.test.ts` converts the same JSON to
  `TimelineDetail` and asserts `buildPreviewPlan` envelopes. One
  fixture file, two implementations, no duplicated formulas.
- **Objective preview verification** — `renderPlanOffline` builds the
  identical WebAudio graph inside `OfflineAudioContext`, so the
  installed acceptance driver renders the preview plan to PCM and
  measures embedded tone / gains / fades / crossfade / automation /
  gap — real audible output, not playhead motion.

**Deferred (reported, not silently dropped)**

- **Per-track automation** — a genuinely new model dimension: track
  keyframes are timeline-absolute (not clip-local), need a new
  command + EDL section, and must be folded into each audible clip's
  envelope over the intersection window at both render and preview
  time — plus a dedicated per-track editor. Architecture is sketched
  in §"per-track automation (deferred)" below; it is the size of a
  milestone on its own and would unacceptably dilute preview +
  acceptance depth inside N2-F. Proposed for N3 audio polish.
- **Continuous drag-scrub** — ruler seeks during playback re-schedule
  correctly (short-range scrub); continuous pointer-drag scrubbing
  re-triggers cleanly but is a [human] perceptual judgment either
  way. Documented, not oversold.

## Preview architecture

```
TimelineDetail ──► buildPreviewPlan(detail)   [previewPlan.ts]
  per audible audio-lane clip (ports composite_plan + _apply_xf):
    { assetId, tlStartS, tlEndS, srcStartS, segDurS, embedded,
      env(t): effective-gain fn — dB sum + automation + fades + xf }
  embedded entries: video-lane clips with src_has_audio →
    constant clip+track gain envelope, no fades/automation/xf

AudioPreviewEngine                        [audioPreview.ts]
  load():  fetchAssetObjectUrl → decodeAudioData → clipId→buffer map
  play(f): BufferSource ─► GainNode(setValueCurveAtTime(env, 64 Hz))
         ─► master ─► destination; anchor = ctx.currentTime + 50 ms
  pause(): position() freeze + stop sources (plan/buffers retained)
  stop():  teardown all nodes; dispose() closes the AudioContext
  scrubTo(s): re-schedules from s while playing
```

## Envelope resolution (measured)

`env(t)` is sampled at 64 Hz into `setValueCurveAtTime`. Measured by
`renderPlanOffline` vs decoded FFmpeg PCM (installed driver, step 5):
step edges blur ≤ one sample period (~15.6 ms); linear/qsin/hsin/exp
fades track the renderer within the fixture tolerances
(±0.02 abs gain, ±20 ms onset); automation keyframes near clip
boundaries are exact because envelopes clamp at stream start/end.
Short clips (<32 samples of envelope) still schedule correctly —
curve times are anchored to the clip's own start, not a global grid.
64 Hz is sufficient for every shipped curve; it is bounded
(clip_dur×64 + 2 points, no unbounded keyframe growth) — no adaptive
scheduler needed.

## Envelope math parity

Same math functions
as the FFmpeg chain: `tri`→u, `qsin`→sin(πu/2)/cos(πu/2),
`hsin`→sin²/cos², `exp`→e^{−11.5129(1−t)}/e^{−11.5129t} (the actual
afade contract — verified against FFmpeg source + PCM); automation
interpolates in the dB domain identically to aeval, incl. `step`.

## Per-track automation (deferred — architecture)

Model: `TimelineTrack.automation_json` keyframes `{at_tl_frames,
gain_db}` (timeline-absolute, sorted, bounded, dB-checked) +
`timeline.track.automation` command + EDL `track.automation`.
Render: for each audible clip, intersect the track curve over the
clip's stream window and fold into the same aeval dB sum (one
pow() — nothing double-applies). Preview: same folding into the
composite envelope. UI: a track-level keyframe lane/editor.

## Acceptance shape

- Unit: `previewPlan` audibility/geometry vs `composite_plan`
  semantics; envelope sampler vs the FFmpeg curve math.
- Installed: build the FILM timeline — two video lanes (embedded
  audio), dialogue + music + SFX lanes, gains, mute/solo, fades
  (all curves), crossfade, automation (both interps), gaps,
  30000/1001 — export, ffprobe (codec/720p/48k stereo/duration),
  PCM checks per element, play/pause/seek live in the UI,
  cancellation, restart persistence, clean shutdown.
- Perf: repeated renders + preview memory are measured and recorded
  in the N2 completion report; no inference/benchmarks beyond that.
