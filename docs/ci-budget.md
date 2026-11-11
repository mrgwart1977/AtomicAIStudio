# CI Budget & Usage Policy

**Current posture (post open-source transition):** the repository is
**public**, so standard GitHub-hosted runners (`ubuntu-latest`,
`windows-latest`) are **unmetered** — GitHub does not bill public-repo
Actions usage on hosted runners. The historical private-repo billing
data below is preserved for reference and would apply again only if the
repository ever returned to private visibility.

Why the cost optimizations in `ci.yml` still matter on free CI:

- **Queue time**: fewer minutes per run = faster feedback for
  contributors and reviewers.
- **Fair-use & abuse resistance**: public repos attract spam/abusive
  runs (crypto-mining PRs, junk pushes). Path gating, concurrency
  dedup, and timeouts bound what any single run can consume.
- **Forked PRs**: approval-for-outside-collaborators is enabled, but
  every approved run should still spend the minimum necessary.
- **Reversibility**: if the repo ever goes private again, the OS
  multipliers and measured baselines below apply immediately.

GitHub billing model (if it applies): minutes are billed per job
wall-clock time multiplied by OS — **Ubuntu 1x, Windows 2x, macOS 10x**.

## Guardrails (implemented in ci.yml)

1. `concurrency` per ref + `cancel-in-progress` — force-push cancels
   the superseded run; spam-pushing a PR re-spends the minimum.
2. `timeout-minutes` on every job (5/10/15/20/30) — caps hangs at ~2x
   normal instead of the observed 45-min stall (was ~90 billed-min).
3. `changes` job gates PR jobs by touched paths; detection uncertainty
   (unresolvable base, diff error, empty diff) outputs all-true and
   runs the full suite — it can only overspend, never under-validate.
4. `scripts/` is CI-sensitive: any change under it triggers backend +
   frontend + tauri; `package` additionally requires the
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
7. `frontend` on ubuntu (cheapest/fastest); all Windows-specific jobs
   stay Windows.
8. Caches: pip, npm, cargo target+registry+git, FFmpeg vendor keyed on
   `backend/ffmpeg-manifest.json`.

Static coverage: `scripts/test-ci-paths.ps1` asserts the classifier's
real patterns against 19 cases (backend/frontend/tauri/package/CI-
workflow/scripts/docs-only/empty-diff/rename/mixed). The `changes` job
runs it on every CI invocation as the "Verify path classifier" step;
run it locally after any edit to the `out` patterns.

## Required-check behavior

Job names (`changes`, `backend (3.12)`, `backend (3.14)`, `frontend`,
`tauri-check`, `package`) are stable — required-check identities
preserved. Skipped = neutral = success for required checks. Never add
`paths-ignore` at workflow level — a filtered workflow reports *no*
checks and required checks stall as "expected".

## Monitoring & abuse response (public repo)

| Control | Mechanism | Type |
|---|---|---|
| Fork-PR gating | Settings → Actions → "Require approval for all outside collaborators" — junk PRs can't self-run | Repo setting (enabled) |
| Superseded-run waste | `concurrency` cancel | In-workflow, automated |
| Hang/runaway waste | `timeout-minutes` | In-workflow, automated |
| Path gating / package skip | `changes` job + classifier self-test | In-workflow, automated |
| Usage audit | `gh api .../actions/runs` durations x OS multiplier | Manual |
| Usage review | Insights → Actions usage metrics; watch for abnormal run counts/durations | Manual (weekly) |
| Spam/abusive PRs | Close + report; approval-gate already prevents unvetted runs | Manual |
| Spending guard | Billing → spending limit $0 still applies to any billable usage (packages storage, private repos) | Native, automated |

Offline fallback: `scripts\validate.ps1` reproduces the full CI gate
locally (backend on both Pythons + frontend + optional Tauri/package)
with no Actions dependency — useful for validating during CI incidents
or before pushing.

## Historical data — private-repo era, November 2026

*Measurements taken while the repository was private and minutes were
metered. Retained as the baseline for cost modeling; not applicable to
current billing.*

Private-era monthly budget (obsolete now):

| Item | Minutes |
|---|---|
| Allowance (upgraded free tier) | 3,000 |
| Planned-validation ceiling | 2,400 |
| Reserve (releases, reruns, incidents) | 600 |

Measured November 2026 baseline — `gh api .../actions/runs` job
durations x OS multiplier (67 runs / 300 jobs):

| Segment | Runs | Billed-min |
|---|---|---|
| pull_request — code branches | 40 | ~1,580 |
| pull_request — docs-only (docs/, chore/) | 6 | ~66 |
| push (main) | 19 | ~719 |
| dynamic | 2 | ~2 |
| **Total** | **67** | **~2,367** |

Measured averages (private era): successful PR run ≈ **37.5 billed**,
successful main push ≈ **39.5 billed**.

Per-PR projection (measured durations x path gating):

| PR type | Old | New | Savings |
|---|---|---|---|
| Backend-only (`backend/**`) | ~37.5 | ~22.5 | ~40% |
| UI-only (`apps/desktop/src/**` etc.) | ~37.5 | ~2 | ~95% |
| `src-tauri` only | ~37.5 | ~6 | ~84% |
| Backend + UI mixed | ~37.5 | ~24–28 | ~30% |
| Packaging-sensitive | ~37.5 | ~46 | ~0% (runs all) |
| Docs-only | ~37.5 | ~0.5 | ~99% |

Monthly projection at November activity: **~1,600–1,800 vs 2,367
measured → ~25–32% reduction**. The structural savings matter beyond
billing: waste classes (superseded runs, hangs, docs-only full runs)
are eliminated rather than amortized — on public CI that translates
directly to queue time and abuse surface.
