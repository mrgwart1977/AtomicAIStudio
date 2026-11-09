# M4.1 — Persistent Inference Workers & GPU Memory Management

**Status:** PLANNED — not started. Requires explicit approval before
implementation. Tracked as GitHub issue #23.

**Motivation (measured, not assumed):** every generation job today spawns a
fresh `sd-cli.exe` worker that reloads the ~2 GiB model — the observed
prime-vs-warm spread (24.2 s cold vs 7.75 s warm) is mostly model load +
process init. A persistent loaded-model worker could eliminate that on
repeat jobs — **if** the runtime supports it. No speed figure is promised
before measurement.

## B.1 — Runtime feasibility investigation (gate)

Investigate before any implementation:

- Does sd.cpp support a long-lived inference mode (server/REPL/API mode,
  repeated `-M img_gen` without reload, or a library interface)?
- Can individual requests be cancelled without killing the worker?
- Can weights be unloaded/reloaded at runtime (model swap)?
- How does it behave after a worker crash — auto-restart safety?

If sd.cpp offers no suitable interface, evaluate alternatives **with the
same approval discipline as any new runtime**:

| Option | Notes |
|---|---|
| Embedded API mode in sd.cpp | upstream feature — check releases/issues |
| Dedicated inference service | app-managed daemon, one per GPU/model |
| Different runtime adapter | e.g. a server-native engine — would enter the runtime catalog like sdcpp did, with license/artifact verification |
| Keep subprocess-per-job | proven fallback — must remain intact regardless |

## B.2 — Persistent worker pool (design targets)

- Reuse an already-loaded model for consecutive compatible jobs
- Worker identity = (model_key, runtime, flavor, gpu_index) — no mixing
- Configurable max workers (global + per GPU)
- Idle-timeout unload; memory-pressure unload; health checks; crash
  detection + safe restart; graceful app-shutdown drain
- Cancellation of a single in-flight request without corrupting the
  worker's next job (or clean worker recycle if it can't)

## B.3 — GPU memory management

- Per-GPU VRAM budgets; model-residency accounting (loaded models consume
  reserved VRAM even when idle — shown to the user)
- Available vs reserved accounting feeds the scheduler's fit check
  (replaces today's estimate-only path with measured residency)
- Memory-pressure detection → unload least-recently-used resident worker
- Safe unload before launching an incompatible job
- Single-GPU and multi-GPU configs; mixed GPU models/VRAM — **VRAM never
  pools across GPUs**
- CPU fallback only where a model declares it and the user picks it
- User-facing explanation for every resource conflict

## B.4 — Required benchmark (before any speed claim)

Identical model + params vs subprocess baseline, measuring: first-gen
latency, subsequent-gen latency, model-load overhead, idle VRAM, in-flight
VRAM, worker reuse rate, cancel latency, crash-recovery time. Median of
≥5 runs, same methodology as the Phase-4 CUDA/Vulkan benchmark.

## B.5 — UI

Automatic management by default (beginner-safe). Advanced settings expose:
persistent-worker toggle, idle unload timeout, max loaded models, max
workers per GPU, GPU memory budget, per-worker status/usage. Show model
state: loaded / loading / unloading / waiting-for-resources.

## B.6 — Compatibility gates

Must not break: existing generation, history, provenance, project
isolation, cancellation, storyboard thumbnails, backup/restore, automatic
backend selection. Failure in persistent mode → safe recovery to the
subprocess path.

## Open questions for the feasibility pass

1. Does sd.cpp (or a maintained fork) expose server/batch mode on Windows?
2. What's the real warm-request latency when weights stay resident —
   the ~7.8 s warm figure is mostly sampling time; how much is actually
   per-request overhead vs load?
3. Does keeping a worker resident starve other jobs' VRAM fit checks
   (residency must integrate with the scheduler, not bypass it)?
