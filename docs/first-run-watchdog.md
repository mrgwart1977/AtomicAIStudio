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

Exit codes: `0` clean (worker finished or never appeared), `2` breach
terminated the worker tree, `3` watchdog error.

## Enforced breaches (first-run thresholds)

| Condition | Threshold | Grace |
|---|---|---|
| Free VRAM on watched GPU | < 0.5 GiB | 2 consecutive samples |
| Available system RAM | < 1.5 GiB | 2 consecutive samples |
| sd-server tree RSS | > 8 GiB | 2 consecutive samples |
| Test duration | > 420 s | immediate |
| Extra sd-server process | > 1 post-baseline | immediate |
| Telemetry unavailable | nvidia-smi/psutil failure | 3 consecutive → fail closed |

Gauge breaches need consecutive samples so a single transient spike
can't kill the run; structural conditions (deadline, extra worker,
monitor loss) fire immediately.

## What it can and cannot kill

At startup the watchdog snapshots all running `sd-server.exe` PIDs as
**baseline** and never touches them. Only post-baseline sd-server
processes — the spawned inference worker — plus their descendants are
termination targets. A foreign sd-server (or any other process) is
never killed. If the worker exits but leaves orphan children, those
orphans are treated as part of the run and killed.

## Evidence

Every ~1 s sample is appended to the JSONL log (and stdout):
`{ts, elapsed_s, ram_available_mib, gpu_free_mib, worker_pids,
tree_rss_mib}`. A breach writes a terminal `termination` record with the
reason, target PIDs, killed PIDs, and any survivors.

## Limitations (honest)

- **~1 s granularity** — a faster VRAM spike can OOM before the second
  confirming sample. The grace count trades safety for flap-resistance;
  `--interval-s` can be lowered.
- **RSS ≠ VRAM** — the 8 GiB tree ceiling watches system RAM residency;
  GPU memory is only watched via nvidia-smi free, not per-process.
- **GPU index mapping** — `--gpu-index` is the nvidia-smi index. On this
  single-GPU machine it equals the app's device 0; multi-GPU machines
  must map it manually.
- **nvidia-smi required** — if the driver tool is missing, GPU telemetry
  fails closed after 3 samples. RAM/process monitoring alone still runs.
- **Process-name matching** — discovery keys on `sd-server.exe`. A
  renamed binary is invisible (but then the app couldn't launch it
  either — the name is fixed by the runtime install).
- **No backend visibility** — by design. It cannot see queued jobs,
  worker leases, or HTTP state; it guards only the process boundary.
