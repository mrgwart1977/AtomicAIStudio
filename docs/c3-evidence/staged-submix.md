# C3-E2 — Staged submix architecture

## Pipeline

```
plan ──► stage_windows(plan, paths, STAGE_MAX_INPUTS=200)
         │  greedy tl-frame windows; unique sources ≤ 200 each
         ▼
       for each window [F,M):
         stage_subplan(plan, F, M)  ──► rebase all anchors
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
clip's stream start after `asetpts=PTS-STARTPTS`). A stage window [F,M)
therefore only needs each clip's **trimmed prefix** removed:

- timeline-anchored fields shift by `−F` (start_tl, occupancy frames,
  tl_start_s, absolute `track_auto.at_tl`);
- stream-anchored fields shift by `−skip` (seg_*_src source positions,
  head_src/head_tl borrows, xf offsets) where `skip = max(0, F − stream0)`;
- `extent_src` is **not** rewritten — fade_out's anchor derives from it
  and must not migrate when the tail is cut off-stage;
- clip-local automation (`off_tl` relative to occupancy start) needs no
  shift since occupancy start is also rebased by −F.

`lead > 0` clamp in `build_composite_argv`: a clip whose stream begins
before the window reports a negative lead — already trimmed at the
boundary — so it plays from stage position 0 instead of emitting an
invalid negative `atrim`.

## Boundedness guarantees

| resource | bound |
|---|---|
| argv bytes | ≤ ~4k chars (200 inputs × ~20-char alias + fixed flags) |
| file handles | ≤ 200 simultaneous decoders per ffmpeg process |
| ffmpeg memory | ≤ 200 decoder instances per stage; intermediates closed between stages |
| temp disk | stages render sequentially — peak = 2 intermediates + list; all removed in `finally` |
| encode loss | none — FFV1 (lossless video) + pcm_f32le (lossless audio) intermediates |

## Bit-exactness boundary

Identical to single-pass **except** at stage boundaries when a clip
crosses a window edge: the audio re-anchor can round `skip`/`take` to a
sample differently than the continuous stream by **≤ 1 sample** at
sample-exact rates (documented `_q` rounding; e.g. 29.97 fps → 1601.6
samples/frame). Tolerance in the PCM parity test: peak < 0.05, RMS <
0.005 — far above any boundary rounding.

The **limiter is applied once**, in the final concat pass, on the whole
program — identical state machine to the single-pass render.

## Failure recovery (C3-E4)

- stage ffmpeg failure → `ValueError("stage N render failed: …")`,
  intermediates unlinked in `finally`, nothing published
- cancel between/inside stages → `check_cancel`/`JobCancelled`,
  intermediates + list unlinked
- final concat failure → existing output probe/delete path; partial
  `out.mp4` removed before CAS registration
- worker crash/restart → job row + `interm` only inside work dir,
  cleaned on reconcile
