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
| Low | `AssetsPage` shared-library **✕ close** and `ScreenplayPage` **+ create** icon buttons had no name | 4.1.2 | `aria-label` added |

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
- Job-list per-row `j.error` spans are persistent list details, not
  live announcements — intentionally not given `role="alert"` (noise).

## Remaining findings (documented, not fixed)

- **Timeline canvas interactions are pointer-first** (scrub ruler, clip
  drag, edge auto-scroll). Full keyboard operability for clip moves
  would need a dedicated interaction model — tracked for a future
  milestone, not silently claimed.
- Contrast not machine-measured this pass; the dark theme uses
  `text-muted`/`text-faint` tiers — a contrast audit is in the human
  checklist (C4.5) rather than asserted.
- Focus order inside the workspace left-nav → main → inspector is DOM
  order; the timeline's virtualized lists may reorder focus —
  human check, not assumed.

## Regression tests

`src/a11y.test.tsx` (4 tests): accessible names on the fixed controls,
`role="alert"` on the PromptBar error path, and a guard that the rename
input stays name-addressable. `npm run build` + vitest green.
