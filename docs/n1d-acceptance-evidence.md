# N1-D installed-app acceptance evidence

**Build under test:** NSIS-installed `atomic-dream-engine.exe` from
`apps/desktop/src-tauri/target/release/bundle/nsis/Atomic Dream Engine_0.1.0_x64-setup.exe`
(built on branch `feat/n1d-composite-export` @ `71fe188`; sidecar
rebuilt with the N1-D backend; installed to
`%LOCALAPPDATA%\Atomic Dream Engine`, sidecar on ephemeral port 64400,
data dir `%LOCALAPPDATA%\AtomicAIStudio`).

**Driver:** `scripts/cdp-n1d-acceptance.cjs` — attaches to the installed
app's WebView2 via CDP, learns the ephemeral port + per-launch token by
sniffing the app's own `/api` traffic, seeds a project/timeline through
real API commands with REAL synthesized media (libopenh264 + aac via the
bundled ffmpeg), then executes the composite export **through the
installed UI** — export panel, 720p profile select, EDL-pinned preview,
Start export — and verifies the rendered output with the bundled
ffprobe. Nothing is reported on faith.

**Result: PASS — 6 steps** (`docs/n1d-evidence/n1d-acceptance.log`)

| # | Check | Evidence |
|---|-------|----------|
| 1 | Seed: 4 real synthesized videos imported; 4-lane layout — base@0 V1 (pos 0), over@24 O1 (pos 1, composited on top per the documented position model), ghost on hidden lane, audio-bearing clip on muted lane | `media/`, API reads |
| 2 | Composite badge: "Exports 3 lanes — composite" (hidden lane honestly excluded from the count) | `02-composite-badge.png` |
| 3 | Export through the installed UI: profile select → preview shows "Export 4 clip(s) as 720p · EDL pinned b055a8bcd5…" → Start export → "Export queued" | `03-export-preview.png`, `03-export-started.png` |
| 4 | Job `aa1b1cb7` succeeded; output asset probed: **h264, 1280×720, 48 frames exactly, 2.000s, aac audio** — program end = max(48 base, 24+24 over, hidden/muted clipped extents) | `output-probe.json` |
| 5 | Full WebView reload → 4 clips in DOM + rendered asset still registered | `05-post-reload.png` |
| 6 | Clean shutdown: no `atomic-*` processes remain | `tasklist` |

**Coverage notes:** the composite render itself (deterministic graph,
frame-exact windows, sample-exact audio, cancel/cleanup paths) is
verified by `backend/tests/test_n1d_composite.py` (26 tests incl. real-
ffmpeg e2e). Human-required items (visual frame-content review of the
composite stack, pen/touch) remain manual — not claimed.

**Not covered by this driver:** explicit UI cancellation of a running
export (adapter-level cancel is directly tested), multiple sequential
exports in one session (job engine path, shared with C5-B).
