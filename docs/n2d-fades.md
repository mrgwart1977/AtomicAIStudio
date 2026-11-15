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
- the pair must be ordered out→in, and the window must lie entirely
  within both clips' coverage (`win_start ≥ out.start_frames`,
  `win_end ≤ in_clip's span end`) — an interval where only one clip
  could sound is not a crossfade and refuses.

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

## Boundaries

- Fades/crossfades/automation are **not rendered** — N2-E mixes and
  applies them; until then visible populated audio lanes refuse
  export.
- No on-curve dragging of automation points this round — the point
  list is the bounded editing surface.
- Human checks outstanding: fade/curve UX feel **[human]**;
  crossfade audition **[N2-E + human]**.
