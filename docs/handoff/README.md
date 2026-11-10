# Atomic Dream Engine — Devin Handoff v0.9

## Start here
1. Read [Devin instructions](prompts/DEVIN_START_HERE.md).
2. Read [branding and visual audit](VISUAL_AUDIT_AND_REBRAND.md).
3. Open the [offline design gallery](visuals/GALLERY.html) and [visual-to-requirement index](VISUAL_DESIGN_SPEC.md).
4. Read the base plan and all feature amendments below. Newer amendments clarify conflicts; no accepted feature is silently dropped.

The product is now **Atomic Dream Engine**, repository slug `atomic-dream-engine`, Windows executable `AtomicAIStudio.exe`. This package is a specification and design handoff, not compiled software.

## Previous scope and included documentation

Start with `prompts/DEVIN_START_HERE.md`, then read `DEVELOPMENT_PLAN.md` and `ADDITIONAL_REQUIREMENTS.md`.

## Included
- `DEVELOPMENT_PLAN.md` — product requirements, multi-project and multi-character architecture, data model, phases, tests, and acceptance criteria.
- `prompts/DEVIN_START_HERE.md` — ready-to-paste Devin instruction.
- `diagrams/application_architecture.svg` — system architecture.
- `diagrams/multi_character_data_model.svg` — multi-character relational model.
- `diagrams/character_library_ui_concept.png` — illustrative Character Library wireframe.
- `diagrams/project_dashboard_ui_concept.png` — illustrative multi-project dashboard wireframe.

## Important
- Multi-project isolation is the default; the Shared Asset Library is explicit and opt-in, with versioned links, copy mode, provenance, and local overrides.
The UI image is a concept wireframe, not a screenshot of a working application. The SVGs are architecture diagrams. This bundle is a development specification, not a completed application.

## v0.4 additions
- `ADDITIONAL_REQUIREMENTS.md`: all 16 accepted feature groups, priorities, phase mapping, schema/API extensions and acceptance criteria.
- `CHANGELOG.md`: amendment history.
- `diagrams/five_systems_architecture.svg`: updated modular architecture visual.
- `MANIFEST.json`: SHA-256 checksums for all bundle contents except the manifest itself.

Earlier visual concepts remain included; they illustrate design intent rather than working software.

## v0.5 mandatory amendment
Read [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md). This adds all sixteen latest production capabilities and establishes world-based, direct prompt and hybrid workflows with take-local edits by default, explicit broader changes, immutable approved-shot manifests, phased delivery and acceptance tests. All existing requirements and visuals are retained.

- `CINEMATIC_WORLD_AND_PROMPTS.md` — v0.5 authoritative additions and dual-workflow requirements.
- `diagrams/world_and_prompt_workflows.svg` — world/direct-prompt/hybrid workflow diagram.

## v0.6 mandatory amendment
Read [WORKFLOWS_MEMORY_AND_USABILITY.md](WORKFLOWS_MEMORY_AND_USABILITY.md). Adds the ten accepted workflow/film-memory suggestions, beginner-first operation, consolidated phases, API/schema additions and usability acceptance gates. Direct prompt, world-based and hybrid modes remain mandatory.

## v0.6.1 camera amendment
Read [CAMERA_FOLLOW_AND_TRACKING.md](CAMERA_FOLLOW_AND_TRACKING.md): all ten required camera-follow modes, beginner and advanced controls, coupled target movement, provider capability mapping, schema/API additions and acceptance tests. This extends v0.6; all earlier requirements remain in force.

## v0.7 mandatory amendment
Read `BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md` for 12 beginner-facing capabilities and the Unified Creative Command System. It contains domain models, API outlines, release tests, and phase priorities. All earlier features and visual assets remain included.

## v0.8 mandatory amendment
Read [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md). Adds ten accepted completion/reliability features, prioritizes backup/recovery, smart rendering and production management, defines the four-action beginner home and separates Version 1.0 essentials from later accepted scope. Existing requirements and visuals remain.

- `diagrams/beginner_home_and_completion.svg` — four-action home and prioritized completion systems.
