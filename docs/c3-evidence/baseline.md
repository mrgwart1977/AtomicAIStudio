# C3 performance baseline + before/after

Methodology: `backend/scripts/c3_baseline.py` — in-process TestClient
(same stack as the suite), median of 3 runs, temporary data dir.
Numbers are methodology-comparable, not production-absolute.
Host: Windows, Python 3.14, dev machine.

| measurement | median(3) | note |
|---|---|---|
| app create (in-process) | 0.512s | — |
| timeline detail, 250 clips | 0.019s | GET /timelines/{id} |
| EDL v2 serialize, 250 clips | 0.036s | GET .../edl?version=2 |
| timeline detail, 1000 clips | 0.049s | same endpoint, 4x data |
| EDL v2 serialize, 1000 clips | 0.137s | same endpoint, 4x data |
| assets list page | 0.005s | GET /assets |
| jobs list | 0.005s | GET /jobs |
| gpu sched sim | see test_a7_scheduling | reservation path unchanged |

Linear scaling holds for timeline detail + EDL serialization
(~3.8x for 4x data) — the ratio-based guards in test_n1e_perf.py
already enforce near-linear behavior without absolute-time flakes.

## Before/after — stage planning hot path

`stage_windows` originally rebuilt the active set by scanning **all**
events at **every** boundary per stage (O(bounds × events × stages)):

| | before | after |
|---|---|---|
| 1500 unique sources, 8 windows | **1.566s** | **0.058s** |

The optimized pass keeps a start-sorted event pointer and a monotonic
active set — O(bounds + events) per stage. Covered by the same 13
staged-export tests (13/13 pass after the change).

## Export ceiling — before/after

| unique sources | before | after (staged) |
|---|---|---|
| ~380–750 (path-length dependent) | `CreateProcess` argv overflow — **export impossible** | staged: bounded argv, exports fine |
| 1500 | impossible | 8 stages, max stage argv ~3.5k chars |

## Remaining limitations

- `copy` path already scales (concat-demuxer list, no per-source `-i`);
  only `encode` and `composite` needed the stage architecture.
- The **encode (v1) path** is still single-pass — it shares the argv
  bound; a v1 timeline with >~400 unique sources would hit the same
  wall (v1 timelines are single-track sequential — large multitrack
  projects land on the composite path). Follow-up: apply the same
  windowed intermediate approach to `build_encode_argv` if v1
  timelines at that scale become common.
- >200 **simultaneously overlapping** unique sources at one timeline
  position still refuse (documented `stage capacity` error) — a real
  compositing limit, not an argv artifact.
- Intermediates hold up to 2 windows of FFV1+PCM on disk
  transiently (bounded, deleted in `finally`).
- Timeline **frontend** rendering/scroll profiling is unchanged — the
  measured bottlenecks were backend serialization, which scales
  linearly.
