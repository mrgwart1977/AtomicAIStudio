# N1-B installed-app acceptance evidence

**Build under test:** NSIS-installed `atomic-dream-engine.exe` from
`apps/desktop/src-tauri/target/release/bundle/nsis/Atomic Dream Engine_0.1.0_x64-setup.exe`
(installer built from PR #122 head `4794bde`; installed to
`%LOCALAPPDATA%\Atomic Dream Engine`). The frozen sidecar ran the
`0012_c5a → 0013_n1a` migration on the real installed data dir at first
launch — genuine upgrade path.

**Driver:** `scripts/cdp-n1b-acceptance.cjs` — launches nothing itself;
attaches to the installed app's WebView2 via CDP
(`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9223`),
discovers the ephemeral sidecar port + per-launch token by sniffing the
app's own `/api` traffic (Network domain), seeds a project/timeline via
real API commands, then exercises the editor through live DOM pointer,
mouse, and keyboard events. Every claim is verified against the API or
the DOM — nothing is reported on faith.

**Result: PASS — 12 steps, 54.9 s** (`n1b-evidence/evidence.json|log`)

| # | Check | Evidence |
|---|-------|----------|
| 1 | Seed: project + 2 imported video assets + timeline via installed backend | jobs succeeded; `kind:"video"` with probe media |
| 2 | Editor opens, renders V1 lane + 2 clips at their `start_frames` | `02-editor-initial.png` |
| 3 | Add overlay lane; reorder it above V1 (live lane list asserted) | `03-lanes-added-reordered.png` |
| 4 | Clip click → inspector with trim/move/remove controls | `04-inspector.png` |
| 5 | Pointer-drag clip: commits integer `start_frames` (+13f for +78px @6ppf), verified via API | `05-after-drag.png` |
| 6 | Drop onto a neighbor's start edge refused (overlap rule) | `06-snap.png` |
| 7 | Overlapping drag → `drop-error` banner "would overlap a clip on this lane"; API state unchanged | banner text in `evidence.log` |
| 8 | ArrowRight nudge commits exactly +1 frame; **locked V1 refuses the nudge** (backend-enforced — API state unchanged) | `08-keyboard-lock.png` |
| 9 | Zoom-in changes clip screen-x; `scrollLeft=200` applied to the scroller | `09-zoom-scroll.png` |
| 10 | Inspector "Move to frame" normalizes to [0,36] → badge "Exports as one video"; drag clip onto overlay lane → badge flips to "Multitrack — export needs N1-D" | `10-export-badge-multitrack.png` |
| 11 | WebView reload → committed state re-rendered | `11-persisted.png` |
| 12 | Full process restart → API + UI show the same multitrack layout (clip on overlay @36, clip on V1 @0); `taskkill` of the main exe exits the sidecar cleanly — zero orphan `atomic-backend.exe`/`ffmpeg` processes | `12-restart-persisted.png` |

## Frame-rate fix verified in this build

`4794bde` replaces the `fps_num ≤ 240` numerator cap with a rate ceiling:
`den ≤ 1001`, `num ≤ 480 000` (schema-compatible), rate ≤ 480 fps.
Exact NTSC timeline rates `24000/1001` and `30000/1001` are now accepted;
integer and legal rational rates unchanged; `999/1` still refused.
Backend suite: **63 passed**.

## Not executed (honest gaps)

- Human visual review of the screenshots (pixel-level look) — automated
  DOM/API assertions only.
- Ctrl+wheel zoom and Esc-drag-cancel are covered by unit tests
  (`TimelineEditor.test.tsx`) but not by this installed run.
- Audio-lane drops: no audio sources exist to drag (refusal is
  unit-tested).
- Hardware/model-gated checklist items remain open as before.
