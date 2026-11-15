# N2 — Audio Editing proposal (PLANNING ONLY — no implementation)

## Scope and reuse

N2 turns the timeline's audio lanes into a real audio editor on top of
the N1 multitrack foundation. Everything below reuses existing
infrastructure — nothing new is invented:

- **Lanes**: `timeline_tracks` already supports `kind: "audio"`
  (rendered as "Audio lane — audio clips aren't supported yet" today).
- **Clips**: `timeline_clips` rows are source-agnostic — an audio clip
  is a pinned source range on an audio lane; `start_frames`,
  `in/out_frame`, `src_*` all apply. Frame-accurate trims already work
  at the command layer.
- **Commands**: every N2 mutation rides `preview → commit → audit`
  with `expected_timeline` pins — undo/redo works for free because the
  stacks snapshot rows, not media.
- **Export**: N1-D's `atrim→adelay→amix→alimiter` chain already mixes
  clip-embedded audio; N2 adds audio-lane inputs to the same graph.
- **Jobs**: waveform generation + loudness analysis are durable
  `generation_jobs` rows, idempotent, cancellable, CAS-registered.

## Deliverables by PR

**N2-A — audio clip model + import.** Audio-kind assets on audio lanes:
`timeline.clip.place` accepts audio assets onto `kind:"audio"` tracks;
probe stores sample-rate/channels/duration; audio-only sources get a
synthetic fps mapping (samples→timeline frames via duration). Audio
lanes become placeable/selectable in the UI; EDL v2 already serializes
them. Migration: none expected (columns exist).

**N2-B — waveform display.** Off-thread waveform generation:
peak/RMS envelopes per clip computed once, cached in CAS
(`adapter_kind="analysis"`), rendered as an in-clip canvas strip.
Zoom-aware detail levels (LOD), cached per zoom band.

**N2-C — audio editing ops.** Trim (reuse `timeline.clip.trim`),
split (reuse split), gain: per-clip `gain_db` column (migration),
per-track `gain_db` on `timeline_tracks`, mute exists (`muted`),
solo: per-track `solo` column — mutually exclusive semantics
documented; UI: volume sliders, mute/solo buttons, fade handles.

**N2-D — fades + crossfades.** Clip `fade_in_frames`/`fade_out_frames`
(migration); crossfade = two overlapping clips on one lane with
matched fades (explicit command `timeline.audio.crossfade`, atomic);
graph: `afade`/`acrossfade` filters in the existing composite chain.

**N2-E — mix + export integration.** Audio lanes feed the composite
mixer: deterministic input order (track position), `volume=` filters
for clip/track gain, `afade` envelopes, `alimiter` master — same
single-pass render. Standalone audio-only export profile
(`aac`/`wav`) for dialogue/music stems.

**N2-F — sync + polish.** A/V offset nudge (sub-frame source trim),
waveform-to-video scrub alignment, audio-only preview playback
(`ffplay`/WebView2 audio element fed by a range request), benchmark
the same 10/100/500/1000 matrix.

## Semantics to pin down early

- **Sample-accuracy**: audio ops quantize to source samples like
  video ops quantize to source frames; sub-timeline-frame audio trims
  are legal in source units, never timeline fractions.
- **Overlap rules**: same-lane audio clips may overlap ONLY inside a
  crossfade region (enforced); elsewhere the existing no-overlap rule
  holds.
- **Solo**: any soloed audible lane mutes all non-soloed audio lanes;
  solo is render state, never clip mutation.
- **Undo**: all of the above snapshot through the same `_touch` →
  undo/redo covers N2 ops with zero extra work.

## Risks

- Waveform generation cost on long sources → LOD cache + durable job
  with progress/cancel, never synchronous.
- `acrossfade` needs same-sample-rate inputs → normalize to 48k in the
  graph (deterministic `aresample`).
- Mixed-rate sources → same normalization rule as N1-D video fps.
- Clip-gain vs track-gain ordering → fixed: clip gain → fade → track
  gain → mix → limiter.

## Estimated shape

6 PRs, each with unit + command + real-ffmpeg + installed-WebView2
coverage like the N1 series. No new job engine, export subsystem, or
history mechanism.
