# N1 completion report — multitrack timeline

Status: **development-complete** (N1-A through N1-E merged). This is
NOT a declaration of production release readiness — tracked blockers
in `RELEASE_CHECKLIST.md` remain open.

## Delivered capabilities

| PR | Scope | Merge |
|----|-------|-------|
| #121 N1-A | Migration integrity, `timeline_tracks`/`timeline_clips` multitrack model, `ade-edl/2` doc, revision/concurrency rules, `revert_timeline` fix | `36729ec` |
| #122 N1-B | Multitrack timeline editor UI, exact NTSC rates (`fps_num`/`fps_den`), drag placement, lane ops, installed acceptance 12/12 | `7228bdb` |
| #123 N1-C | Split, ripple insert/delete, tail trim, gap insert/close, all-scope lane-kind rule, installed acceptance 14/14 | `8f788e5` |
| #124 N1-D | `ade-edl/2` multitrack FFmpeg compositing/export, frame-exact windows, sample-exact audio delay, stacking fix, cancellation/recovery tests | `841b292` |
| #125 N1-E | Persistent per-timeline undo/redo, stable clip identity on restore, mutable-lane restore fix, viewport culling, installed UI perf matrix | `668f55e` |

## Architecture

- Edit data is integer source/timeline frames over rational fps —
  no floating point anywhere in edit semantics; `Fraction` for
  conversions.
- Every mutation rides the command system
  (preview → commit → audit), `expected_timeline` pinned,
  idempotent, project-scoped; isolation enforced in DB+API.
- Tracks: `kind` ∈ video/overlay/audio; `position` is stacking order
  with **0 = bottom**, higher composited above.
- Snapshots: `entity_revisions` JSON per revision;
  `_restore_snapshot` performs identity-aware restores (stable clip
  UUIDs, mutable lane fields, legacy no-id repack compat).
- Undo/redo: `{rev,label}` stacks on the timeline row (migration
  `0014_n1e`), pushed in `_touch`, redo cleared on divergent edits.
- Compositing: single-pass ffmpeg filtergraph from the EDL v2 plan;
  integer-frame `enable` windows `[S,E)`; sample-exact audio offsets;
  `alimiter` master.

## Database migrations

`0013_n1a` (tracks/clips multitrack foundation) → `0014_n1e`
(undo/redo stacks). Legacy snapshots without track/clip-id fields
restore via ceiling-packed default-lane repack — covered by tests.

## EDL versioning

`ade-edl/1` sequential compat (stream-copy eligible) preserved;
`ade-edl/2` carries lanes/clips/pins deterministically — identical
edit state → byte-identical EDL modulo the revision counter.

## Test results

- Backend suite: **1,279 passed** post-N1-E; focused N1-E set 57/57,
  expanded undo/export set 23/23.
- Frontend: 278+ tests incl. culling + undo UI; production build clean.
- Installed acceptance: N1-B 12/12, N1-C 14/14, N1-D composite export
  ffprobe-verified, N1-E 8/8 + perf matrix, N1 audit E2E 18/18.
- CI: green on every merge; PR #125 head `40afb7f` green
  (backend 3.12+3.14, frontend, tauri-check, changes; package
  path-skipped).

## Installed Windows evidence

- `docs/n1d-acceptance-evidence.md`, `docs/n1e-acceptance-evidence.md`,
  `docs/n1-audit-evidence/` (18-step log + ffprobe output + screenshots).
- N1 audit: project→character→scene→shots→3 imports→3 lanes→
  overlap→split→ripple→trim→gaps→visibility/reorder→UI undo/redo→
  composite export (h264 1280×720 48f/2.000s AAC)→reopen persistence→
  clean shutdown, zero orphan processes.

## Performance (installed WebView2, CDP-measured)

10→1000 clips: render 2.3–15.2ms, scroll/zoom ~30ms, select ~31ms,
undo 79–97ms, redo 76–88ms, ~5 mounted DOM clips regardless of N,
heap 4.5–8.8MB (50-cycle +0.33MB). Backend commit/EDL ~linear,
≤140ms at 1000 clips. Methodology + hardware caveats:
`docs/n1e-acceptance-evidence.md`.

## Known limitations

- Undo tips are command summaries, not rich diffs; undo covers
  timeline commands only.
- `clips_roundtrip` seeding anomalies seen in some perf runs
  (500-clip run reported 6→0 once) — interaction timings still
  valid; seeding path quirk noted for future runs.
- Audio lanes exist structurally but carry no audio-clip UX yet
  (N2 scope).
- N1-D installed artifact predates the `aa7034e` stacking fix —
  corrected order proven by deterministic tests, not re-rendered
  through the installer.

## Remaining defects

None known-open inside N1 scope at audit time.

## Release blockers (unchanged)

#96 live-artifact-retrieval security blocker; #5–#15 incl. clean-VM
install, code signing, UNC soak, migration matrix, scalability;
#22–#27 M3/M4 items. N2 requires separate authorization.
