# Mandatory v0.9 instruction — Atomic Dream Engine

Use the renamed product **Atomic Dream Engine**, slug `atomic-dream-engine` and executable `AtomicAIStudio.exe`. Read `VISUAL_AUDIT_AND_REBRAND.md` and `VISUAL_DESIGN_SPEC.md`, and open `visuals/GALLERY.html` before designing screens. The 29 new boards are schematic layout references, not working screenshots or recovered original in-chat photographs. The written requirements and release stages remain authoritative. Follow embedded image references in the plans; do not ignore the pictures or ship them as a static UI. Implement a reviewable, accessible workspace with real controls and preserved prompt editing. Apply the documented migration precautions if prior code/data already exists. Keep the first implementation milestone bounded.

# Mandatory v0.8 instruction for Devin
Read `PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md` after the preceding amendments. Preserve Unified Creative Commands and all prior requirements. Prioritize verified backup/recovery, selectable smart rendering and an explainable production next-step service. Freeze a Version 1.0 short-film backlog and keep post-release features in the accepted roadmap. Keep the foundation sprint bounded; do not build ten parallel placeholder modules.

# Mandatory v0.7 instruction for Devin
Before implementing UI-specific state mutations, read `BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md` and design the Unified Creative Command System. Make Guided Studio the default beginner workspace, while keeping direct prompt editing always available. Implement one vertical slice end-to-end and demonstrate it with tests before building many disconnected features. Preserve all v0.6.1 camera-follow requirements. Report capability limitations and actual tested results; do not claim features work merely because they appear in the specification.

# Devin task prompt — Atomic Dream Engine

You are implementing Atomic Dream Engine, a Windows standalone AI filmmaking application. Read `DEVELOPMENT_PLAN.md` and `ADDITIONAL_REQUIREMENTS.md` and `CINEMATIC_WORLD_AND_PROMPTS.md` and `WORKFLOWS_MEMORY_AND_USABILITY.md` and `CAMERA_FOLLOW_AND_TRACKING.md` completely before changing code. Follow the phased delivery map; the additional requirements are accepted scope, not instructions to implement everything in the first sprint.

## Non-negotiable product requirements
The application must support **multiple independent projects** from one installation. Project isolation is the default and must be enforced by backend services and database relationships, not only by UI state. Each project has independent settings, assets, characters, scenes, shots, LoRAs, timelines, and generation history. Provide a Project Dashboard, project switching without restart, templates, duplicate/archive/restore/delete, independent backup/restore, and portable export/import.

Provide an optional Shared Asset Library, but sharing is explicit and opt-in. Support copying an asset into a project or linking a versioned shared asset with provenance. Shared updates must never silently change consuming projects or historical generations; support project-local overrides.

Phase 2 is a **multi-character system**. A project must contain many characters, and a scene or shot must support multiple characters at once. Use normalized many-to-many relationships (`SceneCharacter`, `ShotCharacter`). Never use a single `character_id` field as the only character association on a scene or shot. Each character must retain independent identity, references, wardrobe, expression, voice, LoRA versions, and continuity rules.

## Work order
1. Inspect the existing repository and report its current state.
2. Propose a concise implementation plan based on the document.
3. Implement Phase 0 foundation without breaking existing work.
4. Design and migrate the relational schema for projects, project-scoped assets, shared-asset links/provenance, characters, scenes, shots, and the scene/shot character join entities.
5. Add API schemas/endpoints and tests.
6. Implement a functional Project Dashboard: create/open/switch/rename/duplicate/archive/restore projects, with project-scoped settings.
7. Implement project isolation and project backup/export foundations; add tests for cross-project access rejection.
8. Implement a functional Character Library vertical slice: list, search, create, edit, archive, and open a character profile.
9. Implement scene/shot cast assignment UI/API and tests.
10. Implement explicit shared-asset copy/link workflow with provenance and local overrides.
11. Keep the app runnable after each increment.

## Required proof
Tests must demonstrate:
- At least three independent projects with different settings.
- Project switching without restart and correct UI-state refresh.
- Cross-project asset/character/LoRA assignment is rejected unless explicitly shared.
- Shared asset copy and linked modes preserve provenance.
- Source changes require explicit acceptance in consuming projects; local overrides do not mutate source assets.
- Independent project backup/restore and package export/import.
- 20+ characters in one project.
- 8+ characters associated with one scene.
- 5+ characters associated with one shot.
- Independent per-character wardrobe, voice, expression, reference, and LoRA assignment.
- Updating one character does not mutate another.
- Generation snapshot captures the exact version/settings for every assigned character.
- Relationships survive application restart.

## Constraints
- Follow the stack in the handoff: Tauri 2, React + TypeScript, Python FastAPI, SQLAlchemy, SQLite, Alembic, FFmpeg.
- Keep UI components free of direct provider calls.
- Use UUIDs and explicit foreign keys.
- No hardcoded secrets or machine-specific model paths.
- Mark unimplemented integrations as stubs; do not claim they work.
- Add migrations, tests, documentation, and changelog entries.
- Ask for approval before paid cloud calls, destructive changes, or training/voice cloning actions.

## First response
Inspect the repository and return:
1. Existing structure and technologies found.
2. Gaps against the handoff.
3. The exact first milestone you will implement.
4. Any blocking questions.
Do not start a large rewrite before this inspection.

## v0.4 architecture requirements
Use the five systems defined in the addendum. Preserve offline local operation, versioned identity bundles, bible revisions, provenance/dependency edges, persistent workflows, budgets and approval scope. Add feature acceptance evidence from the addendum in the relevant phase. Treat identity locks and AI QC as best-effort conditioning and advisory analysis, never as guarantees. Keep collaboration and the external plugin SDK in their specified later milestones.

## v0.5 non-negotiable user preference
Support both persistent cinematic-world production and direct prompt control for image, video and voice/audio. Prompt-only generation must not require a bible. Hybrid workflows expose compiled prompts and conditioning, allow take-local text/conditioning overrides and explicit broader change proposals. Never silently modify canon or approved shots. Implement the new amendment incrementally in the mapped phases; preserve the first sprint scope.

## v0.6 execution priorities
Design one workflow engine with simple and node views, evidence-backed narrative memory and approved story-to-film plans. Default to Guided Studio for nontechnical users; never require nodes, terminal commands, model terminology or LoRA training for a first creative result. Preserve all prompt controls and make technical depth optional. Follow phase boundaries, record real usability evidence separately from automated tests, and keep the original foundation sprint achievable.

## v0.6.1 camera requirement
Implement the complete ten-mode follow system as a high-priority Phase 5 scene-blocking capability. Follow paths must respond to target movement, with independent look-at/focus, group framing, target transitions, collision/lost-target handling and editable prompts. Use the same versioned camera data in Guided Studio and advanced nodes. Distinguish 2D/3D preview precision from actual model conditioning support. Preserve all prior scope and first-sprint boundaries.
