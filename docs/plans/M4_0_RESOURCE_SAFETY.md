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

## Stage 3A — non-inference hardware validation (2026-11-09, 163/163 tests)

| Check | Result |
|---|---|
| Spawn race | ELIMINATED — workers spawn `CREATE_SUSPENDED`, assigned to the job object, THEN resumed (`spawn_supervised`); fails closed: no assignment → process never runs (`SupervisionError`) |
| Backend force-kill | `test_backend_death_reaps_worker`: helper process hard-killed via `taskkill /F`; supervised child died with it — kernel reaped on last handle close |
| Normal exit | worker exits cleanly, handle released, registry empty |
| Forced termination | `terminate_tree` kills stubborn sleeper, pid dead <10 s |
| Fail-closed supervision | job-object failure raises before the worker resumes |
| Repeated cycles | 8× spawn/exit/unsupervise — zero handle/registry leak |
| RAM measurement | RSS + avail-RAM snapshots bound tiny worker cost (<2 GiB drift) |
| Watchdog fail-closed | exception inside the watchdog now aborts the worker, never silently disables |
| Concurrent reservations | `_reserved_ram`: each gen job's estimate subtracts from others' headroom; released on every finalize path |

### Memory budget — 16 GB machine, Wan 2.1 T2V, defaults

| Item | GiB |
|---|---|
| Offloaded components (diffusion+VAE+UMT5, `weight_offload_cpu`) | 6.28 |
| Runtime overhead (activations, buffers, encoder scratch) | 2.00 |
| **Job estimate** | **8.28** |
| OS headroom (warn below) | 4.00 |
| Critical floor (block below) | 1.00 |
| **Free RAM required to launch without warning** | **12.28** |
| Absolute minimum to launch (est. + critical) | 9.28 |

Honest implication: on a normally loaded desktop (~4–6 GiB free) the
preflight now BLOCKS Wan rather than launching into exhaustion. The
earlier successful run only escaped because it predated the gate.
NOTE: 5.5 GiB observed worker RSS is NOT assumed to be peak CUDA need —
the estimate uses full component bytes plus overhead, deliberately
conservative.

## Memory-residency analysis (corrected)

The 6.28 GiB = downloaded FILE bytes. Residency during inference differs
by component and runtime behavior — measured vs estimated vs unknown:

| Component | File bytes | Residency under `--offload-to-cpu` | Basis |
|---|---|---|---|
| diffusion fp16 | 2.64 GiB | host-RAM resident (malloc) or file-mmap'd; streamed to GPU layer-wise | sd.cpp docs describe the flag as "keep weights in RAM"; ggml safetensors loaders may mmap — **not verified which** |
| UMT5 Q4_K_M gguf | 3.40 GiB | ggml typically mmaps GGUF → file-backed pages resident once touched | **estimate** (standard ggml behavior, unverified for this path) |
| Wan VAE | 0.24 GiB | CPU under Vulkan (`--backend vae=cpu`); GPU-resident under CUDA | measured: CUDA VAE on GPU |
| Activations/buffers | — | malloc'd host+device, transient | unknown — not yet measured on CUDA |
| encode/decode buffers | — | small vs weights | estimate |

Measured: Vulkan worker RSS peaked ≥ ~5.5 GiB (cancelled run); CUDA
run completed but RSS peak was NOT sampled. Honest position:

- **Physical-RAM need: measured ≈ 5.5 GiB, estimate ceiling 8.28 GiB.**
  The gate uses the ceiling — conservative, may block jobs that would
  fit. Over-blocking is the safe failure direction.
- **Commit impact is probably lower than the file sum** — file-mmap'd
  pages are file-backed (no commit charge) though they still consume
  physical RAM when resident. Both metrics now gate independently;
  avail-phys is the binding one for Wan.
- Unknowns retained: exact mmap usage per component, CUDA-path peak RSS,
  activation size vs frame count. Issue #26 (calibration) stays open.

## Validation options (no new downloads)

**Option A — sufficient measured free RAM (RECOMMENDED).**
- Need: ≥ 9.28 GiB free physical (est. + 1 GiB critical floor);
  ≥12.28 GiB to run without a warning.
- Achievable by closing browsers/IDE before the run — no downloads.
- Procedure: check `/api/hardware` memory → preflight decides (ok → run;
  warn → requires `memory_risk_acknowledged`; block → refuse).
- Settings: CUDA, 832×480, 17 f, ≤300 s cap, watchdog armed.
- Risk: honest gate; still the heaviest workload this machine can do.

**Option B — lower-memory settings, same installed components.**
- Drop `--offload-to-cpu` on CUDA: weights on GPU instead of RAM
  (~2.9 GiB diffusion + ~0.5 VAE on-GPU; UMT5 still host-side ~3.4 GiB).
  Estimated job RAM ~4–5 GiB — fits typical free RAM; VRAM ~4–6 GiB at
  480p/17f — plausible on 8 GB but UNVERIFIED; OOM would surface as a
  clean worker error, not system exhaustion (weights off host RAM).
- Not a new download — same 3 files, different residency flags.
- Risk: possible OOM abort mid-run (safe failure) or sd.cpp may still
  page weights through RAM.

**Option C — defer to a machine with ≥ 24–32 GB RAM.**
- Zero risk; full validation on adequate hardware; #28 stays open
  meanwhile with the safety system already landed.

**Recommendation: A with B as fallback** — B first attempts may actually
be the LOWER-risk run (less host RAM) and uses only approved artifacts.

## Option B feasibility — CUDA without `--offload-to-cpu` (investigation only)

The installed build exposes per-module placement flags (verified on
`sdcpp-cuda/sd-cli.exe --help` @ pinned release):

| Flag | Effect |
|---|---|
| `--backend te=cpu,vae=cuda0,...` | per-module COMPUTE device |
| `--params-backend disk\|cpu\|<mod>=...` | per-module parameter RESIDENCY — `disk` streams weights from mmap'd files |
| `--max-vram <GiB>` | per-device budget; triggers automatic segmented graph execution |
| `--offload-to-cpu` | weights host-RAM resident, stream to VRAM per need (current Wan path) |
| `--mmap`, `--eager-load`, `--disable-prefetch`, `--split-mode` | residency/loading controls |

### Configurations on 8 GB VRAM / typical 4–6 GiB free RAM

| | weights→GPU | weights→RAM | est. VRAM | est. host RAM | verdict |
|---|---|---|---|---|---|
| B1 all-GPU | 6.28 GiB | ~0 | ~8–9 GiB (w/ activations) | ~2.3 GiB | **likely OOM** — exceeds usable VRAM |
| B2 `te=cpu` | 2.88 GiB (diff+VAE) | 3.40 GiB (UMT5) + ~0.4 paging | ~4.5–5 GiB | ~5.4 GiB | **viable** — fits VRAM; RAM borderline at 4 GiB free, fine at 6 |
| B3 `params-backend disk` | streams segments | ~1 GiB touched | ≤`--max-vram` budget | ~2.9 GiB | **safest** — fits everywhere; slowest |

Basis: component sizes = measured file bytes; activation/attention
~1.5–2.5 GiB at 832×480/17f = **estimate** (not measured on CUDA);
`te=cpu`/`disk` support = documented on the installed binary's help —
**unverified end-to-end** (no run authorized). OOM outcome under B1/B2:
worker exits nonzero → clean failed job, no partial asset, supervision
unchanged — a SAFE failure mode.

### Preflight modeling (implemented, not yet run)

`_estimate_ram_bytes` now takes `residency_mode`: `cpu_offload`
(8.28 GiB est), `gpu` (~3.9), `te_cpu` (~5.8), `disk` (~2.9). Wan's
current catalog default remains `cpu_offload` — conservative until a
measured run proves otherwise. VRAM-side gating for residency modes is
**not yet modeled** — documented gap; gpu_sched's estimate still uses
the offload figure (over-estimates for B2/B3 = safe direction).

### Remaining limitations (supervision)

Job Objects kill the tree on parent death; the `taskkill /T /F` fallback
in `terminate_tree` assumes Windows. No known gaps for the approved
configurations.

## Hardware comparison for local generation

| RAM | Image (SD 1.5) | Wan T2V (current path) | Practical meaning |
|---|---|---|---|
| 16 GB | fine — 4 GB weights + small footprint | borderline: offload path needs ~9–12 GiB free; blocked on a loaded desktop; Option B (te=cpu/disk) is the workaround | daily-driver usable with care; video is the stretch case |
| 32 GB | fine, headroom for parallel apps | comfortable: offload path fits without closing apps | recommended floor for video generation |
| 64 GB | no meaningful gain over 32 for current catalog | enables bigger stacks (14B I2V components ≈ 30+ GiB host) | headroom for future larger models + multitasking |

RAM ≠ VRAM: the 8 GB card still bounds model size, resolution, and
frame count regardless of system memory. 32 GB is the sensible
minimum-for-video recommendation; 64 GB is forward-headroom, not a fix
for VRAM.

## B2 vs B3 — real-inference proposal (PENDING owner approval)

Verified against the installed pinned binary's `--help` (module names
`diffusion`, `vae`, `te` confirmed; `disk` is a valid `--params-backend`).

### B2 — `te=cpu` (recommended first real test)

```
sd-cli.exe -M vid_gen --diffusion-model <fp16> --vae <vae> --t5xxl <gguf>
  -p "..." -n "..." --video-frames 17 --fps 24 -W 832 -H 480
  --cfg-scale 6.0 --sampling-method euler --flow-shift 3.0 --vae-tiling
  --backend te=cpu            # text encoder on host
  --seed 42 -o <job>.webm -v
```

| Measure | Estimate | Basis |
|---|---|---|
| GPU VRAM | ~6.0 GiB (diff+VAE 2.88 + act ~3.1) | file bytes measured; act = estimator heuristic |
| Host RAM | ~5.8 GiB (UMT5 3.40 + 15% rest + 2.0 overhead) | component files + overhead model |
| Free RAM needed | ≥9.8 GiB no-warn; ≥6.8 minimum | gate math |
| Free VRAM needed | ≥6 GiB + GPU headroom | gpu_sched usable check |

### B3 — `--params-backend disk` (safest, slowest)

Same argv but: `--params-backend disk --mmap --max-vram 6` — weights
stream from mmap'd files under a 6 GiB device budget.

| Measure | Estimate |
|---|---|
| GPU VRAM | ~4.0 GiB (bounded by budget) |
| Host RAM | ~2.9 GiB |
| Free RAM needed | ≥6.9 GiB no-warn; ≥3.9 minimum |

### Shared run conditions

- CUDA flavor only; 17 frames / 832×480 / 30 steps / seed 42.
- `ATOMIC_MAX_WORKER_SECONDS=300`; watchdog at defaults (5 s period).
- Monitor every 2 s: worker RSS, avail phys, commit headroom, VRAM.
- Auto-abort: critical headroom, runtime cap, surviving process post-job.
- Emergency stop: job cancel endpoint → terminate_tree; any parent
  death → kernel reaps via the Job Object.
- OOM outcome: clean nonzero exit → `[cuda/<mode>] out-of-memory` job
  error; partial `.webm` deleted pre-asset; RAM+GPU reservations
  released in the engine finally path.
- Unknowns: activation peak on CUDA, `te=cpu`/`disk` end-to-end behavior,
  B3 throughput. First test: **B2, one attempt, no retries**.
