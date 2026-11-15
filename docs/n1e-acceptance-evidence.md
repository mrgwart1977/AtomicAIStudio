# N1-E acceptance evidence — undo/redo + timeline performance

## Architecture

Undo/redo is per-timeline `{rev, label}` pointer stacks
(`undo_stack_json` / `redo_stack_json`, migration `0014_n1e`) into the
existing `entity_revisions` snapshots — no separate history database.
Every timeline mutation's `_touch` pushes the **pre-edit** revision and
clears the redo stack (a divergent edit invalidates the abandoned
branch). `timeline.undo` / `timeline.redo` are ordinary commands:
preview → commit → audit, `expected_timeline` pinned, idempotent,
project-scoped. Undo restores via the shared `_restore_snapshot` used
by `revert_timeline`.

A latent defect was fixed in `_restore_snapshot` while landing this:
lane field mutations (position/name/visible/locked/muted) were not
restored — a track reorder or visibility toggle could not be reverted.
The function now writes mutable lane fields back from the snapshot for
tracks that still exist, which also corrects `timeline.revert`.

## Backend tests — 14 undo + 2 perf guards

`backend/tests/test_n1e_undo.py`: undo/redo roundtrips for clip
remove, split, ripple delete, trim, track remove (lane + clips),
track reorder; 3-step undo walk + 3-step redo walk; redo-branch
invalidation on divergent edit; stale `expected_timeline` refusal;
idempotent replay; empty-stack 422s; EDL-shape determinism (restored
rows carry fresh ids — snapshots recreate rows; layout fields that
EDL v2 keys on are asserted equal); label interpretability; cross-
project isolation.

`backend/tests/test_n1e_perf.py`: EDL v2 serialization and snapshot
of a 1000-clip timeline each under 1 s; detail endpoint under 2 s.

## Performance — measured on this machine

Method: `scripts/n1e-bench.cjs` against the dev backend
(uvicorn + SQLite on localhost, Windows 11, release build of the app
NOT used — numbers are API-side latency, not paint). Each row seeds a
fresh project, imports one real video, and places N clips via real
`timeline.clip.place` commits (fill_ms_per_clip reports that amortized
cost, including a revision write per commit).

| clips | detail (ms) | EDL v2 (ms) | EDL v1 (ms) | commit (ms) | undo (ms) |
|------:|------------:|------------:|------------:|------------:|----------:|
|    10 |         6.8 |         7.7 |         7.0 |        13.3 |      19.2 |
|   100 |         8.9 |        15.4 |        14.4 |        14.7 |      35.9 |
|   500 |        34.1 |        77.3 |        68.2 |        36.7 |     136.4 |
|  1000 |        43.0 |        90.4 |        73.3 |        28.3 |     138.0 |

Scaling is ~linear in clip count for serialization paths (O(n) by
construction); undo is O(snapshot rewrite) ≈ clips — expected for a
snapshot-based history. 1000-clip timelines stay inside interactive
budgets (<150 ms server-side). Memory: not separately metered — SQLite
rows dominate; no measurement is claimed.

Frontend: `LaneClip` was already `memo`'d. Added horizontal viewport
culling — only clips/gaps intersecting the scrolled pixel window
(± one screen) mount; the scroll handler updates a `{l,r}` window.
Culling decides what mounts, never edit math or clip data.

## Installed-Windows UI performance — MEASURED (real WebView2)

Method: `scripts/cdp-n1e-perf.cjs` on the installed NSIS build
(`Atomic Dream Engine_0.1.0_x64-setup.exe`, stable-identity head
`7a84105`), timing `performance.now()` around REAL interactions —
scroll events, zoom clicks, clip pointer events, Undo/Redo button
clicks — plus `performance.memory` heap and a 50-cycle undo/redo
long-session. Machine: Windows 11 10.0.26300, WebView2 Chromium,
localhost sidecar backend. Data: `docs/n1e-evidence/n1e-perf.json`.

| clips | DOM mounts | scroll→end | zoom | select | undo (UI) | redo (UI) | heap |
|------:|-----------:|-----------:|-----:|-------:|----------:|----------:|-----:|
|    10 |          5 |    27.0ms | 27.1ms | 30.9ms |    83.7ms |    75.6ms | 5.7MB |
|   100 |          5 |    29.6ms | 29.4ms | 31.3ms |    88.6ms |    87.8ms | 4.5MB |
|   500 |          5 |    30.2ms | 30.6ms | 31.0ms |    79.2ms |    81.4ms | 8.8MB |
|  1000 |          5 |    30.3ms | 30.5ms | 31.1ms |    96.8ms |    81.3ms | 8.7MB |

DOM mounts stay ~5 regardless of timeline size — viewport culling is
doing its job (3 mounts at the far-end scroll position). Interaction
latency is ~30 ms for paint-level actions and ~85 ms for undo/redo
command round-trips at every size — no growth with clip count.
Long-session: 50 undo/redo cycles on the 100-clip timeline moved the
heap +0.33 MB — no runaway retention observed.

Honest limits: numbers are this machine only; "initial render" is a
post-navigation rAF settle (the timeline was already open), not a
cold first paint; drag *gesture* smoothness is approximated by the
select+scroll timings, not a continuous pointer-move benchmark.

## Installed Windows acceptance — PASS (8/8 steps)

Executed against the NSIS build of the N1-E head (`Atomic Dream
Engine_0.1.0_x64-setup.exe`, 76.13 MiB, installed to
`%LOCALAPPDATA%\Atomic Dream Engine`, backend on an ephemeral port).
Driver: `scripts/cdp-n1e-acceptance.cjs` — real UI input (button
clicks, CDP key events, confirm-dialog click), API truth on every
claim. Evidence: `docs/n1e-evidence/` (screenshots + log).

| # | Step | Result |
|---|------|--------|
| 1 | Seed: real video + 3×48f clips at 0/48/96 via API commits | PASS |
| 2 | Undo button enabled with server label ("Undo: Inserted clip"), Redo disabled | PASS |
| 3 | UI Undo click → 3→2 clips; Redo enabled with label | PASS |
| 4 | UI Redo click → 2→3 clips restored | PASS |
| 5 | Ctrl+Z undid; Ctrl+Shift+Z redid (real key events) | PASS |
| 6 | Ctrl+Z then a divergent edit (Delete + confirm dialog) → Redo disabled — branch invalidated | PASS |
| 7 | Page reload → undo stack intact ("Undo: Removed clip" still offered) | PASS |
| 8 | API detail reports `undo_label` + clip state matching the DOM | PASS |
| 9 | Process terminated cleanly (0 residual processes) | PASS |

Screenshots: `02-undo-enabled.png` … `07-post-reload-undo.png`;
log: `docs/n1e-evidence/n1e-acceptance.log`.

## Viewport culling verification (§5)

- Offscreen clips mount and select after scrolling — measured:
  `dom_clips_scrolled` non-zero, select path exercised on mounted
  clips (DOM mounts stay ~5 at every size).
- Snapping and overlap checks use `clipsByTrack` (the full lane data),
  NOT the culled view — offscreen clip edges still snap and reject
  invalid drops (`TimelineEditor.tsx` drag handler).
- Zoom re-derives the cull window — `contentW` change re-measures the
  visible range; nothing visible is lost.
- Selection is id-keyed (`selClipId`), unaffected by mounts/unmounts.
- Drag listeners live on `window` — a clip scrolling out of view
  mid-drag cannot break the gesture.
- Keyboard handlers live on clip elements; a culled clip can't be
  keyed until scrolled into view (inherent to DOM culling; the clip
  remains selectable, undo/redo still operate on it).
- Culling is a render-time filter — it never writes timeline data;
  frame/px math (`frameToPx`, `ppf`) is unchanged — no coordinate
  drift possible.

Not executed: drag/zoom/selection frame-latency measurement on real
hardware (needs a human-hands session; jsdom culling evidence +
server-side latency bounds above stand in). Multi-user concurrent-
session undo is covered by the command layer's `expected_timeline`
rejection test, not by a second live UI session.
