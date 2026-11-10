# Visual audit and rebrand — v0.9

## Verified source
Audited the actual v0.8 ZIP supplied for this amendment, not an assumed earlier archive. It contained 21 files: 10 visual files (8 SVG diagrams and 2 PNG wireframes), 10 Markdown documents and one checksum manifest. Only two images showed application workspace layouts. The earlier conversational image widgets covered many more screens and cinematic reference categories but their original image bytes/source URLs are not included in that archive or exposed by the available conversation text. Exact original web-image cards cannot be reconstructed faithfully from their search-query descriptions alone.

## Resolution
Bundled 29 new original schematic design boards covering every major prior visual category, an offline gallery and a screen-to-requirement index. Added embedded relative image references in the actual plans rather than merely listing filenames. Added explicit camera-follow diagrams and character/style reference categories. Existing architectural SVGs were rebranded; the two old bitmap workspace previews were recreated under their established paths with Atomic branding. No original retrieved photograph, vendor screenshot or user reference face has been falsely labeled recovered. No unrelated personal images were imported.

## Prior visual-category coverage
| Earlier in-chat category | Bundled replacement design references |
|---|---|
| App navigation, production dashboard, multiple projects | 01_home, 02_projects, 20_production_health |
| Character turnaround, anatomy, emotions, clothes, character bible | 03_character_library, 04_character_bible, 29_character_and_style_reference |
| Location/world reference and environment design | 05_world_bible, 29_character_and_style_reference |
| Storyboard and cinematic shot examples | 06_storyboard; all central scene panels are labeled schematic |
| Scene previsualization, 2D/3D blocking | 07_scene_builder; written requirements retain optional 3D proxies |
| Camera following behind/front/side/orbit and other modes | 08_camera_follow, 28_camera_follow_reference |
| Image-generation UI and concept art | 09_image_studio, 17_style_designer |
| LTX/Wan/Seedance illustrative output and generation panel | 10_video_studio; no fabricated model-output claim |
| Voice workstation, SFX, music and sound mixing | 11_audio_studio, 16_rehearsal, 25_localization |
| Character training and references | 12_training_workshop, 27_motion_and_casting |
| Professional editor, timeline, grading and Resolve examples | 13_editor; architectural interchange requirements preserved |
| ComfyUI/Fusion-like node workflow | 14_workflow_nodes; original layout, not copied product UI |
| Director/assistant planning | 15_assistant, 20_production_health |
| Acting and dialogue preview | 16_rehearsal |
| Fantasy/warm-drama/science-fiction mood references | 17_style_designer; schematic studies, no recovered film stills |
| Temporal masking, repair and rotoscoping examples | 18_region_repair |
| Before/after playback and grading comparison | 19_compare |
| Tool logos and hardware monitor examples | Written provider labels in 21_model_setup; no logo licenses assumed |
| Backup/recovery and setup wizards | 21_model_setup, 22_backup_recovery |
| Film delivery, promo art/trailers and publishing | 23_export, 24_promotional_studio |
| Review/comment player | 26_film_review |
| Onboarding and tutorials | 01_home, 21_model_setup and beginner requirements |

## Naming decision and implementation migration
Display name: **Atomic Dream Engine**. Repository/package slug: `atomic-dream-engine`. Windows executable: `AtomicAIStudio.exe`. Handoff root: `Atomic_AI_Studio_Devin_Handoff`. New installation/project examples use Atomic naming. Generic words such as cinematic, cinematography and Cinematic Style Designer retain their descriptive meaning.

All current-plan exact product-name strings and diagram text are renamed. Prior release versions remain historical version numbers, not rewritten as feature removals. This task amends a specification, not an existing software repository or published installer. Devin must apply the rename to implemented UI titles, About/help, package metadata, installer shortcuts, executable/product resources and documentation if a repository already exists. Inventory existing identifiers before changing application/bundle IDs, data paths or credential namespaces. Preserve UUIDs, project files, signed update compatibility and backup restore; migrate or alias old data paths with explicit reporting rather than creating an empty parallel studio. No domain, trademark or account registration is implied.

## Visual acceptance criteria
Every local Markdown image/link and gallery image resolves inside the extracted handoff. All images open and SVGs parse. Gallery works offline. New mockups carry an illustrative/nonfunctional notice. Written requirements prevail on behavior and release stage; visual detail cannot silently add promised model capabilities. Devin must implement actual controls, accessibility and responsive layout rather than ship these screenshots as the app. Ask for design review at the first runnable workspace, preserving the accepted foundation sprint scope.
