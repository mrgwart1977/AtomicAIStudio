# M4.0 — Resource-Exhaustion Incident: Root Cause & Safeguards

Issue: **#28** (CRITICAL / M4.0 merge blocker). Incident: after Wan 2.1 T2V
tests on RTX 3060 Ti 8 GB / 16 GB RAM, Task Manager showed sustained ~99 %
CPU + ~99 % physical RAM; system unresponsive; Windows restart required.

## Root-cause assessment (evidence-backed, most-likely-first)

1. **Orphaned `sd-cli.exe` after backend death — primary suspect.**
   Workers spawned via `asyncio.create_subprocess_exec` had **no OS-level
   supervision**: on Windows, a child survives its parent's force-kill and
   keeps running detached. A Wan worker holds ~5.5 GiB RSS and saturates
   ~7 logical cores while sampling — one orphan exactly reproduces the
   sustained ~99 % CPU/RAM until reboot. During validation the dev backend
   was force-killed several times while jobs existed; any overlapping
   worker would have been orphaned. **Fix: Windows Job Objects with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`** — the kernel kills the whole
   tree when the last handle closes, even on `taskkill /F` or a crash.
   Verified by `test_job_object_kills_worker_when_handle_closed`.

2. **RAM never gated — compounding cause.** Compatibility/preflight
   checked VRAM only. Wan's spec `min_ram_bytes` equalled the machine's
   *total* RAM; `--offload-to-cpu` streams all ~6.28 GiB of components
   through physical RAM, and observed worker RAM was already marginal on
   a 16 GB box with a normal desktop load. **Fix:** `resources.py`
   (physical RAM vs commit charge, OS headroom + critical thresholds),
   `jobs._memory_preflight` (block/warn before execution), and a
   RAM-aware `classify_model` gate on *available* RAM.

3. **No watchdog / post-job verification — detection gap.** Nothing
   sampled system pressure during inference or confirmed the worker was
   dead afterward. **Fix:** 5 s pressure watchdog (abort → `terminate_tree`)
   + `_verify_worker_dead` + `proc_guard.unsupervise` in every exit path;
   `atexit` reaper for graceful exits.

Not classified as a leak: ordinary file caching/standby memory is
expected and excluded. The orphan + ungated-RAM combination fully
explains the observations; no historical memory measurements were taken,
so the exact pid is not provable — that limitation is documented here.

## Safeguards implemented (this commit)

| Layer | Mechanism |
|---|---|
| Supervision | `proc_guard.supervise` → kill-on-close Job Object per worker (both adapters) |
| Orphan cleanup | kernel kill-on-close + `atexit` reaper + `terminate_tree` (`taskkill /T /F`) |
| Preflight | `_memory_preflight`: offloaded weights + 2 GiB overhead vs avail RAM; block/warn recorded in `progress_json` |
| Compatibility | `classify_model` demotes to `limited`/`unsupported` on available RAM |
| Watchdog | 5 s check: critical commit/RAM headroom or runtime cap → abort (never CPU%) |
| Post-job | `_verify_worker_dead` forces a tree kill if anything survived |
| Config | `ATOMIC_OS_HEADROOM_BYTES` (4 GiB), `ATOMIC_CRITICAL_COMMIT_BYTES` (1.5 GiB), `ATOMIC_CRITICAL_PHYS_BYTES` (1 GiB), `ATOMIC_MAX_WORKER_SECONDS` |

## Lifecycle audit result

All 15 mandated exit paths funnel through the two worker loops; every
path now ends in `finally { kill → _verify_worker_dead → unsupervise }`.
Cancel remains `JobCancelled`; validation-failure deletes partial output
before raising; commit happens only after probe-validate.

## Tests

`tests/test_m4_safety.py` — 13 simulated lifecycle/pressure tests, all
bounded (sleeps, monkeypatched memory). Suite: **157/157**.

## Remaining risks (honest)

- ~ms race between spawn and `AssignProcessToJobObject` — bounded, noted.
- Watchdog period 5 s; a worker could grow memory between checks — the
  OS headroom + critical thresholds provide the floor.
- Peak VRAM not sampled live during CUDA run (succeeded, no OOM).
- `min_ram_bytes` semantics vs available-RAM: spec still documents total
  recommended, gate uses measured availability.

## Controlled reproduction plan (Stage 2 — needs approval)

- CUDA flavor (fastest, 2.6 min observed), 832×480, **17 frames**, seed 42.
- `ATOMIC_MAX_WORKER_SECONDS=600` cap + default pressure thresholds.
- Monitor: 2 s samples of worker RSS, avail RAM, commit headroom, VRAM.
- Abort: critical thresholds OR any orphan after job end → stop.
- Expected: worker dead < 5 s after completion, RAM returns to baseline.
- Emergency stop: cancel endpoint; the Job Object reaps on parent death.

Do NOT repeat the 34-min Vulkan run as the first reproduction.
