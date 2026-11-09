# Visual Design Specification — Atomic AI Studio v0.9

## Status and design authority

These are **new original schematic UI mockups**, created to fill the missing visual coverage in the earlier handoff. They are not recovered copies of prior in-chat web-image cards, screenshots of existing software, or evidence of implemented features. They illustrate intended layout, hierarchy, controls and workflow. The written requirements define behavior; the mockups guide design. Exact art, iconography and brand mark remain proposals for user review.

Open [the offline visual gallery](visuals/GALLERY.html) for all 29 boards. Every image is bundled locally; no network access is needed. Prior architecture diagrams remain in `diagrams/`, rebranded.

## Visual direction

Dark navy surfaces, restrained teal actions, violet selection/camera cues, warm warning indicators. Use accessible contrast, scalable type and keyboard focus; never color alone. Consistent project/branch context at top, task navigation at left, primary working surface center, optional inspector at right and visible editable prompt panel. Guided Studio progressively reveals technical settings. Mockup dimensions are a design reference, not a fixed desktop size. Responsive/dockable behavior and Windows accessibility remain required.

## Screen-to-requirement map

| Visual | What Devin should take from it | Requirement documents |
|---|---|
| [Create something extraordinary](visuals/01_home.png) | A simple starting point. No technical setup required to explore. | [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md) |
| [Your projects](visuals/02_projects.png) | Independent productions, explicitly shared assets and recoverable versions. | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) |
| [Your cast](visuals/03_character_library.png) | Many characters, independent identities, wardrobes and voices. | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) |
| [Mira • Character Bible](visuals/04_character_bible.png) | Approved identity v3 • Appearance, references and continuity remain versioned. | [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md) |
| [The Lantern Keep • World Bible](visuals/05_world_bible.png) | Recurring rooms, props, geography and approved viewpoints. | [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md) |
| [Scene 04 • Storyboard](visuals/06_storyboard.png) | Shot list with cast, camera, lighting, performance, dialogue and sound. | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) |
| [Block the scene](visuals/07_scene_builder.png) | Drag characters, place the camera and draw movement paths. | [BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md](BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md) |
| [Camera • Follow a subject](visuals/08_camera_follow.png) | Beginner controls first. Camera trajectory preview is separate from model fidelity. | [CAMERA_FOLLOW_AND_TRACKING.md](CAMERA_FOLLOW_AND_TRACKING.md) |
| [Image Studio](visuals/09_image_studio.png) | Describe, reference, generate variations and approve a keyframe. | [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md) |
| [Video Studio](visuals/10_video_studio.png) | First / last frames, cast identity, model capability and take review. | [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md) |
| [Audio & Voice Studio](visuals/11_audio_studio.png) | Exact spoken text, performance instructions and editable stems. | [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md) |
| [Keep your character consistent](visuals/12_training_workshop.png) | Optional guided training. Advanced LoRA controls stay available. | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) |
| [Edit your film](visuals/13_editor.png) | Multitrack timeline, inspector, grade, audio and reversible edits. | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) |
| [Workflow • Advanced](visuals/14_workflow_nodes.png) | The same production engine, exposed as validated typed nodes. | [WORKFLOWS_MEMORY_AND_USABILITY.md](WORKFLOWS_MEMORY_AND_USABILITY.md) |
| [Creative Assistant](visuals/15_assistant.png) | An editable proposal, never a hidden project mutation. | [BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md](BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md) |
| [Rehearse the performance](visuals/16_rehearsal.png) | Temporary audio and proxies before expensive generation. | [BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md](BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md) |
| [Choose the cinematic look](visuals/17_style_designer.png) | Palette, lighting, lens and atmosphere are editable settings. | [BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md](BEGINNER_CREATIVE_STUDIO_AND_COMMANDS.md) |
| [Fix part of a shot](visuals/18_region_repair.png) | Time range, tracked mask, repair prompt and versioned result. | [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md) |
| [Compare takes](visuals/19_compare.png) | Synchronized A/B playback and source-time alignment. | [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md) |
| [Your next production step](visuals/20_production_health.png) | Actionable blockers, progress, budget and export readiness. | [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md) |
| [Set up your studio](visuals/21_model_setup.png) | Compatible downloads, local/cloud choice and honest estimates. | [WORKFLOWS_MEMORY_AND_USABILITY.md](WORKFLOWS_MEMORY_AND_USABILITY.md) |
| [Your work is safe • Recovery](visuals/22_backup_recovery.png) | Restore a verified version and reconcile interrupted jobs. | [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md) |
| [Finish & export](visuals/23_export.png) | Choose a destination, review validation and keep your master. | [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md) |
| [Posters, thumbnails & trailers](visuals/24_promotional_studio.png) | Editable typography, approved footage and source references. | [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md) |
| [Create another language version](visuals/25_localization.png) | Human-reviewed translation, voices, subtitles and lip-sync. | [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md) |
| [Review the finished sequence](visuals/26_film_review.png) | Timecoded notes map back to the correct take and source frames. | [PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md](PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md) |
| [Casting & motion references](visuals/27_motion_and_casting.png) | Audition identities and bind motion tracks to the right actors. | [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md) |
| [Camera-follow visual reference](visuals/28_camera_follow_reference.png) | Ten mode schematics and movement relationships. | [CAMERA_FOLLOW_AND_TRACKING.md](CAMERA_FOLLOW_AND_TRACKING.md) |
| [Character & style reference categories](visuals/29_character_and_style_reference.png) | Face, body, expression, wardrobe, light and location reference coverage. | [CINEMATIC_WORLD_AND_PROMPTS.md](CINEMATIC_WORLD_AND_PROMPTS.md) |
