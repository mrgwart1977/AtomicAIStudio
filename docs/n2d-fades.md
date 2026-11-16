# N2-D — fades, crossfades, and bounded gain automation

Fades, crossfade records, and gain keyframes as durable timeline
metadata. **N2-D defines and persists the model only** — nothing is
rendered into audio until N2-E, and populated visible audio lanes
still refuse export explicitly.

## Fades (per audio clip)

| Field | Column | Default | Meaning |
|---|---|---|---|
| `fade_in_units` | `timeline_clips` | `0` | fade-in length in SOURCE units (samples) |
| `fade_in_curve` | `timeline_clips` | `linear` | `linear` \| `equal_power` |
| `fade_out_units` | `timeline_clips` | `0` | fade-out length in source units |
| `fade_out_curve` | `timeline_clips` | `linear` | same vocabulary |

- **Source units, edge-anchored.** Fade-in covers
  `[in_frame, in_frame+units)`; fade-out the last `units` before
  `out_frame`. Trims move the anchors — a fade never references
  source outside the trim.
- **Bounds:** each ≥ 0; `fade_in + fade_out ≤ clip extent`.
  `timeline.clip.fade` accepts partial fields (set one side without
  touching the other). Audio-lane clips only.
- **Curves.** `linear`: `g(t)=t/n` in, `1−t/n` out. `equal_power`:
  `g_in=sin(πt/2n)`, `g_out=cos(πt/2n)` — a paired crossfade sums to
  unity **power** (sin²+cos²=1) instead of a mid-fade dip.
- **Splits** reattach fades to edges: the left fragment keeps the IN
  fade, the right keeps the OUT fade. A split inside a fade region
  refuses. **Trims** that would leave less room than the fade sum
  refuse.

## Crossfades (`timeline_crossfades`)

An **explicit overlap record** — `(out_clip_id, in_clip_id,
tl_start, tl_frames, curve)` joining two clips on the same audio
lane. Lanes still refuse grid overlap; the window borrows **source
handles** at render time:

- out-clip extends `win_end − out_occupancy_end` timeline frames
  into its **tail handle** (requires `out_frame + tail_src ≤ src_frames`)
- in-clip reaches `in_start − win_start` into its **head handle**
  (requires `in_frame − head_src ≥ 0`)
- the pair must be ordered out→in, and the window must **contain
  the seam** `[out_occupancy_end, in_occupancy_start]` while staying
  inside the pair's combined reach: `out.start ≤ win_start ≤
  in.start` and `out.end ≤ win_end ≤ in.end`. A window buried in
  one clip's body is refused — the in-clip would already be playing
  natively when its fade-in asks for zero (a dip), or the out-clip's
  occupancy would keep sounding after its fade "completed".

**`satisfied` is computed at read time** (detail API + EDL), not
stored — a move that detaches the window reports `satisfied: false`
honestly instead of silently rendering wrong or dropping the record.
Trims that would break a window's handles refuse; splits inside the
window refuse; undo/redo restores the full record set (ids stable).
Duplicate is rebound to the copied clip ids.

## Gain automation (bounded)

`timeline_clips.automation_json` — a keyframe list
`[{id, at_frames, gain_db}]`, ≤64 points:

- `at_frames` is a **clip-local** integer timeline-frame offset
  (0 … ceil(extent)−1) — moves never drift it; splits rebase it;
  trims that would orphan points refuse.
- Unique positions, `gain_db` bounds as clip gain (−60…+12).
- **Interpolation: linear in dB** between neighbors, flat before the
  first and after the last point (`automation_gain_db`).
- Replace-all command `timeline.clip.automation` — one commit, one
  undo step; missing `id`s are assigned `p{i}` at write, so the
  stored document is deterministic.

Deferred (reported, not silently dropped): per-track automation,
nonlinear interpolations, fade-rate curves beyond the two pinned.

## Commands

| Command | Payload | Effect |
|---|---|---|
| `timeline.clip.fade` | `clip_id` + any fade field | sets fades (validated against extent) |
| `timeline.clip.automation` | `clip_id`, `points[]` | replaces keyframes |
| `timeline.crossfade.set` | pair ids, `tl_start`, `tl_frames`, `curve?` | creates/updates the pair's record |
| `timeline.crossfade.clear` | `crossfade_id` | removes a record |

All preview/commit/audit/expected-revision/idempotent; snapshots
carry fade/automation columns and the crossfade set, so undo/redo is
exact. Migration `0016_n2d` is additive and idempotent.

## EDL v2

- clip entries: `fades: {in: {units, curve}, out: {…}}` and
  `automation: [...]`
- top-level `crossfades: [{id, out_clip_id, in_clip_id, tl_start,
  tl_frames, curve, satisfied}]` — ordered by window start, with
  read-time feasibility.
- v1 unchanged; video-only v2 gains inert empty fields only.

## UI

- Audio clips render shaded fade wedges at head/tail (clip-path
  triangles) sized by the fade's timeline footprint, each with a
  **draggable apex handle** — one `timeline.clip.fade` commit per
  drag (one undo step), Escape cancels.
- Clips in a crossfade carry an `⇄` badge (red when `satisfied` is
  false — the window detached after a move).
- Inspector (audio clips): fade duration fields (seconds → source
  units) + curve selects, a crossfade row (status + Clear, or
  "Crossfade with next clip" creating a 12f equal-power window —
  the backend reports handle shortages plainly), and the gain
  automation point list (edit/delete/add, whole-list commits).

## N2-E effective-gain contract (pinned by the N2-D review)

The mixer applies these multipliers **in the linear domain, in this
order**, for every audio-lane clip and for embedded audio from
video-lane clips:

```
g_clip(t) = clip_gain_lin                          (10^(gain_db/20))
          × automation_lin(t)                      (10^(interp_db/20))
          × fade_env(t)                            (edge-anchored)
          × xf_env(t)                              (see below)
sample_out(t) = g_clip(t) × src_sample(t)
```

Per lane: `lane_out = track_gain_lin × Σ g_clip(t)·src(t)`;
per mix: `mix = Σ lane_out` over audible lanes only.

- **Processing order is fixed** — clip gain, then automation, then
  clip fades, then the crossfade envelope. Each is a multiplier, so
  none is applied twice; the envelope product is computed once per
  output sample.
- **Audibility precedence** (unchanged from N2-C):
  `visible=false` → lane contributes nothing; `muted=true` → lane
  silent; non-empty solo set → non-solo lanes silent; multiple solos
  all audible; empty solo set → mute is the only silencer. Video
  lanes are members of the audio solo set (embedded audio is a
  lane-level decision) — solo never affects picture.
- **Fade envelope** (`fade_env`): outside a clip's fade regions = 1;
  in fade-in region `g_in(t)`; in fade-out region `g_out(t)`;
  `n=0` ⇒ 1 (zero-duration fades are unity, not silence).
- **Crossfade envelope** (`xf_env`): for the pair's window [S,E)
  that *contains the seam* — out-clip: unity before S, `1−c(t)` on
  [S,E), 0 after E (its stream extends into the tail handle only as
  far as E); in-clip: absent before S (its extended stream starts
  at S), `c(t)` on [S,E), unity on [E, span_end). `c` is the
  record's curve evaluated over the window; for `equal_power`,
  `1−c = cos(πt/2)`, `c = sin(πt/2)` — constant-power transition.
- **Clip gain vs. fades vs. automation never double-apply**: clip
  gain is the clip's static level, automation rides it in dB, fades
  and crossfades shape the edges/junctions — all multiplicative in
  linear domain.
- **`satisfied=false` records never render.** Export refuses
  closed with a clear error when a populated audio lane carries a
  detached window — there is no "safe alternative" interpretation:
  the record says the two clips must overlap over [S,E), and if
  they no longer can, rendering anything would be wrong.
- **Quantization**: timeline-frame positions convert to 48 kHz
  samples as `samples = round(frames × 48000 × fps_den / fps_num)`
  (round-half-up on exact rationals) — the only quantization point;
  everything upstream stays in `Fraction` arithmetic.
- **Output contract**: stereo, 48 kHz, bounded duration
  (timeline extent), hard-limited at −1 dBFS.

## Boundaries

- Fades/crossfades/automation **are rendered** as of N2-E — see
  `docs/n2e-mixing.md` for the render architecture (envelopes at
  native rate, equal_power → afade `qsin`, single quantization
  point).
- No on-curve dragging of automation points this round — the point
  list is the bounded editing surface.
- Human checks outstanding: fade/curve UX feel **[human]**;
  crossfade audition **[human — now audible after N2-E]**.
