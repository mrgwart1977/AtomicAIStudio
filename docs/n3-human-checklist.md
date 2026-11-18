# N3 — Human listening & editing-feel checklist

Manual acceptance for the audio editing milestone. These are **human-required**
checks — they are deliberately kept separate from automated acceptance and are
**not** marked passed without a person running them.

> **A8 note (count reconciliation):** the A8 specification asked for "the
> existing 24 human audio/editing checks." This file is the only human
> audio/editing checklist in the repo and contains **19** enumerated
> checks (A1–A8, B1–B6, C1–C5); `docs/n3-completion-report.md` refers to
> it loosely as a "24-check" matrix. A8 carries the real 19 checks
> forward verbatim in `docs/a8-acceptance.md` — no checks were invented
> to reach 24, and none are marked passed without a human run.

Setup: build + install the app (`scripts\build-sidecar.ps1`, then
`npm run tauri build`, run the NSIS installer). Any project works; for
comparable results the N3-D driver (`scripts/cdp-n3d-acceptance.cjs`) leaves a
`FILM` timeline (30000/1001, 2 video lanes, dialogue+xf, music, sfx) and a
`LONG` timeline (240 clips, 6 audio lanes) in the newest `N3D-*` project.
Headphones or speakers required.

## A. Continuous scrubbing feel

| # | Check | How | Pass if |
|---|-------|-----|---------|
| A1 | Scrub onset feels immediate | Drag the ruler slowly left→right | Audible audio starts within a beat of the pointer moving; no dead zone |
| A2 | Scrub is continuous, not stuttery | Drag steadily across a music lane | Music "scrubs" like tape — pitch/time smear is acceptable, gaps/stutter are not |
| A3 | Scrub stops cleanly on release | Drag, release mid-lane | Audio stops at release position; playhead stays where released |
| A4 | Cancel restores | Drag, press **Esc** before releasing | Playhead + audio return to the pre-scrub position |
| A5 | Scrub over silence/gap is silent | Drag across a gap between clips | No residual tail, clicks, or zombie audio in the gap |
| A6 | Scrub respects mute/solo | Solo a lane, scrub | Only the soloed lane is heard |
| A7 | Scrub reflects automation | Scrub across an automated duck region | Level audibly dips where the curve dips |
| A8 | Rapid zig-zag | Shake the pointer fast across lanes | No runaway queue, no lingering chorus of stale auditions |

## B. Preview parity sanity

| # | Check | How | Pass if |
|---|-------|-----|---------|
| B1 | Preview matches export intent | Play the preview over the xf region, then open the exported mp4 at the same timecode | Same musical content and relative levels — not sample-locked, but no missing/duplicated clips |
| B2 | Play/pause/stop feel | Space / transport buttons | Responsive, position indicator correct |

## C. Editing feel

| # | Check | How | Pass if |
|---|-------|-----|---------|
| C1 | Clip drag has edge auto-scroll | Drag a clip toward the lane edge | View pans while held near the edge — no dropped clip |
| C2 | Anchored zoom | Zoom in/out with the playhead on screen | Playhead stays under its screen position |
| C3 | Ruler stays visible | Scroll lanes vertically | Ruler row remains pinned |
| C4 | Numeric edits don't displace the timeline | Open a clip inspector, focus position/gain inputs, press Enter | The timeline doesn't jump; focus stays in the field |
| C5 | Undo is predictable | Make 3 edits (trim, gain, fade), Ctrl+Z ×3 | Each undo reverses exactly one edit |
| C6 | Keyframe delete | Focus an automation row, press Delete | That keyframe only is removed |

## D. Long-program responsiveness (LONG timeline)

| # | Check | How | Pass if |
|---|-------|-----|---------|
| D1 | App stays responsive | Open LONG, scroll/zoom | No multi-second freezes; UI stays interactive |
| D2 | Waveforms render | Look at lanes | Waveform shapes appear for imported audio |
| D3 | Scrub still feels live | Scrub the long program | Same feel as the short film — no degradation |

## Recording results

Fill in one row per check: `PASS / FAIL / NOTES`. Do **not** paste this table
into automated evidence; file the completed copy next to
`docs/n3d-evidence/` (e.g. `n3d-human-acceptance.md`) with date, tester,
app version, and audio device.
