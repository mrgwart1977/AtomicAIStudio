# N2-B — waveform envelopes & timeline visualization

Status: implemented on `feat/n2b-waveforms`. Audio **rendering/mixing
remains out of scope until N2-E** — this milestone is decode → derived
display data only.

## Contract

A waveform is **derived data**, never a new source asset: deterministic
min/max peak buckets computed from the pinned source bytes, stored as
CAS blobs alongside (not instead of) the original audio.

- **Bucket grid is absolute.** Bucket `b` covers source samples
  `[b*spb, (b+1)*spb)` at the source sample rate — a fixed lattice, so
  a clip's `[in_frame, out_frame)` sample trims map to bucket ranges
  with plain arithmetic. No per-clip offsets, no drift.
- **Levels (LOD).** Samples-per-bucket starts at `BASE_SPB = 256` and
  grows ×4; the finest level is raised until its bucket count is
  ≤ `MAX_FINEST_BUCKETS = 250_000`; levels stop once the previous level
  has < `MIN_USEFUL_BUCKETS = 64` buckets or `MAX_LEVELS = 6` is hit.
- **Peaks.** Signed 16-bit `[min, max]` per channel per bucket — the
  smallest lossless rendering primitive for s16 sources; float/24-bit
  decodes are quantized to s16 by the decode command.
- **Deterministic.** Same source bytes → byte-identical level docs
  (fixed JSON serialization, no floats, no timestamps). Level docs are
  `ade-wave-level/1`; the manifest pinned on the version is
  `ade-wave/1`.
- **Bounded memory.** ffmpeg decodes stream `0:a:0` (the same
  authoritative stream `probe_audio` used) to a temp s16le file with
  `-xerror`; the envelope pass streams it in 1 MiB reads — a 3-hour
  recording never enters RAM. Temp files are `{job_id}-` prefixed so
  job cleanup + crash sweeps remove them.

## Provenance & caching

- The manifest is stored on `asset_versions.media_json.waveform` and
  pins `built_sha256 = <version sha256>`, `sample_rate`, `channels`,
  `samples`, and per-level `{spb, buckets, sha256}` of each CAS blob.
- `asset.waveform` is a durable `ops.asset.waveform` job (idempotent
  command, cancel-aware decode + envelope passes, restart marks
  interrupted per the job engine).
- **No unnecessary regeneration:** if `built_sha256` already equals the
  current version sha, the job returns `{skipped: "waveform up to
  date"}` without touching CAS. Level blobs are content-addressed, so
  re-runs across versions dedupe automatically.
- The serving endpoint returns 409 when no waveform exists and 409
  "predates the current source version" when the manifest's
  `built_sha256` doesn't match — the UI says *rebuild*, never silently
  shows stale peaks.

## Endpoints (project-scoped, session-authed)

- `GET /api/projects/{pid}/assets/{aid}/waveform` — manifest.
- `GET .../waveform/level?level={spb}&start_bucket={s}&end_bucket={e}`
  — a viewport-bounded slice of one level's peaks. Bounds clamp to the
  level's bucket count; the client requests only what the viewport
  needs.

## Frontend

- `waveform.ts` — manifest fetch (409 → "missing", other failure →
  "error"), promise-level caches, FIFO-bounded slice cache (256
  entries), `pickLevel()` chooses the first level with `spb ≥
  samples-per-pixel` (coarsest adequate → minimal data).
- `TimelineEditor` renders a `<canvas>` inside audio `LaneClip`s only —
  viewport-culled like the clip itself; slices are fetched for exactly
  the visible bucket range. Multi-channel rows stack vertically.
- States: `waveform…` (fetching/polling for a build), `no waveform`
  (poll exhausted), `waveform unavailable` + retry (fetch failure).
  The canvas is `pointer-events-none` — drag/trim/keyboard behavior is
  untouched. `ClipInspector` offers **Build waveform** and labels audio
  trims as *samples @ Hz*, not frames @ fps.

## Editing contract (unchanged from N2-A)

All N2-A rules hold: `in_frame/out_frame` are **source-sample indices**,
`src_fps_num/den` the sample rate, `start_frames` integer timeline
frames; trims are sample-exact, splits refuse non-integral source
boundaries, occupancy uses exact `Fraction` ceiling semantics
(< 1 tl-frame max error, never truncated). Move/trim/split/keyboard/
undo/lock/stale-revision behavior is inherited wholesale — N2-B adds
display data only.

## Not in scope

Audio mixing/rendering (N2-E), gain/fades/mute-solo (N2-C/D), audio
preview playback, waveform regeneration on trim (trims map onto the
absolute bucket lattice — no rebuild needed by design).
