# N2-A — audio editing foundation (implementation spec)

Scope: audio assets + audio lanes as first-class timeline citizens.
NOT in scope: waveform rendering, gain/fades/crossfades, audio-lane
rendering at export (N2-E), solo, preview playback.

## Data model — no schema migration

`timeline_tracks.kind = "audio"` already exists; `timeline_clips` is
source-agnostic. An audio clip reuses every column:

- `src_fps_num` / `src_fps_den` = **source sample rate** (e.g.
  48000/1, 44100/1). Source units are SAMPLES.
- `src_frames` = total source samples; `in_frame`/`out_frame` =
  sample indices → sample-exact trims with zero new machinery.
- `start_frames` = integer timeline frames at the timeline's
  fps_num/fps_den — unchanged.
- `clip_tl_extent` = (out−in)/rate × tl_fps → exact fractional
  timeline extent; occupancy rules already `_ceil`.
- Undo/redo, revisions, EDL pins, project isolation: free.

## Timing contract (Phase 6)

- Placement: `start_frames` is an integer timeline frame — a clip
  starts at the frame grid; sub-frame alignment is N2-F (nudge).
- Trims: integer source samples — never fractional.
- Conversion: samples↔timeline only through exact `Fraction`
  arithmetic; no cumulative float drift.
- Unrepresentable boundary: an audio clip's timeline EXTENT is
  rational (e.g. 1.5 frames). Occupancy/collision/`program_end` use
  ceiling semantics — documented maximum error is <1 timeline frame
  at the clip tail on the lane-occupancy grid; audible content is
  never truncated by occupancy (ceil, never floor).
- Export alignment (future N2-E): delays quantize to whole samples —
  max error <1 sample (≈21µs @48k, ≈23µs @44.1k).
- 44.1k and 48k sources supported; mixed rates on one timeline are
  legal (each clip carries its own rate).

## Import (Phase 4)

`asset.import` accepts `kind:"audio"`. Adapter runs a new
`engine.probe_audio()` (ffprobe on the CAS'd copy): requires ≥1 audio
stream; stores `media_json.probe = {ok, codec, container,
sample_rate, channels, channel_layout, duration_s, samples,
probe_backend}`. `samples` = stream `duration_ts` when the stream
time_base is 1/rate, else `round(duration_s × rate)` — documented as
container-derived. Probe failure → asset registers with
`probe.ok=false` + warning (fail closed: unusable on timelines).

Format matrix (decoders in the bundled LGPL ffmpeg build — decode
only; no encoders claimed):

| Extension | Format | Status |
|-----------|--------|--------|
| .wav      | PCM/WAVE | verified |
| .aiff/.aif| AIFF | supported |
| .flac     | FLAC | supported |
| .mp3      | MP3 | supported |
| .m4a/.aac | AAC | supported |
| .ogg      | Vorbis | supported |
| .opus     | Opus | supported |

Anything else ffprobe decodes imports too, but only the matrix is
acceptance-covered. UI infers kind by extension (matrix above +
existing video/image sets); explicit kind always wins.

## Lane ↔ asset kind rule

- `audio` track → `kind:"audio"` assets only.
- `video`/`overlay` track → `kind:"video"` assets only (unchanged).
- Enforced in `resolve_clip_source`-adjacent checks at
  `insert_clip` and `move_clip` — wrong-kind refuses with a clear
  message. Video-with-embedded-audio stays a video asset; extracting
  its audio onto an audio lane is out of scope.

## EDL / export (Phase 7)

- `ade-edl/2`: clip `source` block gains `sample_rate`, `channels`,
  `channel_layout`, `samples` when the version probe carries them —
  additive, deterministic, backward compatible.
- `ade-edl/1`: already refuses populated non-video lanes — inherited.
- `composite_plan` REFUSES when a visible audio lane holds clips:
  "audio lanes cannot be rendered yet (N2-E) — hide the lane or
  remove its clips". Hidden lanes contribute nothing (existing
  documented render decision); empty lanes are inert. Nothing is
  silently discarded.
- UI `exportMode` mirrors the rule: visible populated audio lanes →
  `blocked` with the same message.

## Commands (Phase 5)

`timeline.clip.place` (insert), `timeline.clip.move`,
`timeline.clip.remove`, `timeline.clip.split`, `timeline.clip.trim`,
`timeline.gap.*`, `timeline.undo/redo` all work on audio lanes through
the existing command path — preview→commit→audit, expected_revision,
idempotency, locked-track refusal, source-bounds validation in
SAMPLES.

## Storage vs usable

An audio asset is "storage-ready" once CAS-ingested + sha-pinned; it
is "usable" only when `media.probe.ok` is true (decodable, measured
rate+channels+duration). The UI marks unprobeable audio assets and
timeline placement refuses them.
