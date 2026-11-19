# C3 — Scalability: closeout record

## Merge

- **PR:** #160 — final reviewed head `f269882`
- **Merge SHA:** `3298ae6866b76fb5fe9bb669d65ed1413b4e65f1` (squash)
- **Post-merge main CI:** run `44565881201` — **6/6 success**
  (changes, backend 3.12, backend 3.14, package, frontend,
  tauri-check); frozen backend + NSIS built by the package job
- **Migration head:** `0026_a7` (unchanged — no schema work in C3)

## Review-gate resolutions (Phase 1 verification)

- **C3-F1 CI:** backend 3.12 + 3.14, frontend, tauri-check, changes all
  green on `f269882`; `package` skipped on PR (runs post-merge only) —
  *first run found a CI-only encoder gap, fixed in `f269882`, re-run
  green*.
- **C3-F2 large export:** real 1000-distinct-source staged export —
  995 frames / 41.46 s @ 24 fps, pixel-level source contribution checks,
  peak ffmpeg RSS 760 MB, peak work-dir temp 1.7 MB, intermediates
  cleaned, zero `CreateProcess` failures
  (`docs/c3-evidence/large-export.md`).
- **C3-F3 boundary correctness:** post-envelope stage trim preserves
  fades/crossfades/clip automation/track automation/overlap at window
  cuts; staged-vs-direct PCM parity peak <0.05 / RMS <0.005; frame-exact
  at 24 and 30000/1001 fps (fractional bounds snap up).
- **C3-F4 capacity:** exactly-200 overlapping sources pass, 201 refuse
  before render with actionable error; unicode + long paths; gaps;
  single-frame windows; 50-stage partition test.
- **C3-F5 failure recovery:** cancel before first stage and between
  stages; stage-render failure; final-concat failure; missing + corrupt
  sources refuse pre-render; nothing partially published; intermediates
  unlinked in `finally`.
- **C3-F6 legacy encode path:** actual argv measured post-alias;
  >30,000 chars refuses preflight with an actionable message — no path
  can trip `CreateProcess` unpredictably; staged path unchanged
  semantically (single limiter pass on the whole program).
- **C3-F7 performance evidence:** planning / probing / stage-render /
  final-encode / end-to-end separated in
  `docs/c3-evidence/large-export.md`; planning sweep 1.566 s → 0.058 s
  at 1500 sources (planning only — no end-to-end speed claim);
  `docs/c3-evidence/baseline.md` has the app-level measurements.
- **C3-F8 installed provenance:** frozen sidecar + NSIS built on
  `ab085a2` (shipped code identical to `f269882` — delta is tests,
  driver, docs only); `ADE_STAGE_MAX_INPUTS=1` forced the staged
  machinery in the installed app — a pre-commit process monitor
  captured 2 `<job>-stageN.mkv` ffmpeg invocations + concat encode;
  bundled ffprobe validated h264 output; 12/12 acceptance PASS
  (`docs/c3-evidence/acceptance.txt`, `ffmpeg-monitor.txt`).

## What shipped

- `app/domain/timeline_export.py` — `stage_windows` (O(bounds+events)
  sweep, fractional-bound ceil), `stage_subplan` (post-envelope trim
  contract: full stream + untouched stream-local anchors, window cut
  after envelopes), `build_stage_argv` (lossless FFV1 + pcm_f32le mkv,
  no limiter), `build_final_argv` (concat demuxer + single limiter
  pass), bounded-concurrent source probing, `ADE_STAGE_MAX_INPUTS`.
- `app/adapters/timeline_export.py` — staged branch on the composite
  path, argv-length preflight refusal on both single-pass paths,
  video-clip PTS carries timeline position (pre-existing composite bug
  — only frame-0 clips ever drew; found by pixel verification).
- Tests: `test_c3_staged_boundaries.py` (15), `test_c3_staged_export.py`
  (13) — 1,978 backend tests total, 11 skipped, 0 failures.
- `backend/scripts/c3_baseline.py`, `scripts/cdp-c3-acceptance.cjs`
  (12-check driver), `scripts/c3-install.cjs` (NSIS /S helper).
- Evidence: `docs/c3-evidence/{export-baseline,staged-submix,
  large-export,baseline}.md` + probe/monitor artifacts.

## Remaining limits / non-goals

- Serial per-source ffprobe is process-spawn-bound on Windows
  (~1.3 s/spawn): 1000 sources ≈ 20 min serial, ~8 min at 8-way —
  batch-probe/probe-cache is future work.
- >200 simultaneously-overlapping unique sources refuse (`stage
  capacity`) — a real authoring ceiling, not silent degradation.
- ≤1-sample audio re-anchor rounding at stage boundaries at
  sample-exact rates (documented; parity tolerances far above it).
- v1 `encode` path keeps the single-pass argv bound (refuses
  explicitly); `copy` and staged composite are unbounded.
- Frontend timeline rendering unchanged — backend serialization scales
  linearly (baseline doc).
- Stage B2b ON HOLD; issue #96 open; all adapters `adapter_kind="mock"`.
