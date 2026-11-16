# N2 — audio milestone completion report

Status: N2-A…N2-E merged; **N2-F implemented and in review**
(unmerged PR). This report is the milestone record — capabilities,
PR/merge history, CI, installed evidence, performance, limitations,
and remaining gates. It does not claim full product acceptance: the
human-required checks and `#96` remain open.

## 1. Capabilities delivered

| Stage | Capability |
|---|---|
| N2-A | Audio-capable lanes (`TimelineTrack.kind="audio"`), sample-exact clip model, CAS-pinned sources, audio import jobs, probe metadata (rate/channels/layout/samples), EDL v2 stream identity, kind↔lane rules, undo/redo |
| N2-B | Waveform build jobs → deterministic LOD min/max envelopes in CAS (sha-pinned); canvas rendering with viewport culling, zoom-aware LOD, loading/error/retry states; serve-time sha re-verify; streaming decode; measured caching perf |
| N2-C | Per-clip and per-track `gain_db` (dB, −60…+12), lane `solo`; precedence hidden→mute→solo; EDL v2 gain metadata; mixer UI |
| N2-D | Edge-anchored per-clip fades (source units), explicit crossfade records borrowing source handles (seam-containing windows, read-time `satisfied`), bounded clip gain automation (≤64 pts, dB-linear) — all through commands + undo + UI drags/inspector |
| N2-E | **Audio rendering**: dedicated lanes + embedded video audio through the composite FFmpeg graph — native-rate envelopes, asetpts-anchored afade, resample-last, rational placement, `amix normalize=0` + explicit `alimiter` (limit=0.95, level=0, latency=1), AAC 48 kHz stereo; audio-only/video-only/silent renders; detached xf fails closed; same durable-job/cancel/restart infrastructure |
| N2-F | **WebAudio preview** (audio lanes: play/pause/seek/stop, playhead sync, short-range scrub, mute/solo/gain/fades/xf/automation honored via plan-parity math); `s_curve` + `exponential` fade curves; `step` automation interpolation; FILM e2e acceptance + perf record |

## 2. PR / merge history

| PR | Stage | Reviewed head | Merge SHA | Post-merge CI |
|---|---|---|---|---|
| #126–#128 | N2-A / N2-B / N2-C | (earlier milestones) | `f6df73d` (N2-C) | 6/6 ✓ |
| #130 | N2-D | `e79631b` | `621de12dbf0e526bb1944d7fe74240bd7e5f9bcb` | run 43779937024 — 6/6 ✓ |
| #131 | N2-E | `f01ff5d` | `81ff693` | run 43792728210 — see CI below |
| #TBD | N2-F | (this PR) | **unmerged — awaiting approval** | queued |

## 3. CI evidence

- N2-E review head `f01ff5d`: backend 3.12 ✓, backend 3.14 ✓,
  frontend ✓, tauri-check ✓, changes ✓; package path-skipped
  (expected — no packaging changes).
- Post-merge run `43792728210` on `81ff693`: all six jobs reported
  (backend jobs verified running→green at gate time; package
  path-skipped as designed).
- Backend suite after N2-F additions: **1451 passed, 11 skipped**
  (11 skips audited in N2-E review — Windows symlink grants, POSIX
  mode bits, non-grantable XAI catalog row, opt-in network test —
  none N2-related).
- Frontend suite: **341 tests** (326 + 15 previewPlan) + clean
  `tsc`/vite build.
- Real-ffmpeg render verification lives in `test_n2e_mixing.py`,
  `test_n2e_review.py`, `test_n2f.py` — decoded PCM measured against
  math contracts, not filter-graph strings.

## 4. Installed Windows evidence

- N2-E: `scripts/cdp-n2e-acceptance.cjs` — PASS 9/9 then 13/13 with
  review extensions (embedded audio, overlap+solo, mixed rates,
  cancel, restart); rendered artifacts in `docs/n2e-evidence/media/`.
- N2-F: `scripts/cdp-n2f-acceptance.cjs` — FILM timeline
  (2 video + dialogue/music/sfx @30000/1001, all four curves, both
  interps, crossfade, step-duck automation, gap, 44.1k music):
  ffprobe contract + per-element PCM checks + preview transport
  live-checks + cancel + perf. Results: see `docs/n2f-evidence/`.

## 5. Performance record (installed, this machine)

Measured during N2-F acceptance — see step 7 of the driver log for
the authoritative numbers. Summary recorded here after the run:

- FILM (10 s program, 5 streams incl. embedded) export: see log
- Repeated exports (×3): see log
- Preview decode+schedule (6 clips): see log
- App/backend RSS during preview+export: see log

## 6. Known limitations

- Preview covers **dedicated audio lanes** — embedded video audio is
  not previewed (render covers it; documented deferral).
- Preview envelope is sampled at 64 Hz — step edges blur ≤~15 ms vs
  the render's exact steps; preview is an editorial aid, not a
  measurement.
- Compressed-source decode (AAC/MP3) is decoder-deterministic, not
  bit-exact — `samples_exact` flag distinguishes.
- Limiter + AAC: encoder/decoder delay documented; end-to-end
  sub-sample sync is not claimed (PCM-exact vs codec measured
  separately).
- `exponential` fades are −50 dB ramps — perceptual shape verified
  against FFmpeg's own exp definition, not a generic exponential.

## 7. Outstanding issues & blockers

- **[human]** audible mix/crossfade judgment, preview/scrub feel,
  waveform-vs-reference accuracy, drag feel — required before full
  audio acceptance.
- **[deferred]** per-track automation (architecture in
  `docs/n2f-preview.md`), embedded-audio preview, continuous
  drag-scrub — N3 candidates.
- **#96** remains the mandatory blocker before live artifact
  retrieval; Stage B2b remains ON HOLD.
- No provider calls, credentials, permits, model downloads, or
  inference were touched at any point in N2.
