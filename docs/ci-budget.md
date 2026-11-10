# CI Budget Policy

Private-repo GitHub Actions minutes are billed per job wall-clock time,
multiplied by OS: **Ubuntu 1x, Windows 2x, macOS 10x**.

## Monthly budget

| Item | Minutes |
|---|---|
| Allowance (after 3,000-min upgrade) | 3,000 |
| Planned-validation ceiling | **2,400** |
| Reserve (releases, reruns, incidents) | 600 |

If projected usage for the cycle exceeds 2,400 billed-minutes, treat it
as a budget breach: switch pre-merge validation to `scripts\validate.ps1`
and reserve CI for merge-candidate PRs until the cycle resets.

## Measured November baseline

From `gh api .../actions/runs` job durations x OS multiplier
(67 runs / 300 jobs):

| Segment | Runs | Billed-min |
|---|---|---|
| pull_request — code branches | 40 | ~1,580 |
| pull_request — docs-only (docs/, chore/) | 6 | ~66 |
| push (main) | 19 | ~719 |
| dynamic | 2 | ~2 |
| **Total** | **67** | **~2,367** |

Measured averages: successful PR run ≈ **37.5 billed**, successful
main push ≈ **39.5 billed**. (PR jobs are billed individually even
though they run in parallel.)

## Per-PR projection (measured durations x new gating)

| PR type | Old | New | Savings |
|---|---|---|---|
| Backend-only (`backend/**`) | ~37.5 | ~22.5 | ~40% |
| UI-only (`apps/desktop/src/**` etc.) | ~37.5 | ~2 | ~95% |
| `src-tauri` only | ~37.5 | ~6 | ~84% |
| Backend + UI mixed | ~37.5 | ~24–28 | ~30% |
| Packaging-sensitive | ~37.5 | ~46 | ~0% (runs all) |
| Docs-only | ~37.5 | ~0.5 | ~99% |

The ~50% per-PR target is met for every class except **backend-heavy
PRs (~40%)**. The floor there is the Windows suite itself: two pytest
legs ≈ 22 billed-minutes that requirement explicitly mandates keeping
(3.12 + 3.14, Windows-only tests). Reaching 50% on backend PRs would
require dropping a Python leg or moving the suite off Windows — both
weaken mandated coverage, so they are excluded by design.

## Monthly projection at November activity (measured distribution)

| Segment | Runs | New billed |
|---|---|---|
| ~25 backend-only PRs | | ~560 |
| ~9 mixed code PRs | | ~240 |
| ~6 UI/tauri-only PRs | | ~24 |
| ~6 docs-only PRs | | ~3 |
| 19 main pushes (all jobs, unchanged) | | ~760 |
| **Projected total** | | **~1,600** |

**~1,600–1,800 billed-min vs 2,367 measured → ~25–32% monthly
reduction**, comfortably under the 2,400 ceiling with the 600 reserve.
The per-PR structural savings matter more over time: waste classes
(superseded runs, hangs, docs-only full runs) are eliminated rather
than amortized.

## Guardrails (implemented in ci.yml)

1. `concurrency` per ref + `cancel-in-progress` — force-push cancels
   the superseded run.
2. `timeout-minutes` on every job (5/10/15/20/30) — caps hangs at ~2x
   normal instead of the observed 45-min stall (~90 billed).
3. `changes` job gates PR jobs by touched paths; detection uncertainty
   (unresolvable base, diff error, empty diff) outputs all-true and runs
   the full suite — it can only overspend, never under-validate.
4. `scripts/` is CI-sensitive: any change under it triggers backend +
   frontend + tauri (~26 billed); `package` additionally requires the
   packaging/validation subset (`validate.ps1`, `build-sidecar.ps1`,
   `fetch-ffmpeg.ps1`). Validation infrastructure changes can never
   slip through all-skipped.
5. Dependent jobs use
   `always() && !cancelled() && (... != 'false')`:
   - `changes` succeeded + output=false → skip (neutral = pass)
   - `changes` failed/cancelled → outputs unset → `"" != 'false'` →
     the job RUNS — a dead gate can never turn required checks green
   - workflow cancelled (concurrency/manual) → `!cancelled()` skips —
     dedup still works
6. `package` only on main pushes, `workflow_dispatch`, and
   packaging-sensitive PRs.
7. `frontend` on ubuntu (1x); all Windows-specific jobs stay Windows.
8. Caches: pip, npm, cargo target+registry+git, FFmpeg vendor keyed on
   `backend/ffmpeg-manifest.json`.

Static coverage: `scripts\test-ci-paths.ps1` asserts the classifier's
real patterns against 18 cases (backend/frontend/tauri/package/CI-
workflow/validation-script/docs-only/rename/mixed). Run it after any
edit to the `out` patterns.

## Required-check behavior

Job names unchanged (`backend (3.12)`, `backend (3.14)`, `frontend`,
`tauri-check`, `package`) → required-check identities preserved.
Skipped = neutral = success for required checks. Never add
`paths-ignore` at workflow level — a filtered workflow reports *no*
checks and required checks stall as "expected".

## Automation vs. manual controls

| Control | Mechanism | Type |
|---|---|---|
| Hard overage stop | Billing → spending limit **$0** → jobs refuse at scheduling once included minutes are gone (exactly the November symptom) | Native, automated |
| Approaching-limit warning | Billing → "Budgets and alerts" → Actions budget with alerts at 2,100 / 2,400 / 2,700 | Native, automated (email) |
| Superseded-run waste | `concurrency` cancel | In-workflow, automated |
| Hang waste | `timeout-minutes` | In-workflow, automated |
| Path gating / package skip | `changes` job | In-workflow, automated |
| 2,400 soft ceiling | GitHub has no soft limit — monitor and throttle manually | Manual |
| Per-run billed audit | `gh api .../actions/runs` durations x OS multiplier (or a small script) | Manual |
| Monthly usage check | github.com/settings/billing → Actions & Packages | Manual (weekly) |

Recommended posture: **$0 spending limit** (can never receive a bill —
worst case is CI pauses and local validation carries merges) + budget
alerts at the three thresholds + the in-workflow guardrails above.
