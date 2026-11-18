# C1 baseline — reliability evidence (pre-change state)

Recorded before C1 modifications, per C1.1.

## Environment

- **Main HEAD:** `165cce5` (post-A8 merge + docs; A8 merge `8fbdf96`)
- **Working tree:** clean (three untracked run artifacts under
  `docs/a8-evidence/` — installer outputs, not source)
- **Migration head:** `0026_a7` → `0025_a6` (A8 adds none)
- **CI:** post-merge run `44396217637` — 6/6 success on `165cce5`
  (backend 3.12, backend 3.14, frontend, tauri-check, changes,
  package — frozen sidecar + NSIS verified)
- **Test totals at baseline:** backend **1851 passed** / 11 skipped;
  frontend **411 passed** (29 files). Installed acceptance A8 34/34 on
  `4e6fda3` (code-identical to `a3161f9`; merged `8fbdf96`).

## Flake inventory + root causes (C1.2)

| Test | Failure signature | Root cause | Class |
|------|-------------------|------------|-------|
| `test_process_exit_is_bounded_with_stalled_scan` | probe asserts "scan was not scheduled" under suite load | `ImportWatchService._tick` pauses scheduling when `pressure()=="critical"` — the probe ran the REAL host pressure check, so genuine CI memory pressure paused scheduling before the exit property was exercised | host contention, not a product defect |
| `test_disk_preflight_blocks_video_job` | `wait_for_job` 15s poll timeout under saturation | test raced the background runner; admission failure (the product property) isn't time-bound | host contention |
| `test_edl_v2_serialization_scales` | 1.021s vs 1.0s absolute bound | absolute wall-clock bound on shared CI; the real property is O(clips+tracks) | host contention |

Deterministic fixes on branch `feat/c1-reliability`:

1. Probe injects `pressure=lambda: "ok"` — exit-bound property
   unchanged, scheduling no longer host-dependent.
2. `eng._execute(job_id)` driven synchronously via `asyncio.run`
   (publish stubbed — app-loop-bound); the loop may execute the job
   concurrently with identical terminal state.
3. Median-of-3 wall times at 250 vs 1000 clips; assertion is the
   **scaling ratio** (<8× for 4× data — quadratic regressions give
   ~16×) plus a generous absolute cap as a hang guard. Stronger than
   the old bound and host-speed-agnostic.

## Existing reliability coverage map (verified, pre-C1)

- **Job recovery:** `reconcile_interrupted` requeues resumable
  running jobs, fails non-resumable, purges partial workfiles
  (`test_jobs`, `test_c4e_resources`); attempts close `interrupted`
  on reconcile (`attempts.close_open_attempts`).
- **Cancellation:** hold-barrier cancel, encode-subprocess kill,
  cancel-after-encode, retry-after-cancel (`test_a4/a5`); queued
  cancel releases spend holds (`test_a7_scheduling`).
- **Idempotency:** `idempotency_key` dedupe (`test_jobs`), command
  `expected_*` pins refuse stale commits.
- **Reservations:** A7 scheduler suite — acquire/release on every
  exit path, reconcile-before-admission at startup, device
  loss/return, fairness, heterogeneous devices, no VRAM pooling.
- **Lifecycle:** watchdog suite (`test_first_run_watchdog`) — worker
  kill tree, telemetry loss fail-closed, PID reuse safety, deadline
  hangs; watch-service lifecycle + bounded-exit probe (C15).
- **Migrations:** `test_migrations_create_all_tables`; genuine
  pre-release DB fixtures (`pre_n1_release.db`, `pre_n3_release.db`)
  with backfill/raw-DDL/idempotent tests (`test_n1a_tracks`);
  `run_migrations` idempotent; `UnsupportedRevisionError` refuses
  newer-stamped DBs (fail-closed downgrade protection).
- **Backup:** 73 tests — corrupt rejection before commit, partial
  package sweep on startup, missing-blob honesty, CAS hash checks,
  cross-project id remap, A8 settings sanitization (providers +
  credential-shaped keys stripped), wizard progress survival.
- **FFmpeg:** cancel-kills-ffmpeg tests; bundled ffprobe validated in
  every installed acceptance.

## C1 additions (new `test_c1_reliability.py`, 9 tests)

Adapter crash mid-run (attempt `failed`, error surfaced, work dir
purged, cancel event released) · `WorkerDeadError` attempt outcome
`worker_dead` · `_finalize_crash` marks non-terminal `failed` · CAS
registration failure rolls back honestly (no succeeded-without-
artifact, no orphan rows) · terminal cancel is a no-op · queued jobs
untouched by reconcile · migration chain linearity (single head, all
down_revisions resolve, no cycles, base reachable) · idempotent
re-upgrade · newer-stamped DB refusal.

## Measurable C1 acceptance criteria

- Zero recorded flakes remain load-sensitive: the three named tests
  must pass under repeated suite runs without timing dependence.
- New fault-injection tests pass deterministically (no wall-clock
  assertions on product behavior).
- Full suite: baseline 1851 + new C1 tests green on 3.12 and 3.14.
- Installed acceptance: 15-check C1 driver green on the C1 build
  (interruption, cancel/retry, reservation recovery, uninstall data
  preservation included).
- No product-code changes except where a genuine defect is found and
  separately justified (none found so far — all three flakes were
  test-side).

## Remaining risks / non-goals

- Windows logoff behavior is not automatable in CI — noted for the
  C4 human checklist.
- `sim.gpu.*` remains simulated-only; no real GPU claims.
- Stage B2b ON HOLD; issue #96 still gates live retrieval.
