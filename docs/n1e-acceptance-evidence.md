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
jsdom evidence: 300 clips → 300 nodes before scroll, windowed subset
after. Frame-accurate edits are unaffected — culling only decides what
mounts, never edit math. Drag/zoom/selection responsiveness on real
hardware is recorded as *not directly measured* (needs human-hands
session); server-side latency above bounds the command path.

## Installed Windows acceptance — pending

`scripts/cdp-n1e-acceptance.cjs` drives the installed app: seeds 3
clips, clicks Undo/Redo buttons, sends Ctrl+Z / Ctrl+Shift+Z via CDP,
forces a divergent edit to prove redo invalidation, reloads to prove
stack persistence, and reads API truth for every DOM claim. Executed
against the NSIS build of the N1-E PR head — results appended below.
