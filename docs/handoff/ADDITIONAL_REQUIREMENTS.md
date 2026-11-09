# Additional requirements — v0.4

These requirements extend DEVELOPMENT_PLAN.md. They are accepted product scope, delivered in the phases below. Medium priority items remain in the roadmap; do not inflate the first sprint. No model/API compatibility claim is established by this document: verify exact revisions before implementing an adapter.

## Five architectural systems

| System | Responsibilities |
|---|---|
| Production Management | Projects, bibles, scripts, episodes, scenes, shots, status, notes and planning |
| Creative Intelligence | Character identity, continuity, camera planning, Director and Cinematographer |
| AI Generation Engine | Provider adapters, workflows, local/cloud inference and LoRA training |
| Post-Production | Timeline, audio, grading, QC, renders and delivery |
| Platform Infrastructure | Storage, search, backups, GPU resources, extensions, budgets and compatibility |

Keep these as modules within the existing desktop/backend architecture; this does not require five network services.

## PROD — Production Management
Priority: **Critical**. Delivery phase: **1**.

Versioned Production Bible and Story Bible: universe rules, lore, visual language, narrative chronology, relationships, scripts and episode history. Link entries to characters, locations, scenes and shots; distinguish approved canon from proposals. Shot states: planned, keyframe_pending, generation_pending, generating, reviewing, approved, rejected, finalized. Require allowed transitions, revision reasons and audit history. Add production notes and timecoded review comments.

Acceptance: Create two bible revisions; preserve earlier approved canon; reject invalid shot transitions; filter a production board by status and character.

## IDENT — Character Identity Lock
Priority: **Critical**. Delivery phase: **2**.

Per-character approved identity bundles: multi-angle face references, front/side/back body views, expression sheets, posture and movement references, hairstyles, wardrobe, age/appearance variants, relationship records and emotional voice presets. Lock resolves exact character-version, compatible LoRAs, references and regional conditioning where available. Multi-person identity binding must remain separate; never assume stacking character LoRAs guarantees correct identity assignment. Unsupported controls produce a limitation report and suggested compositing or separate-shot workflow.

Acceptance: Prepare a three-character shot; inspect separate resolved identity bundles; flag incompatible LoRAs and missing regional support; changing a character does not change a prior snapshot.

## CAM — Cinematic Camera Library
Priority: **High**. Delivery phase: **5**.

Reusable shot, lens, framing, composition, focus and movement presets. Include close-up/medium/wide, dolly/tracking/crane/pan/tilt, lens focal length, sensor assumptions, depth of field, rack focus, handheld/stabilized behavior, eyeline and 180-degree continuity guides. Store both intended camera parameters and actual model conditioning; distinguish physical preview from approximate prompt guidance. Presets are project-owned unless explicitly shared.

Acceptance: Save and reuse a camera setup; export a timed path; warn when the selected engine cannot enforce a requested camera movement.

## DIR — AI Director and Cinematographer
Priority: **High**. Delivery phase: **8**.

Project-aware planning tools consume approved scripts, bibles, identities and continuity context. Director proposes narrative beats, cast, shot list, pacing, audio and rough-cut structure; Cinematographer proposes lenses, lighting, camera paths and blocking. Script-to-storyboard conversion produces editable records and keyframe prompts. Preview a typed change set with entity references, estimates and conflicts; support whole-plan or shot-level approval. Record tool calls and resulting versions.

Acceptance: Convert a script into a multi-character scene proposal; edit and approve a subset; prove unapproved changes do not alter the project or launch generation.

## ASSET — Asset Provenance and Dependency Graph
Priority: **High**. Delivery phase: **1–4**.

Record source/derived asset edges, exact model and runtime revisions, prompts, seeds, conditioning, LoRAs, bible versions and hashes. Show where-used and downstream impact when a dependency changes; mark outputs needing review without deleting or regenerating them. Side-by-side take comparison includes synchronized playback, prompts, parameters, seed, evaluation and human rating.

Acceptance: Update a reference version; list affected shots across explicit shared links; preserve approved assets and immutable provenance; compare two takes.

## QC — Rendering and Quality Control
Priority: **High**. Delivery phase: **4/6/7/9**.

Validate resolution, rational FPS, duration, decode integrity, color metadata, audio clipping and AV offsets. Add versioned optional analysis for flicker, temporal consistency, identity similarity and lip sync, with confidence and human overrides. Implement nondestructive color matching, transition suggestions, batch upscaling/interpolation, audio stems and configurable delivery profiles for cinema, YouTube and social media. Render jobs persist progress and recover at supported segment boundaries; explain when an individual model operation must restart.

Acceptance: Detect deliberately malformed media, clipped audio and known sync offsets; export a chosen profile; interrupt a segmented render and resume without duplicating timeline sections. AI quality scores remain advisory.

## FLOW — Workflow Automation Engine
Priority: **High**. Delivery phase: **0/4/9**.

Versioned reusable directed acyclic workflows: reference analysis → identity review → keyframe → video → audio → QC → upscale → rough cut. Typed node input/output contracts, capability validation, dependency scheduling, approval gates, dry-run estimates, retry limits, cancellation and persisted run state. Use idempotency keys; after uncertain remote submission reconcile provider job state before retrying. Project budget caps and authorization scopes apply across the entire workflow.

Acceptance: Run a small pipeline; restart the application mid-run; retain completed outputs and approval gates; do not duplicate a cloud submission; reject dependency cycles and budget overruns.

## OFF — Offline-first Operation
Priority: **High**. Delivery phase: **0/9**.

Installed local editing, project management, search, references and compatible local inference remain usable without connectivity. Setup may download models and dependencies, but offline mode never silently falls back to cloud. Show missing dependencies; queue explicitly requested cloud jobs until connectivity returns subject to authorization. Optional telemetry and updates require opt-in.

Acceptance: Disconnect networking after setup; open and edit a project, search assets, render locally and use an installed local worker; verify no automatic external requests.

## LAB — Model Compatibility Laboratory
Priority: **High**. Delivery phase: **3/4/9**.

Hardware-specific benchmark profiles measure load time, generation time, peak VRAM/RAM, disk use, errors and output metadata. Pin model hash, runtime, driver, precision, device, resolution, duration and workflow revision. Separate inference from training benchmarks and warm from cold runs. Human quality ratings and optional analytical metrics do not claim a universal best model. Report pass/fail/unsupported and supported fallbacks.

Acceptance: Run a tiny installed-model benchmark twice; retain configuration and comparable results; reject unsupported settings before allocating a long job.

## DEP — Project Dependency Manager
Priority: **High**. Delivery phase: **1/9**.

Project lock manifest lists model/LoRA hashes and versions, runtime requirements, workflow versions, references, fonts, LUTs and effects. Verify availability and licenses; relink relocated files by hash, identify missing or mismatched resources and offer explicit replacements. Export portable packages without secrets; package only redistributable optional weights. Exact seeds and versions improve traceability but do not guarantee bit-identical GPU results.

Acceptance: Move a project to a clean environment; produce an actionable missing-dependency report; relink matching files; refuse a hash mismatch without explicit override.

## LIB — Search, Prompts, Templates and Snapshots
Priority: **High**. Delivery phase: **1/3**.

Project-scoped full-text search covers bibles, scripts, shots, assets, notes and prompts; cross-project search is explicit and respects sharing. Version reusable prompt scaffolds, negative prompts, camera/style presets, project templates and workflows with variables and model compatibility. Transaction-consistent autosave snapshots, retention settings and restore previews protect work. Distinguish autosave revisions from independently restorable backups.

Acceptance: Search same-named characters across two isolated projects; use a typed prompt template; restore an autosave while retaining a recoverable pre-restore revision.

## BUD — Generation Cost and Resource Budgeting
Priority: **High**. Delivery phase: **4/8**.

Estimate local GPU time, VRAM and disk needs separately from cloud currency costs. Store price source/date and uncertainty; estimates are not guaranteed invoices. Record actual usage when returned. Per-project and per-run caps, bounded preauthorization, retry caps and cancellation rules prevent unattended spending. Reapproval is required only outside existing authorized scope.

Acceptance: Exceed a test budget during a dry run and block submission; account for retries; persist actual usage without storing credentials.

## SDK — Plugin SDK
Priority: **Medium**. Delivery phase: **0 contracts; post-9 SDK**.

Versioned provider/effect/workflow extension contracts with manifests, capability declarations, compatibility ranges and conformance tests. Load approved extensions in separate processes, restrict filesystem/network permissions where technically enforceable, and isolate failures. Separate processes alone are not a security sandbox. Clearly disclose trusted-code requirements; support install/disable/update/rollback. No unrestricted Python execution from imported workflow data.

Acceptance: Disable a crashing extension without losing project data; reject incompatible manifests; run a sample adapter through conformance tests.

## LANG — Multilingual Dialogue and Subtitles
Priority: **Medium**. Delivery phase: **6/7**.

Language and locale per dialogue line, voice pronunciation dictionaries, transcript alignment, translation drafts and speaker attribution. Retain source text, translated versions and voice selections. Export SRT/VTT; preview safe-area subtitle layout and font coverage. Subtitle translation and generation do not assume TTS supports every requested language.

Acceptance: Create dialogue in two supported languages; retain speaker identities; export timed captions; flag unsupported voices and missing glyphs.

## REVIEW — Collaborative Review and Approvals
Priority: **Medium**. Delivery phase: **1 notes; post-9 sharing**.

Local comments, revision notes and approval history come first. Later add explicit review-package sharing or authenticated review service with viewer/reviewer roles, timecoded comments, approval/rejection, expiration and revocation. Preserve project isolation and audit records. Do not put the active SQLite database on a shared network folder or imply live multi-writer collaboration; that requires a separate server/storage design.

Acceptance: Export a scoped review package without secrets; import review comments against the correct asset version; reject stale-version approval without reconciliation.

## CAL — Production Calendar
Priority: **Medium**. Delivery phase: **post-9**.

Project tasks and deadlines link to scenes/shots, generation batches, LoRA training and rendering. Dependencies, priorities and estimated GPU windows inform scheduling; user can manually override. Calendar plans never imply an unavailable machine will execute a job. Calendar integration and notifications are optional.

Acceptance: Reschedule a training task and show affected dependent tasks; flag unavailable GPU resources without silently launching work.

## Data and API additions

All production-owned rows carry project scope and validated versioned relationships. Add migrations incrementally:

| Domain | Proposed entities |
|---|---|
| Story and production | BibleDocument, BibleRevision, StoryEvent, CharacterRelationship, ShotStatusEvent, ProductionNote, ReviewDecision |
| Camera and identity | CameraPreset, CameraPath, IdentityBundleVersion, ConditioningBinding |
| Asset lineage | AssetDependency, ComparisonSession, DependencyManifest, DependencyEntry |
| Workflow | WorkflowDefinitionVersion, WorkflowNode, WorkflowEdge, WorkflowRun, NodeRun, AuthorizationGrant |
| Quality and benchmarks | QCReport, QCFinding, BenchmarkProfile, BenchmarkRun, DeliveryPreset |
| Reuse and resilience | PromptTemplateVersion, ProjectSnapshot, SearchIndexEntry |
| Budgets | BudgetPolicy, CostEstimate, UsageLedgerEntry |
| Later scope | ExtensionInstallation, ReviewPackage, ReviewComment, TranslationVersion, SubtitleCue, CalendarTask |

API service groups: project bibles/revisions; shot transitions; notes/reviews; camera presets; identity-bundle resolution; asset where-used/impact; workflow validate/dry-run/run/cancel; QC reports; benchmark runs; dependencies/verify/relink; snapshots/restore; budgets/usage. Use project-scoped typed contracts, pagination, optimistic concurrency for edits, and idempotency for submissions. WebSocket events must include project_id, resource_id, version, run_id where applicable, event sequence and status; reconnect can replay or reconcile from persisted state.

## Implementation sequencing

Preserve Phases 0–9 in the main plan. Foundation contracts, project scoping, provenance and persistent jobs come first. Build a thin full path before expanding model coverage. Add bibles/search/notes/snapshots in Phase 1, identity bundles in Phase 2, prompt templates and benchmarks in Phase 3, workflow execution/budgets/take comparison in Phase 4, camera controls in Phase 5, language/audio QC in Phase 6, render QC in Phase 7, approved AI planning in Phase 8 and dependency/offline/recovery validation in Phase 9. Deliver external plugin SDK, collaborative review service and advanced calendar afterward. Basic offline operation and extension interfaces are designed in Phase 0.

## First sprint boundary

Do not implement all additions at once. Inspect the existing repository, establish Phase 0, then implement Project Dashboard and multi-character vertical slice. Include schemas/contracts for project scope, immutable asset lineage, typed jobs and bible revisions where needed by that slice. Report acceptance evidence, unsupported features and next milestone. Production code and model integrations require real validation; mock adapter tests prove orchestration only.
