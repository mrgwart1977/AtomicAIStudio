# N2 — audio milestone completion report

Status: **N2-A…N2-F all merged** — PR #132 merged as `d67329d`
on reviewed head `cf5d390`; post-merge CI 6/6 green. This report is
the milestone record — capabilities, PR/merge history, CI, installed
evidence, performance, limitations, and remaining gates. It does not
claim full product acceptance: the human-required checks and `#96`
remain open.

## 1. Capabilities delivered

| Stage | Capability |
|---|---|
| N2-A | Audio-capable lanes (`TimelineTrack.kind="audio"`), sample-exact clip model, CAS-pinned sources, audio import jobs, probe metadata (rate/channels/layout/samples), EDL v2 stream identity, kind↔lane rules, undo/redo |
| N2-B | Waveform build jobs → deterministic LOD min/max envelopes in CAS (sha-pinned); canvas rendering with viewport culling, zoom-aware LOD, loading/error/retry states; serve-time sha re-verify; streaming decode; measured caching perf |
| N2-C | Per-clip and per-track `gain_db` (dB, −60…+12), lane `solo`; precedence hidden→mute→solo; EDL v2 gain metadata; mixer UI |
| N2-D | Edge-anchored per-clip fades (source units), explicit crossfade records borrowing source handles (seam-containing windows, read-time `satisfied`), bounded clip gain automation (≤64 pts, dB-linear) — all through commands + undo + UI drags/inspector |
| N2-E | **Audio rendering**: dedicated lanes + embedded video audio through the composite FFmpeg graph — native-rate envelopes, asetpts-anchored afade, resample-last, rational placement, `amix normalize=0` + explicit `alimiter` (limit=0.95, level=0, latency=1), AAC 48 kHz stereo; audio-only/video-only/silent renders; detached xf fails closed; same durable-job/cancel/restart infrastructure |
| N2-F | **WebAudio preview**: dedicated audio lanes **and embedded video audio** (`src_has_audio` persisted flag, migration `0018_n2f`), play/pause/seek/stop, playhead sync, short-range scrub, mute/solo/gain/fades/xf/automation honored via plan-parity math; `s_curve` + `exponential` fade curves; `step` automation interpolation; shared-fixture parity harness; FILM e2e acceptance + perf record |

## 2. PR / merge history

| PR | Stage | Reviewed head | Merge SHA | Post-merge CI |
|---|---|---|---|---|
| #126–#128 | N2-A / N2-B / N2-C | (earlier milestones) | `f6df73d` (N2-C) | 6/6 ✓ |
| #130 | N2-D | `e79631b` | `621de12dbf0e526bb1944d7fe74240bd7e5f9bcb` | run 43779937024 — 6/6 ✓ |
| #131 | N2-E | `f01ff5d` | `81ff693` | run 43792728210 — 6/6 ✓ |
| #132 | N2-F | `cf5d390` | `d67329dda3d1c2984f4af9ac4fddeb709bc409df` | run 43807362638 — 6/6 ✓ (incl. package NSIS build+upload) |

## 3. CI evidence

- N2-F review head: this report's commit on PR #132 (head recorded
  in the merge-gate entry). CI: see the PR checks — backend 3.12,
  backend 3.14, frontend, tauri-check, changes; package path-skipped
  is **not** treated as a build (the NSIS used for §4 evidence was
  built locally and installed before acceptance).
- Backend suite: **1464 passed, 11 skipped** — one flake observed
  (`test_c15_watch_polling` exit-race probe, timing-sensitive under
  concurrent build load; passes in isolation — not N2-related).
  The 11 skips were audited in the N2-E review (platform-grant /
  POSIX / opt-in-network items — none N2-related).
- Frontend suite: **335 passed** + clean `tsc`/vite build.
- Real-ffmpeg render verification lives in `test_n2e_mixing.py`,
  `test_n2e_review.py`, `test_n2f.py`, `test_n2f_parity.py` —
  decoded PCM measured against math contracts, not filter-graph
  strings.

## 4. Preview/render parity (final review additions)

**Shared language-neutral fixtures** — `fixtures/audio-parity/*.json`
(7 cases: basic gain/fade, automation linear+step, all four curves,
embedded video audio, mixed-rate trim, linear + equal-power
crossfades). Each fixture defines the timeline, probe facts, and
expected clip geometry + envelope checkpoints.

- `backend/tests/test_n2f_parity.py` builds `composite_plan` from the
  fixture EDL, runs the entry's REAL `_lane_chain`/`_embedded_chain`
  through ffmpeg on a constant signal, and asserts measured output
  at each checkpoint: **14/14 pass**.
- `apps/desktop/.../previewPlan.parity.test.ts` converts the same
  JSON to `TimelineDetail` and asserts `buildPreviewPlan` envelopes
  at the same checkpoints: **7/7 pass**.

**Real parity bug found and fixed in review**: WebAudio up-mixes a
mono buffer to stereo at full gain; the render's
`aformat=channel_layouts=stereo` uses the center-mix convention
(L=R=in/√2 — measured −3.01 dB). Preview played mono sources +3 dB
hot vs the export. `scheduleClip` now folds 1/√2 into the envelope
for mono buffers — measured in the installed app: embedded mono
preview 0.1776 vs export 0.1777, SFX mono 0.3535 vs 0.3534.

**Objective output verification** — `renderPlanOffline` schedules
the identical graph (shared `scheduleClip`) inside an
`OfflineAudioContext`; the installed driver renders the real FILM
plan to PCM and compares Goertzel magnitudes per element against
the decoded FFmpeg export. All nine comparisons within tolerance
(≤0.06 abs, dominated by AAC coding noise — see §4 of the driver
log for actual values).

## 5. Envelope resolution — 64 Hz verdict

Measured (installed driver, step 5a/5c — `renderPlanOffline`):

- Continuous envelopes (linear/qsin/hsin/exp fades, xf): within
  ±0.06 magnitude of decoded export PCM at every probe point —
  64 Hz sampling adds no measurable error for shipped curves.
- Step automation: edge blurs ≤ one sample period (~15.6 ms) vs the
  render's exact step — measured seek-into-duck parity 0.1255 vs
  0.1254.
- Seeked scheduling is position-invariant: seek-into-crossfade at
  tl 4.9 s reproduced the continuous render's envelope at the same
  absolute window (0.1946 vs 0.1947); seek-into-gap measured silent;
  boundary seek stayed silent before the 1.001 s clip start and
  played after.
- Envelope growth is bounded: `ceil(dur_s × 64) + 2` points per
  clip — no unbounded keyframe growth, no adaptive scheduler needed.

## 6. Transport reliability (installed)

- Play/pause/resume/stop verified via CDP: playhead advances while
  playing, freezes on pause, rewinds on stop.
- 24 rapid scrubs across gap/clip-boundary/xf/EOF positions: live
  node count stayed at the scheduled-window size (4), no
  accumulation; `stop()` drains to 0 live nodes.
- Workspace unmount (project switch) disposes the engine — the
  `__n2fPreview` hook and all scheduled gain events are released;
  no stale playback.

## 7. Installed Windows evidence

- `scripts/cdp-n2f-acceptance.cjs` on the NSIS-installed build:
  **PASS 11/11** — FILM @30000/1001 (h264 720p 300 f + aac 48 kHz
  stereo 10.010 s), 12 PCM checks, 9 preview↔export magnitude
  parities, 5 seek-envelope checks, transport, node-leak, dispose,
  cancel, perf, persistence. Artifacts: `docs/n2f-evidence/`
  (mp4 + wav committed).
- Migration `0017_n2f → 0018_n2f` runs cleanly on the installed DB
  at app start; `src_has_audio` backfills from the import probe.

## 8. Performance record (installed, this machine)

- FILM export (10 s program, 6 streams incl. embedded): **4.0 s**
  typical (one 7.7 s outlier under desktop contention).
- Backend CPU during 3 exports: **3.8 s over 15.8 s wall ≈ 24%**.
- Preview decode+schedule (6 clips, 12 s sources): **~3.6 s** one-time.
- Scrub restart: ~50 ms schedule lead (restart-based, not
  sample-accurate — human feel check remains [human]).
- App RSS flat **27→27 MB**, backend **~135 MB** across run.
- Waveform cache unchanged (N2-B path; no N2-F regression — waveform
  jobs are unchanged).

## 9. Known limitations

- Preview envelope is sampled at 64 Hz — step edges blur ≤~15 ms vs
  the render's exact steps (measured, bounded, documented).
- Scrub is restart-based, not sample-accurate — ~decode+schedule
  latency on long programs; continuous drag-scrub feel is [human].
- Preview does not apply the render's `alimiter` (preview can clip
  on hot mixes the render would tame — preview peak measured <0.9
  in acceptance; the limiter is a render-stage contract).
- Surround (>2ch) sources: preview down-mixes per WebAudio's speaker
  map; the render uses aformat's matrix — approximate, not verified
  (no surround fixtures).
- Compressed-source decode (AAC/MP3) is decoder-deterministic, not
  bit-exact — `samples_exact` flag distinguishes.
- Limiter + AAC: encoder/decoder delay documented; end-to-end
  sub-sample sync is not claimed (PCM-exact vs codec measured
  separately).
- `exponential` fades are −50 dB ramps — perceptual shape verified
  against FFmpeg's own exp definition, not a generic exponential.

## 10. Outstanding issues & blockers

- **[human]** audible mix/crossfade judgment, preview loudness feel,
  scrub/drag feel, waveform-vs-reference accuracy — required before
  full audio acceptance.
- **[deferred]** per-track automation (architecture in
  `docs/n2f-preview.md`), continuous drag-scrub — N3 candidates.
- **#96** remains the mandatory blocker before live artifact
  retrieval; Stage B2b remains ON HOLD.
- No provider calls, credentials, permits, model downloads, or
  inference were touched at any point in N2.
