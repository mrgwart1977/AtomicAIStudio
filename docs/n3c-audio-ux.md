# N3-C — Audio editing UX

Interaction-layer milestone on top of N3-A automation + N3-B
scrubbing. All edits still go through the same command system
(`timeline.track.automation`, `timeline.clip.automation`,
`timeline.crossfade.update`) — no parallel model, no new
persistence.

## C1 — Automation editing

Shared `AutomationEditor` (track lanes + clip inspector):

- SVG automation curve (`aria-label="Automation curve — N
  keyframes"`) — linear paths, step paths rendered as horizontal
  steps; points visible as dots, selected point emphasized.
- Per-keyframe rows: numeric **frame position** and **gain dB**
  inputs — Enter/blur commits through the existing automation
  commands; invalid input is rejected in-UI (`role="alert"`) before
  any command fires:
  - duplicate position → "a keyframe is already there"
  - out-of-range position → "range 0–{max}" (clip = occupancy−1,
    track = program bound)
  - out-of-range gain → "−60–12 dB"
- Rows are `role="group"` + `tabIndex={0}` — keyboard focusable;
  **Delete/Backspace on the focused row deletes that keyframe**
  (focus counts as selection — no prior pointer click required).
- Interpolation `select` per editor (`aria-label="Track automation
  interpolation"` / `"Automation interpolation"`), commits `linear`
  ↔ `step` through the same command.
- Undo/redo verified through the existing revision system — no
  separate automation history.

## C2 — Crossfade editing

`CrossfadeSection` in the clip inspector + lane overlay:

- **Visual region**: `[data-testid="xf-region"]` band rendered on
  the lane spanning `tl_start .. tl_start+tl_frames` — region is
  visible without opening the inspector.
- Participating clips shown by name (out → in), selected `curve`
  displayed and editable (`equal_power`, `linear`, `s_curve`,
  `exponential`).
- Numeric **start** and **duration** inputs (whole frames,
  Enter/blur commit) — reuses `timeline.crossfade.update`.
- Source-handle validation preserved: insufficient handles surface
  the backend error verbatim in-UI and leave the crossfade
  unchanged; detached crossfades stay fail-closed.
- No silent source-handle or clip-timing modification — only the
  crossfade record's `tl_start`/`tl_frames`/`curve` change.

## C3 — Timeline navigation

- **Edge auto-scroll** (`edgeScroll`, 48 px threshold, 32 px
  bounded step): wired into ruler scrub moves *and* clip
  drag/trim handlers — the scroller pans while the pointer is held
  near a horizontal edge, clamped to
  `[0, scrollWidth − clientWidth]` (no runaway).
- **Anchored zoom**: zoom keeps the playhead's screen position
  stable (falls back to viewport center when the playhead is
  off-screen) by re-computing `scrollLeft` after `ppf` changes.
- **Keyboard**: PageDown/PageUp pan one visible span; existing
  arrows/Home/End retained.
- **Sticky ruler**: the ruler row is genuinely `position: sticky;
  top: 0` inside the scroll container (the previous
  `position:"relative"` inline style silently defeated the `sticky`
  class) — the ruler stays visible while lanes scroll vertically.
- The sticky track-header cell intentionally overlays the ruler
  row's left edge — the interactive ruler span begins at the
  header's right edge (acceptance driver probes `visLeft`, not the
  element's layout left). Its width is responsive —
  `min(176px, max(96px, 40cqw))` against the scroll container — so
  it shrinks instead of covering the entire ruler on very narrow
  windows.
- **Inspector dock**: the clip inspector sits in a wrap row beside
  the timeline column (`min-width: min(440px, 100%)` on the
  timeline side), capped at `calc(100vh − 200px)` with its own
  `overflow-y: auto` + `overscroll-contain`. At widths where it
  fits, focusing inspector inputs cannot scroll the page column —
  the ruler is never displaced by numeric edits. Below the
  breakpoint it wraps underneath (normal document scroll).

## C4 — Beginner-friendly controls

- Fade controls labeled in seconds with curve descriptions
  ("Fade in (seconds)", equal-power/s-curve explained in tooltips);
  raw sample counts no longer the primary label — exact source
  units remain in tooltips where useful.
- Mute/solo/gain unchanged (already labeled); automation section
  headers describe what the curve does.

## C5 — Accessibility

- Ruler: `role="slider"`, `aria-valuemin/max/now` in frames and
  `aria-valuetext="Playhead at M:SS (frame N)"`.
- All 128 interactive controls in the editor have accessible names;
  none use `tabindex < 0`; keyframe rows are keyboard-focusable.
- Inline validation errors use `role="alert"`.
- Verified across the scaling matrix — 1100 px@1.0×, 900 px@1.25×,
  760 px@1.5×, 700 px@2.0×: ruler probes resolve to ruler elements,
  audit controls still reachable.

## C6 — Performance (measured on the installed build)

- **Virtualization**: 120-clip WIDE timeline → **5 mounted clip
  DOM nodes** mid-lane — viewport culling intact.
- **Dense automation**: 200-point lane ramp renders + commits fine.
- **Zoom settle**: click → two rAFs ≈ 12–18 ms.
- **Engine load** (120 clips, 4 unique assets): ~1.2–1.4 s;
  preview plan build ~0.1–0.3 ms; 350 ms offline window render
  ~6–10 ms.
- **Memory**: app RSS ~27 MB, backend ~103–144 MB during perf
  section.

## C7 — Installed acceptance

`scripts/cdp-n3c-acceptance.cjs` — **11/11 steps** on a real NSIS
install (`docs/n3c-evidence/n3c-acceptance.txt`):

1. scene — embedded V1 + 3 audio lanes, satisfied xf, fades,
   clip+lane automation, 3-frame clip, gap.
2. export FILM → PCM parity: pre-duck, −9 dB step duck, xf
   out-fade, xf in-leg full gain, music lane duck, embedded ramp,
   tiny clip audible, silence past program end.
3. engine load on the real scene.
4. Activity rail collapse/expand/narrow/1.5× — ruler unobstructed,
   audit controls independently reachable.
5. track automation: numeric entry, dup/range/gain validation,
   keyboard Delete, undo/redo, interpolation, **no-remount + focus
   survives commit**, **stale-revision refusal (409)**,
   **cross-project refusal (404)**.
6. clip-local bounds (0–89), xf region band, duration edit
   24f→40f, xf single-undo step, insufficient-handle rejection,
   curve edit + restore, **xf preview parity** (40f render energy
   vs 24f), **inspector focus → zero page scroll**.
7. edge auto-scroll (0→192 scrollLeft during held drag), anchored
   zoom (playhead x stable ±8 px), PageDown/PageUp pan, preview
   transport play→playing→stop.
8. a11y names/focus/slider semantics + scaling matrix probes
   (1100@1×, 900@1.25×, 760@1.5×, 700@2×).
9. perf numbers above.
10. regression scrub drag, windowed parity spot-check, clean close
    — no orphan processes.
11. **restart persistence** — app relaunch re-opens FILM with all
    clips/xf/automation at the committed revision; engine reloads;
    clean close again.

### Latency note

Scrub scheduling latency is unchanged from N3-B (the engine is
untouched); this milestone's interactions are command commits whose
cost is the normal revision round-trip, not audio scheduling.

### Known limits / human checks

- `TimelinePage` defect found + fixed here: `useCallback`
  dependencies on the whole `project` object remounted the entire
  editor on every command commit (wiping selection, automation
  section, playhead). Callbacks now key on `project?.id`.
- Focus-safe commit inputs: while a command commit is in flight
  (`busy`), the currently focused editor control stays enabled so
  the browser does not blur it mid-commit — focus and selection
  survive successful commits (verified installed).
- The inspector docks beside the timeline where space allows;
  below ~440 px of timeline width it wraps underneath — focusing
  its inputs there uses normal document scroll (ruler can leave
  the viewport — scroll back). Not a defect; noted for testers.
- Fresh-install note: the "pre-existing N2 project reopen" check
  self-skips when the installed DB has no prior project.
- Audible quality / editing feel remain **[human]** checks — PCM
  parity is machine-verified, feel is not.
