# C4.4 — Accessibility audit and fixes

Scope: keyboard navigation, focus order, screen-reader naming, status
announcements, contrast, error surfacing — verified against the React
source (`apps/desktop/src`, 82 files), not guessed.

## Verified defects fixed this pass

| Severity | Defect | WCAG | Fix |
|----------|--------|------|-----|
| High | `RenameDialog` name input had **no accessible name** (bare `<input>`, autoFocus) — SR announced only "edit" | 4.1.2 | `aria-label={title}` |
| High | `ProvidersPage` provider-search relied on **placeholder only** | 4.1.2 | `aria-label="Search providers"` |
| High | `ProvidersPage` four per-task **default-provider selects** had sibling text but no programmatic association | 4.1.2 | `aria-label="{task} default provider"` |
| High | `ProvidersPage` assisted-workflow **prompt textarea** relied on placeholder only | 4.1.2 | `aria-label` bound to provider name |
| High | **Page-level error blocks appeared silently** — `PromptBar`, `NewProjectDialog`, `ProjectsPage`, `AssetsPage`, `AssistedPanel`, `BackupsPage`, `CloudImagePanel`, `CloudPage`, `EntityPage`, `LiveGatePanel` rendered `{error}` into a plain `<p>` — screen readers never announce them | 4.1.3 | `role="alert"` on every page-level error block |
| High | **`LaneClip` could not be selected by keyboard** — `role="button"` + `tabIndex=0` but `onClick` never fires on Enter/Space for a `<div>`, so the clip inspector (trim/move/fade/gain controls) was unreachable keyboard-only | 2.1.1 | `clipKey` handles Enter/Space → `setSelClipId` (works on locked lanes too) |
| High | **Primary buttons failed contrast** — `bg-action` (#2f8f76) + `text-white` = **3.95:1**; hover `bg-action-hi` + white = **2.95:1** (33 sites incl. shared `steps.tsx btn`) | 1.4.3 | `text-white` → `text-ink` on `bg-action`/`bg-action-hi` (4.77 / 6.41) |
| High | **`bg-teal` + `text-white`** = ~2:1 (wizard generate button, storyboard badge) | 1.4.3 | `text-ink` (10.08:1) |
| Medium | **`--color-faint` #6b7a94 failed AA everywhere** — 3.77 on card, 4.09 panel, 4.35 ink (timestamps, hints, lane-empty text) | 1.4.3 | token lightened to **#7e899b** → 4.63 / 5.02 / 5.34 |
| Low | `AssetsPage` shared-library **✕ close** and `ScreenplayPage` **+ create** icon buttons had no name | 4.1.2 | `aria-label` added |
| Low | Clip `aria-label` exposed a raw `sha256` prefix | 4.1.2 | label now `${kind} clip on ${trackName} — starts {timecode}, {n} frames` |

## Announcement model — verified, not assumed

- All 23 `role="alert"` sites mount conditionally (`{error && <p …>}`):
  the node is inserted *with* content, so it announces once on
  insertion and re-announces on text change. No toast/notification
  layer duplicates them — no double announcements.
- Persistent per-row history errors (`job.error`, `b.error`) are
  intentionally **not** alerts — they're static list detail.
- Alerts never move focus; dialogs (Radix) keep focus trapped inside —
  post-error focus is predictable.

## Timeline keyboard model — what exists vs what was added

Already keyboard-operable before this pass:
- Ruler: `role="slider"` + arrows/Home/End/PgUp/PgDn scrub.
- Focused clip: ←/→ nudge 1 frame, Shift = ±1 s, Ctrl+↑/↓ re-lane,
  Delete/Backspace remove, `s` split at playhead — all frame-exact.
- Ctrl+Z / Ctrl+Shift+Z undo/redo; gap-close, zoom, preview, history
  are native buttons; track header controls are named buttons.
- Inspector: labeled numeric Trim in/out + Start frame inputs +
  Apply/Move/Remove buttons; GainSlider is a native `input
  type=range` (arrows + commit on keyup); fade length numeric inputs,
  curve selects, crossfade and automation inputs all carry
  `aria-label`s.

Added this pass:
- **Enter/Space selects a focused clip → opens the inspector** — the
  missing link that made every inspector control reachable by
  keyboard. `TimelineEditor.test.tsx` regression: Enter and Space
  both select + show `clip-inspector`.
- Meaningful clip `aria-label` (kind, lane name, timecode, duration)
  replacing the sha prefix; `aria-pressed` already reflects selection.

Pointer-only actions that remain (all have keyboard-equivalent paths):
trim/fade *handles* drag — covered by inspector numeric inputs;
clip *drag* — covered by arrow nudges + inspector Start-frame field.

## Contrast audit — measured (src/contrast.test.ts, repeatable)

WCAG 1.4.3 AA text ≥4.5:1 / 1.4.11 non-text ≥3:1. Results after fixes:

| Pair | Ratio | Verdict |
|------|-------|---------|
| fg on ink/panel/card/cardHi | 16.96 / 15.94 / 14.72 / 13.9 | PASS |
| muted on ink/panel/card | 9.72 / 9.14 / 8.44 | PASS |
| faint (new #7e899b) on ink/panel/card | 5.34 / 5.02 / 4.63 | PASS (was FAIL) |
| teal on ink/card (links, sel clip text) | 10.08 / 8.75 | PASS |
| ink on action/action-hi (primary btns) | 4.77 / 6.41 | PASS (was FAIL as white) |
| ink on teal | 10.08 | PASS (was FAIL as white) |
| danger on card/panel (errors) | 5.18 / 5.61 | PASS |
| amber on card / red-300 hover / violet sel cue | 8.67 / 8.63 / 4.92 | PASS |
| focus ring teal on ink/card; sel ring teal on card | 10.08 / 8.75 | PASS (non-text) |
| disabled states (opacity-40/50) | 1.8–3.6 | **exempt** — WCAG 1.4.3 inactive-component clause; recorded, not asserted |
| `--color-line` hairlines on card | 1.38 | **advisory** — decorative separators; inputs get `focus:border-action` + ring, so no control relies on the hairline alone. To reach 3:1 would need ~#636b76 — deferred as a design decision |

`bg-accent` on `AssistedPanel`/`LiveGatePanel` buttons is an undefined
token (renders transparent over dark panel) — `text-white` there is
correct against the effective background; flagged as a latent styling
bug, not a contrast failure.

## Verified already-correct (not defects)

- Radix `Dialog` everywhere — focus trap, Esc, labelled titles.
- All `<img>` carry `alt` (logo, thumbnails); `AuthImage` requires `alt`.
- `focus:outline-none` is always paired with `focus:border-action` — a
  visible focus indicator remains (border color shift).
- Wizard (`steps.tsx`) and `NewProjectDialog`/`ProjectsPage`/`ConfirmDialog`
  already label virtually every control (`aria-label` or wrapping
  `<label>`); the `✕` remove buttons audited there already had
  `aria-label` on an earlier attribute line.
- Status/notice regions already use `role="status"` (wizard, assembly,
  dialogue) — only the *error* variant was missing `role="alert"`.
- Track header buttons (rename, hide, lock, mute, solo, reorder) all
  carry `${verb} ${track.name}` aria-labels.

## WCAG criteria actually tested vs assumed

| Criterion | Status | Evidence |
|-----------|--------|----------|
| 1.4.3 Contrast (text) | **Tested** | `contrast.test.ts` — 21 measured pairs, all PASS post-fix |
| 1.4.11 Non-text contrast | **Tested** | focus/selection rings measured ≥8.75 |
| 2.1.1 Keyboard | **Partially tested** | clip select/move/delete/split, ruler, undo, inspector automated; full end-to-end keyboard-only task completion needs human verification (U-series) |
| 4.1.2 Name/role/value | **Tested** | `a11y.test.tsx` + audit; all interactive controls named |
| 4.1.3 Status messages | **Tested** | `role="alert"` verified; announcement *behavior* is DOM-verified but real SR output needs human listening (L-series) |
| 1.3.1 / 3.3.x field-error association | **N/A mostly** | errors are operation-level, not field validation; no orphaned field errors found |

## Regression tests

- `src/contrast.test.ts` — 21 measured pairs enforced (was: no audit).
- `src/a11y.test.tsx` — 4 tests: names on fixed controls, `role="alert"`.
- `TimelineEditor.test.tsx` — Enter/Space clip-selection regression.

## Honest limits

- Screen-reader announcement quality (verbosity, ordering, NVDA/JAWS
  quirks) is NOT asserted — DOM-level roles verified; real speech
  output requires human listening checks (L-series, NOT RUN).
- Focus order inside virtualized lanes and post-command focus
  restoration are DOM-order-verified only — human keyboard task in
  the U-series.
