# C3-E2 — Staged submix architecture

## Pipeline

```
plan ──► stage_windows(plan, paths, STAGE_MAX_INPUTS=200)
         │  greedy tl-frame windows; unique sources ≤ 200 each
         ▼
       for each window [F,M):
         stage_subplan(plan, F, M)  ──► rebase timeline anchors
         build_stage_argv           ──► composite graph, NO limiter
         ffmpeg → stage{i}.mkv      ──► ffv1 video + pcm_f32le audio
         ▼
       concat_demuxer(list.txt)  ──► file list, argv stays bounded
       build_final_argv          ──► [0:a]alimiter→aac + h264
       ffmpeg → out.mp4          ──► ONE limiter pass on full program
```

## Why the math preserves semantics

Every envelope anchor in the composite graph is **stream-local in
source units** (atrim/afade/automation positions are counted from the
clip's stream start after `asetpts=PTS-STARTPTS`). `afade` cannot
express a negative `start_sample`, so a fade or crossfade that STARTS
before the stage window cannot be re-anchored by subtraction.

The exact fix: **the stream keeps its full original segment and every
stream-local anchor untouched** inside a stage. `stage_subplan` rebases
only timeline-anchored fields (start_tl, occupancy, tl_start_s,
absolute `track_auto.at_tl` by −F) and attaches `stage_skip_src` /
`stage_end_src`. `_lane_chain` then runs atrim→asetpts→aformat→aeval
automation→afade envelopes on the ORIGINAL stream — identical envelope
state to the single-pass render — and a **post-envelope
`atrim=start_sample…end_sample` cuts the stage window out** of the
rendered stream. Envelopes crossing boundaries (fade mid-curve, xf
window split at the cut) continue exactly; no negative anchors exist.

- clip-local automation (`off_tl` relative to occupancy start) and
  `head_tl`-derived `aeval` offsets stay in original stream units — the
  aeval `n` index counts the untrimmed stream, so positions are
  already correct.
- `extent_src` is **not** rewritten — fade_out's anchor derives from it
  and must not migrate when the tail is cut off-stage.
- fractional timeline edges (non-integer fps, e.g. 29.97 → a clip
  ending at frame 119.88) snap **up** in `stage_windows` — a clip
  starting at 119.88 first shows in frame 120 and one ending there
  still contributes frame 119 — so stage bounds are always whole
  frames and tile the program exactly.

`lead > 0` clamp in `build_composite_argv`: a clip whose stream begins
before the window reports a negative lead — the post-envelope atrim
already cut it — so it plays from stage position 0 instead of emitting
an invalid negative `atrim`.

## Boundedness guarantees

| resource | bound |
|---|---|
| argv bytes | ≤ ~4k chars (200 inputs × ~20-char alias + fixed flags) |
| file handles | ≤ 200 simultaneous decoders per ffmpeg process |
| ffmpeg memory | ≤ 200 decoder instances per stage; intermediates closed between stages |
| temp disk | stages render sequentially — peak = 2 intermediates + list; all removed in `finally` |
| encode loss | none — FFV1 (lossless video) + pcm_f32le (lossless audio) intermediates |

`STAGE_MAX_INPUTS` is read from `ADE_STAGE_MAX_INPUTS` at call time —
installed builds exercise the staged path by launching the app with
that variable set small.

## Bit-exactness boundary

Identical to single-pass **except** at stage boundaries when a clip
crosses a window edge: the audio post-trim can round `skip`/`take` to a
sample differently than the continuous stream by **≤ 1 sample** at
sample-exact rates (documented `_q` rounding; e.g. 29.97 fps → 1601.6
samples/frame). Tolerance in the PCM parity tests: peak < 0.05, RMS <
0.005 — far above any boundary rounding.

The **limiter is applied once**, in the final concat pass, on the whole
program — identical state machine to the single-pass render.

## Legacy encode path (C3-F6)

- `ade-edl/2` multitrack → composite → staged when >200 unique sources.
- `ade-edl/1` single-lane → `copy` (concat list file — argv always
  bounded) or single-pass `encode` (one `-i` per clip). The encode
  path measures its actual argv after alias substitution and **refuses
  preflight** when >30,000 chars with an actionable message (split the
  range or rebuild multitrack). Users cannot unknowingly trip
  `CreateProcess` — the refusal fires before any render.
- `copy` stays byte-exact stream copy; semantics unchanged.

## Failure recovery (C3-E4)

- stage ffmpeg failure → `ValueError("stage N render failed: …")`,
  intermediates unlinked in `finally`, nothing published
- cancel before first stage / between stages → `check_cancel`/
  `JobCancelled`, intermediates + list unlinked
- final concat failure → existing output probe/delete path; partial
  `out.mp4` removed before CAS registration
- missing source (`_source_path`) / corrupt source (pinned-sha
  `_verify_bytes`) → refuse before any ffmpeg invocation
- worker crash/restart → job row + `interm` only inside work dir,
  cleaned on reconcile
