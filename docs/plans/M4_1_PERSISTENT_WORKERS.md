# M4.1 — Persistent Inference Workers & GPU Memory Management

**Status:** Phase 1 merged (PR #39). PHASE 2A IMPLEMENTED — real
sd-server integration, **no model inference**. Phase 2B (real weight
inference + benchmark) remains gated. Tracked as GitHub issue #23.

**Motivation (measured, not assumed):** every generation job today spawns a
fresh `sd-cli.exe` worker that reloads the ~2 GiB model — the observed
prime-vs-warm spread (24.2 s cold vs 7.75 s warm) is mostly model load +
process init. A persistent loaded-model worker could eliminate that on
repeat jobs — **if** the runtime supports it. No speed figure is promised
before measurement.

## Phase 1 — implemented (this branch)

Architecture (`app/domain/workers/`):

| Module | Role |
|---|---|
| `types.py` | `WorkerKey` (model, runtime, flavor, gpu, protocol) — a lease never crosses keys; `WorkerSpec`; `WorkerState` starting→ready→busy→idle→draining→dead |
| `protocol.py` | `StdioJsonLinesProtocol` (JSON-lines over pipes; non-JSON stdout = worker logs) + `SdServerHttpProtocol` (verified surface, see B.1) |
| `supervisor.py` | `SupervisedWorker`: one OS process under `proc_guard` Job-Object supervision; readiness timeout, heartbeat deadline, crash detect, graceful→forced shutdown, wait_dead that guarantees the process is actually reaped |
| `resources.py` | `ResidencyRegistry`: RAM+VRAM resident reservations, per-GPU accounting (VRAM never pools), fail-closed admission, headroom, pressure signal |
| `pool.py` | `WorkerPool`: keyed reuse, global+per-GPU caps, LRU idle eviction (timeout, pressure, admission), `WorkerUnavailable` → lane-level backpressure |
| `attempts.py` | Durable execution-attempt records appended to `generation_jobs.progress_json.attempts` — **no schema migration** (see §9) |
| `remote.py` | Mock remote-provider contract: `SubmissionIntent`, idempotent submit (dedupes on idempotency key), unknown-outcome → never silent resubmit, best-effort cancel |

Wiring: `JobEngine._execute` records an attempt before any work and
finishes it on every outcome (succeeded/failed/cancelled/interrupted/
worker_dead). `main.py` builds one `WorkerPool` inside the data-dir lock;
pool drains at shutdown before lock release — workers cannot outlive the
owning backend (belt: Job Objects; suspenders: lock + drain order).

Adapters: `PersistentMockAdapter` (registered as `mock.persistent-image`,
`adapter_kind="mock"`) prefers a pooled worker, falls back to a transient
supervised subprocess when the pool is absent/unavailable — the pre-M4.1
execution shape stays intact.

## Phase 2A — implemented (sd-server, no inference)

`SdCppServerAdapter` (`adapter_kind="local"`, `worker_model="persistent-pool"`)
registered only behind `ATOMIC_PERSISTENT_INFERENCE_ENABLED=1` — unvalidated
persistent inference is never selectable by default.

- Launch: `sd-server.exe -l 127.0.0.1 --listen-port <alloc> --model-dir <dir> --log-level warn` — loopback only; port allocation is TOCTOU so bind failure retries on a fresh port (≤3); admission refusals surface honestly.
- `SdServerHttpProtocol`: `GET /v1/models` readiness + active health probe (heartbeat-interval cadence); `POST /v1/images/generations`; stdout drained into a bounded log deque (sd-server logs per request — an unread pipe would deadlock it).
- Uncertain-outcome contract: transport failure / read timeout / abandoned-cancel → raise → supervisor marks dead → process killed → reservation released. A coherent HTTP response (even 500+JSON) is a real answer — worker survives and is reused.
- GPU identity: `WorkerKey.gpu_index` + `_device_env` pin (`GGML_VULKAN_DEVICE`/`CUDA_VISIBLE_DEVICES`); residency estimates = weight bytes + 2 GiB VRAM / max(4 GiB, weight) RAM — uncalibrated Phase 2A numbers.
- `GET /v1/models` → 200 with no weights (verified on real binary); a model-resident server is NOT assumed to fit — M4.0 preflight still gates every job.

## §9 — Proposed `0008_m4_1` migration (NOT applied — needs approval)

Phase 1 stores attempts inside `progress_json` (JSON list). If Phase 2
needs queryable worker rows (e.g. "which jobs ran on worker W"), a
dedicated table is cleaner:

```sql
CREATE TABLE worker_attempts (
  attempt_id   TEXT PRIMARY KEY,         -- uuid, stable per execution
  job_id       TEXT NOT NULL REFERENCES generation_jobs(id),
  worker_key   TEXT, worker_pid INTEGER,
  started_at   TEXT, ended_at   TEXT,
  outcome      TEXT                      -- succeeded|failed|cancelled|interrupted|worker_dead
);
```

Owner decision required before Phase 2 writes it.

## B.1 — Runtime feasibility investigation (gate)

**Verified on the pinned sd.cpp runtime (master-1073, `runtimes/sdcpp`):**

| Capability | Status |
|---|---|
| Persistent multi-request mode | ✅ `sd-server.exe` ships alongside `sd-cli.exe`; full generation flag surface (`--diffusion-model`, `--vae`, `-p`, `-W/-H`, `--steps`, seed, cfg…) |
| HTTP readiness/health | ✅ `GET /v1/models` → 200 without a model loaded (lazy `--model-dir` loading verified — server binds + serves before weights) |
| Generation endpoint | ✅ `POST /v1/images/generations` (+ A1111-style `/sdapi/v1/txt2img`) — flag surface present, exercised via HTTP stub only, NOT on real weights |
| Weights resident between requests | ✅ structural — server holds the loaded model across HTTP requests (the whole point of the mode) |
| Mid-request cancel | ❌ no cancel endpoint in this build — Phase 2 must define cancel = abandon + worker-recycle decision |
| Model unload/swap endpoint | ⚠️ not verified — `--model-dir` enables per-request model selection, hot-unload unproven |
| Crash recovery | ✅ by architecture — process death is detectable; pool respawns; Job Object owns the tree |

Phase 1 exercises the HTTP protocol class against an in-process stub and
the real binary's readiness only — **no inference run**.

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

## §5 — Crash recovery & job reconciliation (mandatory)

Every generation job persists enough state to reconcile after any crash:
job+project IDs, model/runtime versions, backend+GPU assignment, prompt/
seed/params/input refs, worker identity + **execution-attempt ID**, state
timestamps, output+temp refs, retry policy.

Handled cases: worker crash, app crash, power loss, GPU failure/OOM,
interrupted model load, interrupted output write, crash during asset
registration. On restart: reconcile persisted jobs vs real state; keep
validated outputs; quarantine invalid temp artifacts; release GPU
reservations; never duplicate asset registration; bounded safe retries
only; user-visible recovery options. No resume-from-interruption claims —
restart = fresh attempt unless the runtime documents checkpoints.

Crash-consistent state: durable transitions + stable attempt IDs; job
persisted before execution; completion recorded only after output
validation + commit; atomic file ops + transactions at the commit
boundary; attempts recorded separately; auditable history.

## §6 — Remote worker & cost protection (architecture-ready, not active)

Paid remote execution is NOT authorized — the schema and state machine
must be ready: provider identity, remote job ID, idempotency key,
submission intent+status, estimated/actual cost+currency, billing state,
cancellation state, retry authorization, reconciliation history.

Rules: reconcile before new submissions; query by remote ID; provider
idempotency keys; unknown outcome → `Needs Reconciliation`, NEVER
auto-resubmit; cancellation ≠ refund; preserve provider/billing evidence;
explicit approval before any billable retry; no second billable
submission may ever start silently (crash, timeout, double-click, retry).
No exactly-once claims where providers can't guarantee it.

## §7 — Required recovery tests

Worker crash · app crash · restart with unfinished jobs · GPU OOM ·
interrupted load · interrupted write · crash during asset registration ·
duplicate retry · remote disconnect post-submit · acceptance-then-crash
before remote ID · remote completion while offline · unknown billing
outcome · repeated reconciliation · cancel during recovery. Local
recovery real; remote via mock provider.

## §8 — Performance/UX

Persistent vs subprocess benchmark (same methodology as Phase 4):
first-gen latency, subsequent latency, reuse rate, idle/active VRAM,
cancel latency, recovery time, unload behavior. States: Loading Model /
Ready / Generating / Unloading / Recovering / Waiting for GPU.
Auto-managed default; advanced worker/memory controls.

## Open questions for the feasibility pass

1. ~~Does sd.cpp expose server/batch mode on Windows?~~ **Resolved:
   `sd-server.exe` is in the pinned build — see B.1.**
2. What's the real warm-request latency when weights stay resident —
   the ~7.8 s warm figure is mostly sampling time; how much is actually
   per-request overhead vs load? → Phase 2 benchmark (§B.4)
3. Does keeping a worker resident starve other jobs' VRAM fit checks
   (residency must integrate with the scheduler, not bypass it)?
   → Phase 1 answer: `ResidencyRegistry` charges resident workers; the
   scheduler's estimate-only path still applies per job on top.
4. sd-server cancel + hot-unload semantics — must be measured in
   Phase 2 before real-job cancellation policy is set.
