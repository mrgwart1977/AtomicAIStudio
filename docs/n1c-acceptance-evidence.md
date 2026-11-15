# N1-C installed-app acceptance evidence

**Build under test:** NSIS-installed `atomic-dream-engine.exe` from
`apps/desktop/src-tauri/target/release/bundle/nsis/Atomic Dream Engine_0.1.0_x64-setup.exe`
(installer built on branch `feat/n1c-advanced-editing`; sidecar rebuilt
with the N1-C backend; installed to `%LOCALAPPDATA%\Atomic Dream Engine`,
sidecar observed on ephemeral port 53294).

**Driver:** `scripts/cdp-n1c-acceptance.cjs` — attaches to the installed
app's WebView2 via CDP (`--remote-debugging-port=9223`), learns the
ephemeral sidecar port + per-launch token by sniffing the app's own
`/api` traffic (Network domain), seeds a project/timeline via real API
commands, then exercises the N1-C controls through live DOM pointer,
mouse, and keyboard events. Every claim is verified against the API —
nothing is reported on faith.

**Result: PASS — 10 steps** (`docs/n1c-evidence/n1c-acceptance.log`)

| # | Check | Evidence |
|---|-------|----------|
| 1 | Seed: project + 2 imported video assets + timeline via installed backend | jobs succeeded |
| 2 | N1-C controls render: Split-at-playhead, Ripple toggle, Insert-space, Close-gap, ≥2 trim handles | `02-n1c-controls.png` |
| 3 | Split at playhead → `timeline.clip.split` commits; starts `[0,18,36]`; total source duration conserved (84 src frames); right fragment keeps `source_version_id` + `source_sha256` | `03-after-split.png`, API reads |
| 4 | Non-ripple delete leaves an explicit gap; DOM shows `lane-gap` indicator "Gap — 18 frames — click to close" | `04-gap-indicator.png` |
| 5 | Clicking the gap indicator commits `timeline.gap.close` → lane abuts `[0,18]`, indicator gone | API read |
| 6 | Insert-space 12f at a boundary commits `timeline.gap.insert` → follower `[18→30]`, gap indicator renders | `06-inserted-gap.png` |
| 7 | Close-gap toolbar button restores `[0,18]` | API read |
| 8 | Trim-handle pointer-drag commits `timeline.clip.trim` (`out_frame` 36→31); non-ripple leaves the follower in place; retracted end renders as an honest `lane-gap` | `08-trim-handle.png`, API reads |
| 9 | Ripple toggle + delete via the real ConfirmDialog ("later clips on the same lane shift left") → follower shifts by the deleted footprint (18→5), preserving the 5f gap as a leading gap | API read |
| 10 | `ade-edl/2` export of the final layout is honest (track + gaps + pins) | `final-edl-v2.json` |

**Clean shutdown:** after `taskkill` of the app process, no
`atomic-*`/`atomic-backend.exe` processes remained (verified via
`tasklist`).

**Not covered by this driver (unit-tested instead):**
`S`-key split (same command path as the button — covered by
`TimelineEditor.test.tsx`), stale-approval 422 refresh UX (page test +
backend tests), `ripple_scope:"all"` locked-lane refusal (backend
tests), mixed-frame-rate split refusal (backend tests), idempotent
replay + revert (backend tests), cross-project isolation (backend
tests). Human-required items (visual polish review, pen/touch input)
remain manual.
