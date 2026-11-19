# C3-F2 / C3-F7 — Large-scale real export evidence

## Workload (reproducible)

`backend/tests/test_c3_staged_boundaries.py::
test_export_1000_distinct_sources_real`

- **1000 byte-distinct synthetic mp4s** (16-level RGB palette —
  survives yuv420p chroma quantization; `sha256` uniqueness asserted).
- Each placed as a **1-tl-frame** clip on an `ade-edl/2` timeline:
  V1 = frames 0–499, V2 = frames 495–994 (5-frame lane overlap so the
  composite path exercises real multitrack blending).
- 32×32 @ 24 fps, program = 995 frames ≈ 41.46 s.
- Probe metadata stubbed for re-runnability (see "probe bottleneck"
  below) — every ffprobe/ffmpeg **render** step is 100% real:
  stage_windows → stage_subplan → per-stage ffmpeg renders → concat
  demuxer → final h264 encode → pixel verification.

## Result (PASS)

| metric | value |
|---|---|
| exit | succeeded — no CreateProcess failure at any stage |
| output frames | **995** (exact) |
| duration | 41.46 s (expected 41.458) |
| fps | 24/1 — rational preserved end to end |
| source contribution | frame-color spot checks at frames 0,123,250,494 (V1) and 497,700,994 (V2/top lane) — all match source palette ±4/255 |
| peak ffmpeg RSS | **760 MB** (psutil sampler, 2 Hz) |
| peak temp disk (work dir) | **1.7 MB** (aliases + concat list + at most 2 intermediates before cleanup) |
| intermediates after run | none — stage mkv/graph/concat files all unlinked |
| end-to-end wall time | 28.4 s (warm) / 211.7 s (cold incl. source generation) |

## C3-F7 timing separation

| phase | value | note |
|---|---|---|
| planning | ~0.05 s | `stage_windows` sweep at 1500 sources (baseline doc) |
| probing (real) | ~20 min serial / ~8 min @8-way | see bottleneck below |
| stage rendering | ~25 s total | 6 stages × ~4 s (32×32 workload) |
| final encode | ~3 s | concat demuxer + single limiter pass |
| end-to-end (render only) | ~28 s | warm path, 995-frame program |

No end-to-end speed claim is made beyond these measurements; the
planning sweep optimization (1.566 s → 0.058 s) is a planning-phase
improvement only.

## Probe bottleneck finding (C3.5 datapoint)

Serial per-source ffprobe is process-spawn-bound on Windows (~1.3 s
each): 1000 sources ≈ **20+ min serial**. Bounded-concurrent probing
(8-way `asyncio` semaphore + gather, deterministic order) cut this to
~8 min. Both are environmental-spawn-dominated; a future batch-probe or
probe-cache would address it properly.

## Compositing bug found & fixed during F2

Frame-pixel verification exposed a **pre-existing composite bug**:
clip chains reset `setpts=PTS-STARTPTS`, so every clip's frames were
consumed at output time 0 — with `eof_action=pass` only clips starting
at frame 0 ever drew. All earlier exports validated structure/duration,
never pixels. Fixed by carrying each clip's timeline position in PTS
(`setpts=PTS-STARTPTS+{tl_start_s}/TB`). PCM/audio parity was
unaffected (audio positions via leading-silence, a different and
correct mechanism).

## C3-F8 — Installed staged-export verification (`ab085a2`)

Frozen PyInstaller backend + NSIS 76.4 MiB built from `ab085a2`,
installed silently. `scripts/cdp-c3-acceptance.cjs` (12 checks) ran
with `ADE_STAGE_MAX_INPUTS=1` to force the staged machinery on the
assembled timeline (~2 unique mock sources + duplicate overlay lane →
`ade-edl/2` composite, windows split at clip boundaries).

- A tight-loop process monitor (50 ms CIM sampling, started **before**
  the export commit — a ~2 s job outruns post-commit polling) captured
  **2 distinct `<job>-stageN.mkv` ffmpeg invocations** followed by the
  concat/final encode — the staged pipeline provably ran inside the
  installed app (`docs/c3-evidence/ffmpeg-monitor.txt`).
- Output validated by the **bundled** ffprobe
  (`backend/_internal/ffmpeg/bin/ffprobe.exe`): h264, 8.0 s.
- **12/12 checks PASS** (`docs/c3-evidence/acceptance.txt`): unauth
  refusal, 20-shot storyboard, 24-job queue, CAS browse, sim-only
  surface + live refusal, backup/restore, staged export, cancellation
  → terminal, restart persistence, 1.4 s bounded shutdown, no orphans.
- Shipped-code delta `ab085a2..6226b8e`: `git diff --stat` = zero
  `backend/` + `apps/` changes — the installed binary is the merged
  head's code.
