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
as a budget breach: pause merges that only re-run CI, prefer local
`scripts\validate.ps1` evidence for review, and defer non-urgent
`main`-push packaging until the cycle resets.

## Per-run cost model

Observed durations (before caching effects):

| Job | Runner | ~Minutes | Billed |
|---|---|---|---|
| changes | ubuntu | 0.5 | 0.5 |
| backend (3.12) | windows | 5.5 | 11 |
| backend (3.14) | windows | 5.5 | 11 |
| frontend | ubuntu | 1.5 | 1.5 |
| tauri-check | windows | 2 | 4 |
| package | windows | 12 | 24 |

## Per-PR projection vs. November baseline

| PR type | Old flow | New flow | Savings |
|---|---|---|---|
| Backend-only PR (typical) | ~52 | ~22.5 | ~57% |
| Frontend/UI PR | ~52 | ~24 | ~54% |
| Packaging-sensitive PR | ~52 | ~48 | ~8% |
| Docs-only PR | ~52 | ~0.5 | ~99% |

November actual: ~2,285 billed-minutes across ~64 runs / ~300 jobs.
At identical activity the new structure projects **~1,900–2,200
billed-minutes/month** — below the 2,400 ceiling — because typical PRs
drop to ~22–24 billed and superseded/incident waste is eliminated:

- Superseded PR pushes cancel in-flight runs instead of finishing them.
- Job timeouts cap a hung run at ~2x normal instead of the observed
  45+ minute stall (~90 billed-minutes for one incident).
- FFmpeg binaries (~100 MB+ per job) are cached per manifest hash.
- `package` (24 billed) no longer runs on ordinary PRs.

## Guardrails (implemented in ci.yml)

1. `concurrency` group per ref with `cancel-in-progress: true` —
   force-pushing a PR branch cancels its superseded run.
2. `timeout-minutes` on every job (changes 5 / frontend 10 /
   tauri-check 15 / backend 20 / package 30).
3. `changes` job (ubuntu, ~0.5 min) gates PR jobs by touched paths and
   **fails open** — a detection error runs everything, never skips.
4. `package` only runs on `main` pushes, manual `workflow_dispatch`,
   or PRs touching packaging-sensitive paths (PyInstaller spec/entry,
   pyproject deps, ffmpeg manifest, build-sidecar/fetch-ffmpeg scripts,
   `src-tauri/**`, the workflow itself).
5. `frontend` runs on `ubuntu-latest` (platform-independent tsc+vite,
   1x billing); all Windows-specific jobs stay on Windows.
6. Caches: pip, npm, Rust (target + cargo registry + git db), FFmpeg
   vendor dir keyed to `backend/ffmpeg-manifest.json`.

## Required-check behavior

Job names are unchanged (`backend (3.12)`, `backend (3.14)`,
`frontend`, `tauri-check`, `package`), so required-check identities in
branch protection are preserved. A job skipped by its `if:` reports a
**skipped/neutral** conclusion, which counts as success for required
checks — an ordinary PR still shows `package` as green without paying
for it. Never use workflow-level `paths-ignore` for this reason: a
filtered workflow reports *no checks at all* and required checks stall
as "expected".

## Monitoring

- **Weekly:** github.com/settings/billing → "Actions & Packages" shows
  cycle-to-date billed minutes vs. allowance.
- **Alerts:** enable usage alerts at 75% / 90% / 100% on the billing
  page (the 100% point should never be reached under this policy —
  2,400 is the working ceiling).
- **CLI (user-side):** `gh api user/settings/billing/actions` returns
  `total_minutes_used` / `included_minutes` (requires `user` scope).
- **Per-run audit:** `gh api repos/mrgwart1977/AtomicAIStudio/actions/runs`
  lists durations; billed = sum(duration) x OS multiplier.
- **Warning threshold:** if cycle usage passes ~70% (2,100) before the
  final week, switch pre-merge validation to `scripts\validate.ps1`
  locally and reserve CI for merge-candidate PRs only.
