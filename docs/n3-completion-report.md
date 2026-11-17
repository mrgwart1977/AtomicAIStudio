# N3 — Audio editing milestone — completion report

Scope: N3-A track automation → N3-B continuous scrubbing → N3-C editing UX →
N3-D hardening & acceptance (this report). Tested commit of the installed
build: the PR #139 head (`feat/n3d-hardening`, built post-fix). Installed
artifact:
`apps/desktop/src-tauri/target/release/bundle/nsis/Atomic Dream Engine_0.1.0_x64-setup.exe`
(76.19 MiB; backend 0.1.0, migration head `0019_n3a_track_auto`,
packaged FFmpeg n9.0.2). Evidence: `docs/n3d-evidence/n3d-acceptance.txt`
(11/11 steps),
`docs/n3c-evidence/n3c-acceptance.txt` (11/11), `docs/n3b-evidence/`,
plus the backend suite (1505 passed, 11 skipped).

## Milestone merges

| Step | PR | Merge SHA | Post-merge CI |
|---|---|---|---|
| N3-A track automation | #134 | `df58439` | 6/6 green |
| N3-B continuous scrubbing | #136 | `9824526` | 6/6 green |
| N3-C editing UX | #138 | `26face3` | 6/6 green |
| N3-D hardening + acceptance | #139 | `d29714c` | 6/6 green (incl. package: frozen sidecar + FFmpeg staged/smoke + NSIS uploaded) |

## N3 architecture summary

- **One model, one write path.** Timeline state lives in `timeline_*` tables;
  every mutation (trim, split, move/ripple, gain, mute/solo, fades,
  crossfades, clip automation, track automation, export) is a
  `CreativeCommand` — preview → commit → audit → revision. No parallel
  automation or audio model exists anywhere.
- **Revision + undo.** Commands carry `expected_revision` (stale → 409);
  timeline undo/redo are themselves commands (`timeline.undo|redo`) over
  per-timeline undo/redo stacks — one user edit = one undo step.
- **Preview.** `audioPreview.ts` builds a `previewPlan` from timeline detail
  and schedules bounded audition windows (≤350 ms render, ~20 ms lead,
  ≤45 ms coalesce) on WebAudio — live scrub and offline render share the
  same planner (`buildPreviewPlan` + `renderPlanOffline`), which is what the
  parity checks measure.
- **Export.** Durable `generation_jobs` row per export; FFmpeg supervised
  spawn with `cancel_event`, per-job work dir, post-render ffprobe
  verification, output registered as an asset. Copy/encode/composite path
  selection from the EDL.
- **Isolation.** All timeline rows carry `project_id`; cross-project commits
  and jobs 404 at the API.

## D1 — Full audio regression — **automated + installed: PASS**

| Requirement | Result | Evidence |
|---|---|---|
| Audio import | PASS — 8 assets (wav/mp4) imported via durable jobs | n3d step 0 |
| Waveform generation | PASS — canvas present; backend `test_n2b_waveforms` | n3d step 5; pytest |
| Trimming | PASS — in/out trims incl. nonzero source offsets | n3d steps 1–2 |
| Splitting | PASS — frame-exact split → 2 fragments share source pin | n3d step 2 |
| Ripple editing | PASS — explicit move retime; ripple_scope validated | n3d step 2 |
| Gain | PASS — clip −3 dB + track −6 dB audible in export | n3d step 3 |
| Mute/solo | PASS — muted lane inert; solo silences all other lanes incl. video | n3d steps 2, 4 |
| Fades | PASS — linear in / s_curve out / exponential set + undone | n3d step 2 |
| Crossfades | PASS — equal_power xf 24f, edit 24→28→24 via undo | n3d steps 1–2 |
| Clip automation | PASS — step duck −9 measured in export PCM | n3d step 3 |
| Track automation | PASS — linear duck on music + ramp on embedded | n3d step 3 |
| Embedded video audio | PASS — V1 220 Hz ramp + V2 280 Hz present in mix | n3d step 3 |
| Continuous scrubbing | PASS — 12/12 moves delivered, 0 coalesced, 13 auditions | n3d step 5 |
| Preview | PASS — engine loads, transport verified (N3-C step 7) | n3d 5–6, n3c 7 |
| Multitrack export | PASS — 6.006 s mp4, all lanes mixed | n3d step 3 |
| Undo/redo | PASS — split/move/xf/automation undo + redo verified | n3d step 2 |
| Restart persistence | PASS — full relaunch → rev/clips/xf/automation intact | n3d step 9 |

## D2 — Preview/export parity — **installed: PASS**

Method: `renderPlanOffline` (the live engine's own planner + scheduler)
rendered at tl windows vs decoded PCM of the FFmpeg export — Goertzel
magnitudes at the fixture frequencies, ±40% tolerance.

| Coverage | Result |
|---|---|
| Frame rates | 30000/1001 container-verified `r_frame_rate` (n3d 3); 30/1 (n3c 2); other rates covered by `test_c5b_export` fps paths |
| Mixed sample rates | PASS — 48 k stereo + 44.1 k stereo (music) + embedded AAC mixed |
| Mono/stereo | stereo verified installed; mono assets imported (mono48k/mono441) — mono mix covered by `test_n2a_audio`/N2-E suite |
| Embedded audio | PASS — V1+V2 embedded tracks audible in export and render |
| Gain stacking | PASS — clip(−3)×automation(−9 step)×track(−6) all measurable |
| Interpolation | linear + step verified in render and export PCM |
| Fade curves | linear, s_curve, exponential, equal_power (xf) exercised; shapes unit-tested in `test_n2f` |
| Crossfade handles | insufficient-handle refusal verified (n3c 6); 28f head bound found |
| Seek into automation | PASS — render from tl 0.8 s already carries step-automation gain |
| Timeline gaps | PASS — sfx gap + post-program silence measured |
| Short/long programs | PASS — 6 s film + ~2 min programs at 240/500/1000 clips all export (below) |

**Not claimed:** sample-exact preview — tolerance-based parity only.

## D3 — Reliability — **automated + installed: PASS**

| Check | Result | Evidence |
|---|---|---|
| Export cancellation | PASS — job ends `cancelled`, no output asset | n3d step 7; `test_export_cancel_terminates_and_preserves_sources` |
| Backend interruption / restart reconciliation | PASS — interrupted resumable jobs requeue; reconcile marks + bumps attempt | `test_jobs.py::test_interrupted_resumable_job_requeued`, `test_m4.py` reconcile tests |
| Missing source | PASS — fails safe | `test_export_missing_source_bytes_fails_safe` |
| Hash mismatch / corrupt bytes | PASS — fails closed | `test_export_corrupt_source_bytes_fails_closed` |
| Invalid streams | PASS — clip with no decodable video stream refuses; corrupt bytes fail closed | adapter validate + c5b tests |
| FFmpeg failure | PASS — nonzero exit unlinks output + error propagates | adapter code + cancel test |
| Disk-space refusal | PASS — `insufficient disk` preflight | `test_c4e_resources` + adapter |
| Temp cleanup | PASS — cancelled job leaves no partial output; job work dirs retain artifacts by design | n3d step 7 |
| No phantom output | PASS — no asset registered on cancel; output verified by ffprobe before registration | n3d step 7; adapter |
| No orphan FFmpeg | PASS — supervise.run kills on cancel; `tasklist` clean | n3d step 7 |
| Durable-job / auth behavior | PASS — durable rows, `idempotent_replay` verified, 404 cross-project | n3d step 7; suite |
| Export argv bound | **RESOLVED (was WinError 206 @ ~240 segs)** — root cause was BOTH the filter graph (~203 KiB at 240 clips) and one `-i` per clip (~26 KiB). Fix: filter graph written to a temp script in the job work dir and passed via `-/filter_complex <file>` (the FFmpeg ≥7 option-argument mechanism — `-filter_complex_script` was removed in FFmpeg 8); `-i` inputs deduplicated by source path with `split`/`asplit` fan-out; unique sources get short hardlink aliases in the job dir (~770 B argv at 1000 clips on few sources). Remaining bound ≈ ~750 *unique* sources on one timeline — a staged submix render would be needed past that (documented, not hit). | n3d step 8; `test_n1d_composite` argv/dedupe tests |

Backend suite evidence (D1/D2/D3/D9): full suite **1504 passed, 11
skipped** (~20 min) — incl. `test_c5b_export, test_jobs, test_m4,
test_n2f, test_n2f_parity, test_n1e_perf, test_n1e_undo,
test_n1d_composite` (argv-bound + input-dedupe + script-lifecycle
tests), `test_n3a_track_automation`.

## D4 — Performance — **installed: measured**

| Metric | Measured | Baseline |
|---|---|---|
| Clips placed via commits | 240 in 6.2 s / 500 in 15.4 s / 1,000 in 42.6 s | — |
| Large-timeline exports | 240-clip → 73.8 s; 500-clip → 189.2 s; 1,000-clip → 498.3 s; all produce 119.5 s h264/aac output (~2.3 MB) — argv bound resolved | — |
| Virtualized DOM | 3–5 clip nodes mounted for 120–240-clip timelines | 5 (n3c) |
| Dense automation | 200-pt + 200-pt lanes committed/rendered | 200 pts (n3c) |
| Zoom settle | ~12–20 ms | 12–18 ms (n3c) |
| Engine load | ~1.2–1.4 s (120 clips) | ~1.2 s (n3c) |
| Scrub | 12/12 delivered, sched ≤0.6 ms | same (n3c) |
| Repeated exports | 4.0 s, 3.4–4.1 s, 4.0 s (film, cold→warm) | — |
| Memory | app RSS ~27–28 MB stable across exports; backend 138–146 MB | 27 / 103–144 (n3c) |
| CPU | no runaway observed during repeats | — |
| Audio-node cleanup | auditions counted, no accumulation (13 auditions → engine steady) | — |
| UI responsiveness | interactive during LONG ops (human feel in checklist) | — |

## D5 — Installed Windows E2E — **installed: PASS 11/11**

Representative film `FILM` @30000/1001 (29.97): 2 video lanes (220 Hz +
280 Hz embedded audio), A1 dialogue pair + equal_power xf, A2 music
44.1 kHz w/ fades + lane duck, A3 sfx tiny clip + gap; clip gain, track
gain, clip+track automation, mute/solo, split/move/undo edits. Editing
ops, scrub, preview, export (ffprobe: h264 30000/1001, aac 48 k stereo,
6.006 s), decoded PCM battery (10 checks) + **pre/post-fix ffprobe and
PCM-window comparison (D5: codec, rational fps, audio spec, duration,
all sampled windows identical)**, solo export (3 checks), parity (4
checks), cancel, restart persistence, clean close, **large-timeline
exports at 240/500/1,000 clips (D4)**, and **pre-N3 populated-project
migration regression (D7)** — all green on the real NSIS/WebView2 app
(`docs/n3d-evidence/`).

### D7 — pre-N3 populated-project regression (step 11)

Genuine fixture, not synthesized: `backend/tests/fixtures/
pre_n3_release.db` was generated by the actual release code at merge
`663cb5d` (post-N2-F, last pre-N3 build; alembic head `0018_n2f`) —
project "Pre-N3 Feature" with character/world/location bibles, scene +
3 shots + cast, a 30000/1001 timeline (rev 12) with clip gain/fades/
step+linear automation, per-lane gain, a 24f equal-power crossfade, a
two-entry undo stack backed by real `entity_revisions` snapshots, and
four sha-pinned WAV sources staged into CAS blobs. The installed app
migrates it `0018→0019` on open: **all entities, lanes, gains, fades,
automation, crossfade, and revision history verified**; new N3 track
automation commits on the migrated lane; undo/redo across the
migration boundary verified; `backup.export` produces an `.atomicpkg`;
a relaunched instance re-opens the migrated project intact.

**Real defect found by this fixture and fixed:** `_restore_snapshot`
restored only track fields *present* in an old snapshot
(keep-if-absent), so undoing across the pre-0019 boundary leaked the
post-edit `automation_json`/`automation_interp` — clips already
defaulted missing keys, tracks now match (column defaults). Guarded by
`test_undo_across_pre0019_boundary_clears_automation`.

## D6 — Human acceptance — **human-required: prepared, NOT evaluated**

`docs/n3-human-checklist.md` — 24-check listening/editing-feel matrix
(scrub feel, cancel behavior, parity sanity, editing feel, long-program
responsiveness). **No human has run it — not marked passed.**

## D7 — Remaining limitations & blockers

New from N3-D:
- The `filter:waveform` for very long programs and the 176 px sticky
  header (now responsive ≥96 px) remain documented quirks, not defects.
- Resolved this milestone: the Windows export argv bound
  (WinError 206 @ ~240 segs) — see D3; and the pre-0019
  undo-across-migration leak — see D7.
- **Remaining scalability bound:** FFmpeg argv stays under Windows'
  limit via the script file + input dedupe + job-dir aliases, but
  ~**750 unique sources on one timeline** would still overflow input
  arguments (each unique source needs one `-i`; ~40-char aliases).
  Timelines beyond that need a staged submix render — **not
  implemented, not claimed; requires separate authorization**.
- **Intermittent test flake (tracked):**
  `test_c15_watch_polling.py::test_process_exit_is_bounded_with_stalled_scan`
  hung >30 s once on post-merge CI (backend 3.12, `d29714c`); passed on
  rerun, on 3.14, and locally. Recorded as RELEASE_CHECKLIST #14.

Carried (pre-existing, unchanged): Stage B2b ON HOLD; issue #96 mandatory
before live artifact retrieval; no live providers/credentials/paid work;
v1.0 blockers in RELEASE_CHECKLIST unchanged.

## Classification summary

| Requirement | Class | Status |
|---|---|---|
| D1 regression | automated+installed | PASS |
| D2 parity | installed (+backend unit) | PASS (tolerance-based) |
| D3 reliability | automated+installed | PASS — argv bound resolved |
| D4 performance | installed | measured, within baseline |
| D5 installed E2E | installed | PASS 11/11 |
| D6 human acceptance | human-required | prepared, not run |
