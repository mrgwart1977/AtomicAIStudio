# N1-C installed-app acceptance evidence

**Build under test:** NSIS-installed `atomic-dream-engine.exe` from
`apps/desktop/src-tauri/target/release/bundle/nsis/Atomic Dream Engine_0.1.0_x64-setup.exe`
(installer built on branch `feat/n1c-advanced-editing`; sidecar rebuilt
with the N1-C backend; installed to `%LOCALAPPDATA%\Atomic Dream Engine`,
sidecar observed on ephemeral ports 53294/51981 across runs).

**Driver:** `scripts/cdp-n1c-acceptance.cjs` — attaches to the installed
app's WebView2 via CDP (`--remote-debugging-port=9223`), learns the
ephemeral sidecar port + per-launch token by sniffing the app's own
`/api` traffic (Network domain), seeds a project/timeline via real API
commands, then exercises the N1-C controls through live DOM pointer,
mouse, and keyboard events. Every claim is verified against the API —
nothing is reported on faith.

**Result: PASS — 14 steps** (`docs/n1c-evidence/n1c-acceptance.log`)

| # | Check | Evidence |
|---|-------|----------|
| 1 | Seed: project + imported video assets + timeline via installed backend | jobs succeeded |
| 2 | N1-C controls render: Split-at-playhead, Ripple toggle, Insert-space, Close-gap, trim handles | `02-n1c-controls.png` |
| 3 | Split at playhead → `timeline.clip.split` commits; total source duration conserved; right fragment keeps `source_version_id` + `source_sha256` | `03-after-split.png`, API reads |
| 4 | Non-ripple delete leaves an explicit gap; DOM shows `lane-gap` indicator with exact frame count | `04-gap-indicator.png` |
| 5 | Clicking the gap indicator commits `timeline.gap.close` → lane abuts, indicator gone | API read |
| 6 | Insert-space commits `timeline.gap.insert` → followers shift +12, gap indicator renders | `06-inserted-gap.png` |
| 7 | Close-gap toolbar button restores contiguity | API read |
| 8 | Trim-handle drag commits `timeline.clip.trim`; non-ripple leaves followers in place; retracted end renders as an honest `lane-gap` | `08-trim-handle.png`, API reads |
| 9 | Ripple toggle + delete via the real ConfirmDialog → followers shift by the deleted footprint (gap preserved) | API read |
| 10 | Locked-track refusal: V1 locked → trim handles gone, split/insert-space/gap buttons all disabled | live DOM |
| 11 | Escape cancels a mid-flight trim drag → `out_frame` unchanged via API | API read |
| 12 | Export eligibility: leading-gap layout shows "Multitrack — export needs N1-D"; closing it flips to "Exports as one video" | badge text |
| 13 | Restart persistence: full WebView reload → DOM renders the committed arrangement (clip count + start frame match API) | post-reload DOM + API |
| 14 | `ade-edl/2` export of the final layout is honest (tracks + gaps + pins) | `final-edl-v2.json` |

**Clean shutdown:** after app termination, no `atomic-*` processes
remain (verified via `tasklist`).

**Not covered by this driver (unit/backend-tested instead):**
ripple `clip.insert`/`clip.place` — the editor has no "place clip at
playhead" affordance (clips arrive via Storyboard send-to-timeline);
the command path is covered by `test_ripple_insert_pushes_followers`
and `test_ripple_insert_mid_clip_refuses`. `S`-key split (same command
path as the button — `TimelineEditor.test.tsx`), stale-approval 422
refresh UX (page test + backend), `ripple_scope:"all"` (backend-only;
UI always sends `"track"`), mixed-frame-rate split refusal +
24000/1001 boundary (backend tests), idempotent replay + revert +
cross-project isolation (backend tests). Human-required items (visual
polish review, pen/touch input) remain manual.
