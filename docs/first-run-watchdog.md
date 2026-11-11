# First-run watchdog — independent resource safety

`app.domain.firstrun_watchdog` is a standalone operator-run monitor for
the controlled first real-inference test. It runs in its own OS process,
**outside** the backend, so it keeps working even if the backend hangs
or the normal `POST /api/jobs/{id}/cancel` path is unreachable. It never
talks to the backend API or any sd-server HTTP endpoint — it only reads
process/GPU telemetry and kills, so it cannot trigger model loading or
inference.

## Run it

In a **separate terminal**, before committing the generation command:

```powershell
scripts\first-run-watchdog.ps1                    # defaults: GPU 0, 420 s
scripts\first-run-watchdog.ps1 -DryRun            # observe + log, never kill
```

or directly:

```powershell
backend\.venv\Scripts\python -m app.domain.firstrun_watchdog `
    --gpu-index 0 --deadline-s 420 `
    --log "$env:LOCALAPPDATA\AtomicAIStudio\watchdog\first-run.jsonl"
```

**Proceed only after the `"event": "ready"` record appears** in the log
(and stdout) — that is the machine-verifiable signal that all startup
gates passed and monitoring is armed. Exit codes: `0` clean, `2` breach
terminated the worker tree, `3` watchdog error/refused.

## Startup gates — all must pass before "ready"

| Gate | Requirement | On failure |
|---|---|---|
| Process scan | enumeration works, baseline **empty** (`--allow-baseline` overrides) | `refused`, exit 3 |
| Telemetry | `nvidia-smi` + `psutil` respond (bounded retries) | `refused`, exit 3 |
| VRAM readiness | free VRAM ≥ **4.5 GiB** | `refused`, exit 3 |
| RAM readiness | available RAM ≥ **6 GiB** | `refused`, exit 3 |

A `refused` record carries the exact reason. With a non-empty baseline
the pre-existing `sd-server.exe` PIDs are printed — terminate them and
restart; a watchdog that starts with a polluted baseline could never
safely guard the run.

## In-run breaches (after "ready")

| Condition | Threshold | Grace |
|---|---|---|
| Free VRAM on watched GPU | < 0.5 GiB | 2 consecutive samples |
| Available system RAM | < 1.5 GiB | 2 consecutive samples |
| sd-server tree RSS | > 8 GiB | 2 consecutive samples |
| Test duration | > 420 s | immediate |
| Extra sd-server process | > 1 post-baseline | immediate |
| Telemetry unavailable | nvidia-smi/psutil failure | 3 consecutive → fail closed |

Gauge breaches need consecutive samples so a single transient spike
can't kill the run; structural conditions fire immediately.

## Kill targeting — identity, not just PID

Every tracked process is recorded as `(pid, create_time)` and that
identity is re-verified **immediately before every kill** — including
the `taskkill /F /PID` fallback (which is deliberately *not* `/T`: `/T`
would reap the unrelated children of a reused pid). A pid whose
create_time no longer matches was reused by the OS — it is a different
process and is never signaled; it is reported in the termination
record under `identity_mismatch`.

Baseline entries are identity-scoped too: a baseline pid that is later
reused by a *new* `sd-server.exe` counts as a fresh post-baseline
worker (correct — it is not the process that was snapshotted).

If the worker exits but leaves orphan children, those orphans are
identity-checked and killed.

## Evidence

Every ~1 s sample is appended to the JSONL log (and stdout):
`{ts, elapsed_s, ram_available_mib, gpu_free_mib, worker_pids,
tree_rss_mib}`. Gate results appear as `start` / `startup_probe` /
`ready` / `refused` records. A breach writes a terminal `termination`
record with reason, targets, killed, survivors and identity mismatches.

## Limitations (honest)

- **~1 s granularity** — a fast VRAM spike can OOM inside the 2-sample
  debounce window; `--interval-s` can be lowered.
- **RSS ≠ VRAM** — the 8 GiB tree ceiling watches system RAM residency;
  per-process GPU memory isn't visible (only card-level free via smi).
- **GPU index mapping** — `--gpu-index` is the nvidia-smi index; equals
  the app's device 0 on this single-GPU machine.
- **nvidia-smi required** — the startup gate refuses to arm without it
  (fail closed); mid-run loss also fails closed after 3 samples.
- **Process-name matching** — discovery keys on `sd-server.exe`; a
  renamed binary is invisible (but the app couldn't launch one either).
- **No backend visibility** — by design; it guards the process
  boundary, not job state.
- **Ordering is procedural** — the watchdog cannot stop an operator
  from committing a generation without it; the `ready` record +
  empty-baseline gate are the verifiable checkpoints.
