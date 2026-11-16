# N3-B — Continuous audio scrubbing

Extends the N2-F `AudioPreviewEngine` (WebAudio) — no parallel
scheduling path, no new decode pipeline, no render-chain changes.

## Interaction model

- **Pointer down on the ruler** → `beginScrub()` + window-level
  `pointermove`/`pointerup`/`pointercancel` listeners (the same
  pattern as clip drags — pointer capture is deliberately *not* used:
  in WebView2 a scrub gesture can be turned into a native
  drag-and-drop (`dragstart`), which cancels the pointer and loses
  capture mid-drag, wedging scrub state; the ruler sets
  `user-select`/`user-drag: none` + a `dragstart` guard as defense).
  If the engine was playing, playback pauses and is remembered; if it
  was never loaded, a lazy `load()` runs in the background and
  auditions start as soon as buffers exist.
- **Each pointer move** → `scrubAudition(frame/fps)` → a *bounded
  audition window*: for every plan clip intersecting
  `[s, s + SCRUB_WINDOW_S)` (350 ms), one `AudioBufferSourceNode` +
  one `GainNode` scheduled for at most the window length, with the
  same single `setValueCurveAtTime` envelope application used by
  playback, offline parity renders, and the FFmpeg render. Playhead
  state updates are rAF-gated (≤1 React render per frame).
- **Pointer release** → `endScrub()`: every live source is stopped
  immediately (no stale audio), then playback resumes from the final
  position if the scrub interrupted it (smooth scrub→play).
- **Escape or window blur** → `cancelScrub()` — the documented
  deterministic cancel: the playhead returns to the position it had
  when the gesture began, and the transport returns to its pre-scrub
  state (a playing transport resumes *from the pre-scrub position*; a
  stopped one stays stopped). No accidental playback activation.
- **Click or arrow key** (ruler has focus; arrows ±1 frame,
  Shift ±1 s, Home/End jump) → `auditionOnce()`: one bounded window
  without entering scrub mode.

### Transport-state contract (one authoritative state)

`engine.state` is the single authoritative transport state:

| before      | gesture        | after                                    |
|-------------|----------------|------------------------------------------|
| stopped     | scrub → release| stopped at release position (`ready`)    |
| paused      | scrub → release| paused at release position (`ready`)     |
| playing     | scrub → release| `playing`, resumed from release position |
| playing     | scrub → cancel | `playing`, resumed from scrub-start pos  |
| stopped     | scrub → cancel | `ready` at the scrub-start position      |
| any         | project switch / unmount / close | `dispose()` — nodes stopped, ctx closed |

`play()` and `auditionOnce()` are ignored while `scrubActive` — the
drag owns the transport; there is exactly one state.

## Bounds — why this is not an unbounded source sequence

- Per audition generation, scheduled sources ≤ number of plan clips
  in the window (`clipsInWindow`, half-open `[s, s+W)` — a clip ending
  exactly at `s` or starting exactly at `s+W` contributes nothing).
- Pointer streams faster than `SCRUB_MIN_INTERVAL_MS` (45 ms) coalesce
  to the *latest* position via a single pending timer — a 60-move
  drag produces far fewer auditions than calls.
- Every fired generation calls `stopLive()` first: the previous
  generation's sources are stopped before the new one schedules. Live
  nodes stay ≤ ~2× the windowed clip count (old gen draining + new
  gen), and sources self-terminate at window end regardless.
- `endScrub()`/`dispose()` clear the pending timer and invalidate the
  generation counter — a timer fired after release/decode-race is a
  no-op.
- Gaps schedule zero sources (silence); past program end the window
  is empty. Very short clips just get a shorter `dur` — no special
  case.

## Timing model

Timeline→source mapping is unchanged: `env(tS)` evaluates at
*stream-local* seconds built from the rational frame model; scrub
only moves `fromS`. The window's duration cap is
`min(segRemaining, bufferRemaining, windowS − clipLead)`, so a clip
joining mid-window gets exactly its remaining share. 30000/1001 and
24000/1001 land on fractional seconds the same way playback does —
there is no separate scrub clock.

## Latency contract (measured, not claimed)

`scrubStats` exposes bounded per-phase sample rings plus counters;
`latencyReport()` returns p50/p95/max/n for each measurable phase:

| phase | ring | what it measures |
|-------|------|------------------|
| pointer input → scrub handler | `inputMs` | `event.timeStamp` → `scrubAudition` entry |
| handler → audio scheduling | `queueMs` | handler entry → `fireAudition` start (includes the ≤45 ms coalesce wait) |
| scheduling loop | `schedMs` | the scheduleClip loop itself |
| scheduling → expected onset | `leadMs` | `ctx.currentTime` → scheduled start (~20 ms lead) |

`latencyReport().estOutputLatencyMs` additionally reports the
AudioContext's own `outputLatency`/`baseLatency` estimate — the
expected-onset→audible proxy. **Audible latency is not objectively
measurable in this environment** (no loopback capture); it is
reported as an estimate and never conflated with scheduling latency.

Preview is still editorial — the N2-F tolerances (64 Hz envelope
sampling, decode/resample, AAC priming, limiter) apply;
sample-exactness is not claimed.

### Installed acceptance measurements (NSIS build, WebView2)

`scripts/cdp-n3b-acceptance.cjs` — **16/16 steps** on a real Windows
install (`docs/n3b-evidence/n3b-acceptance.txt`), final review head
`e3b97ac`, merged via PR #136 → **`9824526`**.

- **Layout (B1)**: Activity rail is a collapsible layout sibling —
  12 `elementFromPoint` probes across the visible ruler resolve to
  ruler elements with zero audit-panel hits, expanded (288 px),
  collapsed (32 px rail, ruler viewport +~250 px), at 900 px width
  and 1.5× device scale. Audit controls re-expand independently.
- **Real CDP pointer drag** 30f→85f ×20 moves: **20/20 delivered**,
  delivered→audition latency **p50 4–5 ms, p95 ≤ ~60 ms**.
- **Phase latency (B4, measured inside the engine)**: input→handler
  p50 0.9 ms / p95 ≤2.2 ms; handler→scheduling p50 0.5 ms /
  p95 ≤5.6 ms (coalesce wait); schedule loop p50 0.3–0.4 ms;
  scheduling→expected onset p50 20 ms (fixed lead).
  `estOutputLatencyMs` ≈ 40–48 ms is reported as the AudioContext's
  device estimate — **audible onset is not objectively measurable
  here (no loopback) and is never claimed**.
- **Transport matrix (B2)**: stopped→release stays stopped at the
  release position; paused→release stays paused; playing→release
  resumes at 5.23 s ≈ release 4.67 s + playback; **playing/stopped →
  Escape cancel restores the pre-scrub position AND transport**;
  dispose on project switch/unmount; clean close mid-scrub.
- **Lifecycle (B3)**: 24-move zigzag → bounded auditions, maxLive 4,
  live 0 after release; release *outside* the ruler still ends the
  scrub; gaps/gap windows schedule zero sources; tail-end auditions
  truncate to remaining duration; repeated cycles leave no nodes.
- **Rapid 60-move burst**: 61 pointer calls → 21 auditions (~34 %
  scheduled — throttle coalesces; no runaway sources).
- **Keyboard**: arrows ±1 frame (Shift ±1 s) and Home/End each fire
  one bounded audition blip.
- **fps variants**: clip at frame 48 onsets at the rational position
  on 24/1, 25/1, 30/1, **24000/1001 (2.002 s)**, **30000/1001
  (1.6016 s)** — same rational timing model as playback.
- **Mixed sample rates**: 44.1 kHz + 48 kHz sources; windowed offline
  renders match FFmpeg export PCM within N2-F editorial tolerances.
- **Long timeline**: 20 clips / 4 lanes / ~46 s — decode+build
  ~390–550 ms, 350 ms window render ~3 ms.
- **Perf during scrub**: ~+0.7 s process-CPU over a 2 s drag, app
  RSS ~28 MB, backend ~135 MB.
- **Restart persistence (B6)**: app closed mid-scrub leaves no
  orphans; relaunch preserves the timeline (5 clips), the satisfied
  crossfade, and 3-point clip automation; engine reloads clean.

### Known limits

- No **edge auto-scroll**: the playhead stops at the visible ruler
  edge; scroll first, then drag — same as other N2 editors.
- The **Activity rail is collapsible** (right side): when expanded it
  is a real layout sibling — the ruler's scroll viewport ends at its
  edge, nothing overlays it. Collapse it (or it auto-collapses under
  ~1200 px window width) for maximum ruler width. Persisted via
  `localStorage["atomic.activityCollapsed"]`.
- Audition windows are 350 ms editorial previews, not transport
  playback — constant loudness during a held position is by design.
- Decode/`AudioContext` failures surface as `state="error"` with the
  existing preview-error message; scrub stays inert.

## Errors and edges

- AudioContext construction failure → `engine.error` is set, the
  scrub stays inert (no unhandled rejection).
- A source that fails to fetch/decode during `load()` → `state =
  "error"` + the existing preview-error UI message.
- Scrub while `loading` → auditions no-op until buffers arrive.
- `play()`/`auditionOnce()` are ignored while a scrub is active —
  the drag owns the transport.
- `dispose()` (project switch, unmount, window teardown) clears
  pending timers and stops all nodes; window close mid-scrub is the
  app's normal quit path — no orphan processes.
