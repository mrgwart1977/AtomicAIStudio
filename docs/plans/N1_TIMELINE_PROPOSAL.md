# N1 — advanced timeline (implementation proposal)

Status: **proposal only — no implementation authorized**.
Precondition: S-C5 merged. Builds on C5-A (assembly), C5-B (export),
C5-D (First Film), C5-E (compatibility). Extends `docs/handoff/` v0.9
— nothing is removed.

## Current state (verified in S-C5)

- `Timeline` (id, project_id, name, fps_num/den, status, revision)
- `TimelineClip` — dense integer `position` (sequence index), pinned
  `source_asset_id`/`source_version_id`/`source_sha256`, integer source
  frames `in_frame`/`out_frame`, pinned `src_frames`/`src_fps_*`/
  `src_duration_s`, `shot_id`/`scene_id` provenance
- Revisions: `EntityRevision` snapshots embed the full ordered clip list
- Commands: `timeline.{create,update,archive,unarchive,duplicate,revert,
  export}`, `timeline.clip.{insert,remove,move,trim}` — all via
  preview → commit → audit with `expected_timeline` stale guards
- EDL: `ade-edl/1` — ordered clip list, integer frames, single implicit
  track, timebase-declared
- Export: C5-B engine, stream-copy on uniform untrimmed paths, re-encode
  otherwise; durable ops-lane job; output registers as rendered asset

**Gaps vs N1 scope:** no tracks, no clip start-times (position is a
sequence index — overlaps and gaps are unrepresentable), no split, no
ripple semantics, no undo/redo beyond full-snapshot revert, no zoom/
navigation model beyond "list of clips".

## 9. Timeline architecture

### 9.1 Schema evolution — extend, never parallel

One additive migration (`0008_n1`). The same `timelines` /
`timeline_clips` tables grow columns; no new timeline database, no new
entity families for tracks-as-projects.

**New table `timeline_tracks`:**
`id, project_id, timeline_id, kind ("video"|"audio"|"overlay"),
position (stack order, 0 = bottom), name, visible (bool), locked (bool),
muted (bool), created_at`.

**`timeline_clips` grows:**
`track_id (FK→timeline_tracks)`, `start_frames (int, timeline-time
position)`, `speed_num/den (default 1/1 — reserved, not required for
N1-A..E)`, `transition_in_json/transition_out_json (nullable —
N3-era, declared now to avoid a second migration)`.

`position` is retained but redefined as **within-track order** for
backward compat; `start_frames` is authoritative for timeline time.
Existing rows migrate as: one video track `V1` (position 0, visible,
unlocked), `track_id=V1`, `start_frames` = cumulative duration of
prior clips in `position` order — today's sequential semantics are a
lossless special case of the new model.

**Invariants (enforced in domain, not just UI):**
- `start_frames >= 0`; clips on the same track may overlap only if a
  transition declares it — otherwise insert/move resolves by
  split-or-shift per the edit's ripple flag
- `locked` tracks refuse mutations at the command layer (422, not a
  silent no-op)
- `visible=false` video tracks and `muted=true` audio tracks are
  dropped from export *render* but kept in the EDL (edit decision vs
  render decision separation)

### 9.2 Time model — frame-accurate positioning

All timeline time is integer frames at the timeline's declared
`fps_num/den` — the same contract trims already use for source frames.
Clip timeline extent = `out_frame - in_frame` (source frames are
measured at clip fps; placement is timeline fps — conversion happens at
insert/EDL emit via `Fraction`, never floats). Gaps are represented
explicitly: EDL emits `gap` entries rather than synthetic clips.

### 9.3 Commands (all through the existing command system)

New command types (additions to the existing enum — audit, revisions,
idempotency, `expected_timeline` pinning all inherited):

- `timeline.track.{add,rename,reorder,set_flags,remove}` —
  set_flags covers visible/locked/muted atomically
- `timeline.clip.place` — insert-with-`start_frames` (replaces
  position-only insert; old `insert` keeps working as
  "place at end of V1")
- `timeline.clip.move` — extended: `{clip_id, to_track_id?,
  start_frames?, ripple: bool}` — legacy `to_index` still accepted
- `timeline.clip.split` — `{clip_id, at_frame}` → two clips sharing
  pinned source geometry (`a.in→split`, `b.split→out`)
- `timeline.clip.trim` — unchanged semantics; `ripple: bool` added:
  ripple trims shift subsequent clips on the same track
- `timeline.gap.{close}` — ripple-delete a gap span on a track
- `timeline.undo` / `timeline.redo` — see §9.4

`timeline.revert` (full-snapshot restore) stays as the floor of history.

### 9.4 Undo/redo — command-ledger, not snapshots

Snapshot revert already exists; N1 adds per-step undo by **inverse
command replay**, consistent with the command-first architecture:

- Every mutating command already writes an `AuditEvent` + a timeline
  `EntityRevision`. `timeline.undo` commits a meta-command that
  restores the previous *edit decision* — implemented as revert-to
  `revision-1` scoped to the timeline, but recorded as a NEW revision
  (never history rewriting). Redo replays forward.
- Per-timeline undo depth: bounded (e.g. 100 steps) — snapshots are
  small (clip-list JSON); memory stays bounded.
- Undo scope is per-timeline (not global project undo) — global undo
  is a separate future design, don't conflate.

### 9.5 UI model

- Track headers: name, V-lock/mute/S-solo toggles, add/remove track
- Clip positioning: drag on a common ruler; snapping (clip edges,
  playhead, gaps); frame-accurate nudge (arrows = 1f, shift = 10f)
- Zoom: px-per-frame scale around playhead anchor; minimap optional
- Ripple mode toggle (explicit per-operation flag in commands, UI
  chooses default)
- Zustand: select stable fields only — derive clip layout outside
  selectors (existing rule applies doubly to track×clip matrices)
- First Film wizard keeps using the single-track happy path — N1 must
  not complicate the beginner flow

## 10. Export compatibility

### 10.1 EDL evolution — `ade-edl/2`

```
format: "ade-edl/2"          # ade-edl/1 readers must cleanly refuse
timebase: {num, den}          # unchanged semantics
tracks: [ {id, kind, position, visible, locked, muted, name} ]
clips:  [ {id, track_id, start_frames, src:{asset,version,sha256,frames,fps},
           in_frame, out_frame, transitions?} ]   # timeline-time ordered
gaps:   [ {track_id, start_frames, duration_frames} ]  # explicit
```

- **Backward compat:** `/timelines/{id}/edl` gains `?version=1|2`.
  Version 1 output is still generated for single-visible-video-track
  timelines by collapsing to sequence order (current semantics). When a
  timeline has >1 visible video track or explicit gaps, `version=1`
  returns 409 `edl_v1_incompatible` rather than silently flattening —
  honest refusal, matching the "no silent treatment" rule.
- Export preview pins `edl_format` + hash exactly as today; committing
  a v1 export against a multitrack timeline refuses at preview AND at
  commit (stale-format is a stale-approval case).

### 10.2 Render semantics

- **Multi-video-track:** composite bottom-up in `position` order;
  overlay tracks need alpha/blend ops — ffmpeg `overlay` filter graph
  generated per timeline-time segment. Overlaps on the same track are
  resolved pre-render by the edit ops (never arrives at export
  ambiguous); cross-track overlaps are the compositing case.
- **Gaps:** emit black/silence segments via `color`/`anullsrc` sources
  — duration-exact, never "skip to next clip".
- **FPS normalization:** clips keep pinned `src_fps`; export resamples
  each to timeline fps (`fps` filter + trim on output frames) —
  identical rule to today's stream-copy/re-encode decision, generalized:
  stream-copy only when every rendered segment is a uniform unmodified
  span.
- **Audio:** `kind="audio"` tracks mix via `amix`; `muted` dropped at
  render, kept in EDL. N1 scope is clip-embedded audio only — detached
  audio assets are N2.
- **Durable jobs:** same ops-lane job machinery; `request_json` carries
  `edl_format`. Provenance records track structure + EDL hash on the
  output version — a v2 export of the same sources is distinguishable
  from v1.
- **Cancellation/interruption:** identical contract verified in S-C5 —
  interrupted exports fail honestly, no partial asset.

### 10.3 What N1 deliberately does not do

- No transitions implementation (schema fields declared; engine N3)
- No detached audio tracks/assets (N2)
- No nested timelines/compound clips
- No keyframed effects, speed ramps (`speed_num/den` reserved)

## 11. PR boundaries

| PR | Scope | Depends | Risks | Tests | Size |
|----|-------|---------|-------|-------|------|
| **N1-A** | Migration `0008`: `timeline_tracks`, clip `track_id`+`start_frames`, backfill V1; domain invariants (track ops, placement model); `timeline.track.*` + `clip.place` commands; EDL v2 *read* path alongside v1 | S-C5 | Migration correctness on existing projects; backfill fidelity | migration up/down on real fixtures; backfill = sequential special-case proof; track-flag refusal tests; dual-EDL emit | M |
| **N1-B** | Multitrack editor UI foundation: track headers, ruler, clip blocks at `start_frames`, zoom/navigation, drag-with-snapping; First Film untouched | N1-A | Layout perf on long timelines; selector churn | component tests (jsdom layout math), CDP acceptance additions, a11y pass | M–L |
| **N1-C** | Split, ripple/non-ripple move+trim, gap.close, keyboard nudge; overlap resolution rules | N1-B | Ripple edge-cases (multi-clip cascades); split at frame 0/len | property-style tests: split∘merge = identity; ripple duration conservation; boundary refusals | M |
| **N1-D** | Export v2: filter-graph compositing, gap fill, fps normalization, audio mix; v1 compat + refusal path; provenance extension | N1-C | Filter-graph correctness; render cost on many tracks | golden-EDL renders (ffprobe frame counts), v1-refusal test, audio-mix fixture, provenance assertions | M–L |
| **N1-E** | `timeline.undo`/`redo` command-ledger; undo UI (Ctrl+Z); bounded history; perf hardening (incremental revision snapshots if profiling demands) | N1-B (C for full coverage) | Undo semantics across ripple/split correctness; snapshot memory | undo/redo round-trip suite, bounded-depth test, memory bound, restore-vs-replay equivalence | S–M |

**Sequencing:** strictly A→B→C→D→E for correctness (each builds schema
or UX the next needs). B and C could overlap if C's command tests land
first, but UI split/trim needs B's layout model — keep sequential for
reviewability.

**Migration strategy:** single additive migration; old installs read
new DBs? — No: `alembic_version` guard already fail-closes older builds
(existing behavior, documented in RELEASE_CHECKLIST #7 upgrade matrix).
Rollback = downgrade migration that refolds V1 clips into `position`
order and drops tracks — lossy only for genuinely-multitrack edits,
which v1 couldn't represent anyway.

## Safety boundaries (unchanged)

No live provider transport, no model downloads, no inference, no paid
activity. N1 is local editing infrastructure — mock/simulated paths
only until separately authorized.
