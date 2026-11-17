# Phase A — Integrated AI Filmmaking Workflow — proposed plan

Status: **proposal for review — not authorized, not implemented.**
Prepared after N3-D merge (`d29714c`). Grounded in `docs/handoff/` v0.9
(spec of record), especially `BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md`,
`CAMERA_FOLLOW_AND_TRACKING.md`, and `WORKFLOWS_MEMORY_AND_USABILITY.md`.

## 1. Goal

Take a beginner from "I have a story" to an exported film with a safe,
recoverable, project-scoped pipeline:

```
screenplay → scenes/shots (+cast, camera, dialogue)
  → per-shot keyframe images (character/world continuity)
  → per-shot motion clips (camera-follow modes)
  → dialogue/audio generation
  → timeline assembly → review/revise → export
```

Every step is a command (`preview → commit → audit`), every artifact a
versioned CAS asset, every generation a durable `generation_jobs` row,
every mutation undoable where the spec requires it.

## 2. What already exists (reuse, don't rebuild)

| Need | Existing component |
|---|---|
| Projects, isolation | `project.*` commands; `project_id` on every row |
| Character/world/location bibles | `bibles.py`, versioned `entity_revisions` |
| Scenes, shots, cast | `storyboard.py` (`Scene`, `Shot`, `SceneCharacter`) |
| Generation jobs | durable rows, idempotency keys, cancel events, restart reconciliation, resource admission |
| Provider plumbing | `providers/registry.py`, `catalog.py`, `credentials.py`; worker pool (`workers/pool.py`, `remote.py`), REST providers, simulated providers |
| Hardware/GPU | `hardware.py`, `gpu_sched.py`, `compatibility.py`, `compat_report.py`, `model_catalog.py`, `model_files.py` |
| Cost/safety gates | `cloud_cost.py`, `spend.py`, `live_gate.py`, `live_permit.py`, `image_safety.py`, `proc_guard.py` |
| Image edit | `image_edit.py` (inpaint/outpaint contracts) |
| Prompt building | `prompt_compose.py` (direct/world/hybrid modes) |
| Timeline + audio + export | `timeline.py`, `timeline_export.py`, editor UI (N1–N3) |
| Backup/recovery | `backup.py`, revisions, `.atomicpkg` |
| Preview parity method | `previewPlan.ts` + `renderPlanOffline` (N2-F/N3) |

## 3. What Phase A adds

1. **Beginner-friendly film creation** — a guided "new film" flow
   (template + a few questions) that produces a populated project:
   title, duration target, style/world, characters — all defaults
   revisable. No blank-canvas start for beginners; full manual mode
   stays available.
2. **Screenplay workflow** — a screenplay entity: structured
   scene/shot/action/dialogue text (Fountain-lite subset or structured
   editor), parsed into `Scene` + `Shot` rows with dialogue lines and
   cast. Regeneration of the shot list from an edited screenplay is an
   explicit, audited command (never silent).
3. **Storyboard & shot planning** — per-shot camera preset (all ten
   follow modes from `CAMERA_FOLLOW_AND_TRACKING.md`), blocking fields
   (position/path/facing/timing per `SceneCharacter`/`ShotCharacter`),
   dialogue assignment, and a storyboard review surface.
4. **Model selection & compatibility** — per-shot/per-job model picks
   gated by `compatibility.py` (VRAM, architecture, provider
   capability) with beginner-safe defaults and explicit reasons when
   an option is unavailable.
5. **Generation orchestration** — ordered per-shot pipelines
   (keyframe image → approval → motion clip → approval → audio) as
   dependent durable jobs; batch generation with per-job cancel and
   whole-sequence cancel.
6. **Character & visual continuity** — shot prompts compose each
   character's bible refs + wardrobe/expression assignments; reference
   sets and (later) LoRA selection per shot-character, validated so A's
   LoRA can never land on B.
7. **Camera movement & tracking** — camera params on shots map to
   provider capability flags; unsupported moves degrade with an
   explicit user-visible reason, never silently dropped.
8. **Audio/dialogue integration** — per-line TTS/dialogue jobs
   against voice profiles; outputs land on audio lanes at the shot's
   timeline range; existing fades/ducking/automation apply.
9. **Timeline assembly** — "assemble cut" command builds/refreshes
   the timeline from the approved storyboard (shot order, handles,
   crossfades from the plan), keeping later manual edits intact.
10. **Review, revision, export** — approval states per shot output;
    regeneration keeps prior versions (CAS + revisions); existing
    N3 export paths produce the film; `backup.export` packages all.
11. **Single-GPU & multi-GPU** — `gpu_sched` admission across one or
    several local GPUs; video-generation workers get VRAM-aware
    admission (hardware-gated tests classified, not faked).
12. **Optional cloud providers** — same pipeline against authorized
    cloud workers; **all existing gates remain**: live_permit,
    spend caps, credential storage, issue-#96 retrieval rules, and no
    provider call without explicit owner authorization.

## 4. Proposed milestones (independently reviewable PRs)

### A1 — Screenplay & shot-list scaffold (backend-first, UI minimal)
- `screenplays` entity + import/structured edit; deterministic parse
  to `scenes`/`shots` + `dialogue_lines` (shot-scoped, cast-tagged).
- Commands: `screenplay.{import,update,reparse}` with preview diffs —
  reparse never silently deletes user-edited shots.
- Migration: `0020_*`; every row `project_id`-scoped; suite + API tests.
- **Acceptance:** round-trip screenplay→shots→JSON; edit-safe reparse;
  isolation tests; undo/redo on reparse.

### A2 — Shot planning & storyboard board
- Shot fields: `camera_mode` (ten-mode enum), `camera_target`,
  `dialogue_refs`, blocking fields on `ShotCharacter`; board UI with
  cast/continuity view.
- Camera capability mapping surface (provider-neutral schema first).
- **Acceptance:** all ten modes selectable+persisted; cast validation
  (A's settings never apply to B); installed UI checks.

### A3 — Keyframe image generation per shot
- `generate.image.shot_keyframe` command → composed prompt
  (character bible + wardrobe/expression + world + camera hint) →
  durable image job (simulated provider first; real providers gated).
- Approval state on outputs; regenerate creates new versions.
- **Acceptance:** per-shot generation with continuity-composed
  prompts; cancel/restart/idempotency; compatibility-gated model
  picks; objective artifact checks.

### A4 — Shot motion clips (camera-follow)
- `generate.video.shot_motion` — image-to-video with camera-follow
  parameters; bounded durations; provider capability flags honored.
- Simulated video provider for CI; real local workers hardware-gated.
- **Acceptance:** motion clips produced per shot; camera mode carried
  into provider params; unsupported mode → explicit refusal reason.

### A5 — Dialogue & audio generation
- `generate.audio.dialogue` — per dialogue-line TTS-style jobs,
  voice profile per character; outputs placed on the timeline at shot
  range with existing gain/fades/automation applied.
- **Acceptance:** two+ speakers per shot; clip placement correct;
  PCM-verifiable presence in exports (same method as N3-D).

### A6 — Assemble cut & review loop
- `timeline.assemble` command: builds/updates the editor timeline
  from approved shots; preserves manual edits outside replaced
  ranges; crossfades from the plan.
- Approval workflow UI (approve/regenerate/note) + revisions kept.
- **Acceptance:** assemble→preview→export end-to-end on installed
  build; re-assemble doesn't clobber manual edits.

### A7 — Multi-GPU scheduling & cloud gate integration
- Worker admission across enumerated GPUs; per-job GPU affinity;
  cloud providers behind `live_permit` + spend caps + credentials.
- **Acceptance:** multi-GPU box schedules correctly (hardware-gated
  test class, not run on single-GPU CI); cloud path requires and
  honors authorization gates — no live calls in CI.

### A8 — Beginner flow & installed acceptance
- Guided new-film wizard wiring A1–A6 with safe defaults; preset
  packs; copy reviewed for beginner comprehension.
- **Acceptance:** a beginner-shaped E2E (screenplay→film) on the
  installed NSIS build; human-feel checklist updated, still not run.

## 5. Dependencies & risks

- **Provider contracts:** real video/TTS providers are the largest
  unknown — mitigated by simulated providers driving every milestone;
  live providers land only under explicit authorization
  (issue #96, Stage B2b, spend/permit gates unchanged).
- **Hardware:** local video generation needs serious VRAM — classify
  real-gen tests as hardware-gated; never fake.
- **Screenplay parsing:** natural-language scripts are ambiguous —
  use a structured format + preview-diff reparse so nothing is lost.
- **Continuity without LoRA:** bible references + prompt composition
  get consistency first; LoRA training per character stays a later
  milestone (spec §3.5) — Phase A must not promise trained-quality
  continuity.
- **Migration safety:** each milestone's schema ships forward-only
  migrations + a populated-fixture regression (the N3-D pattern).

## 6. Deferred (explicitly not in Phase A)

- LoRA training pipeline & checkpoint management (spec §3.5).
- Real cloud provider adapters / paid inference of any kind.
- 3D proxy preview, advanced color/grade tooling.
- Batch episode/series management beyond single-project scope.
- Staged-submix export beyond the documented ~750-source bound.

## 7. Recommended first PR

**A1 — screenplay & shot-list scaffold.** Smallest blast radius, no
provider/hardware risk, produces the spine every later milestone
builds on (shots + dialogue + cast), and validates the
migration-fixture pattern end-to-end before generation work starts.
