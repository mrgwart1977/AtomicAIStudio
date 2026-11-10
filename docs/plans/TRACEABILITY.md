# Requirements traceability — Milestone 1

Maps the M1 brief (project management + large-media storage foundations) to
implementation and test evidence. Status as of the `feat/m1-projects` branch.

## 1. Project dashboard

| Requirement | Implementation | Evidence |
|---|---|---|
| Create / open / rename / duplicate / archive / restore / delete | `project.*` commands in `backend/app/domain/commands.py`; card actions in `apps/desktop/src/features/ProjectsPage.tsx` | `test_m1.py::test_duplicate_project`, `test_archive_blocks_mutations`, `test_delete_requires_confirm_name`; screenshots `docs/m1-evidence/01-projects.png` |
| Cards: thumbnail, title, last-modified, progress | `ProjectCardOut` (counts, running_jobs, thumbnail_asset_id); `Card` in ProjectsPage | `test_projects_list_returns_cards` |
| Search, sorting, filtering | Client-side on card list: name search, updated/created/name sort, status + kind filters | `01-projects.png` |

## 2. Creation wizard

| Requirement | Implementation | Evidence |
|---|---|---|
| Title, project type, aspect ratio, resolution, frame rate, location, style | 3-step `NewProjectDialog` (basics → format → location & style); backend defaults `ProjectCreate` | `POST /api/projects` coverage in `test_m1.py` (rational fps 24000/1001) |
| Beginner defaults + advanced settings | Guided 16:9/1080p/24fps defaults; storage location + style under "Advanced" | wizard steps 1–3 |

## 3. Project isolation

| Requirement | Implementation | Evidence |
|---|---|---|
| Per-project characters/worlds/locations/assets/settings/history | All owned tables carry `project_id` FK (CASCADE); queries scoped | `test_isolation.py` (cross-project read/write denied) |
| No cross-project contamination | `_validate` + `bibles.get_entity` check `project_id`; `get_project` dependency | `test_m1.py::test_entity_isolation` |
| Explicit sharing only | `asset.share` (scope→shared) + `asset.link` (link/copy through `shared_asset_links`); `GET /shared-assets` is the only cross-project read | `test_share_and_link_asset` |

## 4. Character & World Bible foundations

| Requirement | Implementation | Evidence |
|---|---|---|
| Persistent versioned characters/worlds/locations | `characters`, `worlds`, `locations` tables + `domain/bibles.py`; `entity_revisions` snapshots every mutation | `test_entity_revision_history`, `test_entity_revert` |
| Create/edit/save/reopen | `{etype}.create/update/archive/unarchive/revert` commands; `EntityPage` editor + version list | API round-trip in `test_entity_create_update_versions` |
| No AI training/inference | None implemented (mock adapter only) | `test_adapters_list_mock_only` |

## 5. Backups & recovery

| Requirement | Implementation | Evidence |
|---|---|---|
| Backup / restore / import / export | `.atomicpkg` (zip) via `domain/backup.py`; `backup.export` job writes sha256 manifest; `backup.restore`/`backup.import` rebuild as NEW project | `test_backup_export_roundtrip`, `test_import_creates_new_project` |
| Preserve references & relationships | Manifest rows carry IDs; world→location links remapped; CAS blobs copied by hash | `test_backup_restores_entities_and_assets` |
| Validation, integrity, incomplete-op recovery | Manifest + per-file sha256 verify on import; staging dir + failed→sweepable runs; `backup_runs` reconciled at startup | `test_import_rejects_corrupt_package`, `test_import_rejects_bad_manifest` |

## 6. Unified Creative Command integration

| Requirement | Implementation | Evidence |
|---|---|---|
| All mutations via commands | No write endpoints for project-owned resources outside `/commands/*`; entity/asset/backup ops are command types | routers: only GET endpoints for entities/assets |
| Audit history, validation, undo | `creative_commands`, `command_transactions` (before/after), `entity_revisions`, `project_revisions`, `audit_events`; `UNDOABLE` set | `test_commands.py` (idempotency, revision conflicts, undo) |

## 7. UX

| Requirement | Implementation | Evidence |
|---|---|---|
| Design references | Board design tokens in `styles.css`; workspace tabs per boards | `02-overview.png` |
| Progress indicators | JobsPanel live WS progress; durable ops show job status | `installed-home.png`, `03-cast.png` |
| Confirmation for destructive actions | `ConfirmDialog` (type-the-name delete) + confirm dialogs for archive/revert/restore | `ProjectsPage`, `EntityPage`, `BackupsPage` |
| Understandable errors | `CommandError` → readable detail in UI; friendly empty states | all pages |

## 8. Testing & evidence

| Requirement | Evidence |
|---|---|
| Creation, isolation, persistence, duplication, backups, import/export, version history, failure recovery | `backend/tests/` — **34/34 pass** (15 M1 + 19 M0 regression) |
| Screenshots | `docs/m1-evidence/*.png` — dashboard, workspace, cast, assets, backups, settings, models, installed app |
| Dev + packaged verification | Dev: vite+backend; Packaged: NSIS reinstall → real `0001→0002` upgrade on existing DB → M1 API E2E (character create + backup job) → screenshot |

## 10. Large-media storage foundations

| Requirement | Implementation | Evidence |
|---|---|---|
| Media on filesystem, never in SQLite | CAS blobs under `<location>/cas/<sha>`; DB holds `asset_files`/`asset_versions` refs | `storage.py`, `test_cas_roundtrip` |
| SQLite for metadata/refs/history/jobs | Existing schema + `asset_versions`, `entity_revisions`, `backup_runs` | models |
| Configurable locations, multiple drives | `storage_locations` (CRUD + default); project `storage_location_id` | `test_storage_locations`, SettingsPage |
| Relative paths for portability | `asset_files.rel_path` relative to location root; `.atomicpkg` stores rel paths only | backup manifest |
| Responsive with thousands of assets | `assets` page endpoint (`offset/limit`, `total`, `kind/q/status` filters); indexed FKs | `test_assets_pagination` |
| Atomic writes + interrupted transfers | `storage.put` writes `.tmp`→`os.replace`; `.tmp` sweep on startup; CAS dedupe | `test_orphan_tmp_swept`, `test_cas_dedupe` |
| Streaming backup/export (no full-RAM) | zip writes/reads via `shutil.copyfileobj` chunks; manifest assembled incrementally | `backup.py` |
| Missing/moved media + relink | `assets.verify` job marks `missing`; `asset.relink` re-hashes new path into CAS | `test_verify_marks_missing`, `test_relink_restores` |
| Proxy/render-cache/index compatible | `asset_versions.media_json` is open-ended (proxies later); CAS is immutable | design note |

## 9. Outstanding release requirements (tracked, not blocking)

See `docs/RELEASE_CHECKLIST.md`.

---

# Milestone 1.1 — Storage, Backup & Recovery Improvements

Status as of the `feat/m1.1-storage` branch (builds on M1).

## A. Configurable backup destinations

| Requirement | Implementation | Evidence |
|---|---|---|
| Default backup dir via native folder picker | `PUT /api/settings/backup-dir` + `pickDirectory` (Tauri dialog); Settings → Backups card | `test_app_default_backup_dir_set_reset`; `06-settings.png` |
| Internal/secondary/external/network paths | Absolute-path validation; drive-letter + UNC reachability checks in `backup_dest._root_reachable` | `test_validate_endpoint_catches_missing_drive` |
| Per-project override | `projects.backup_dir` via `project.update_settings`; resolution order project → app → built-in | `test_project_override_beats_app_default` |
| Free space + estimated size before start | `GET …/backup-destination` snapshot: free_bytes + estimated_bytes (sum of deduped blobs + overhead) | `test_destination_snapshot_reports_estimate_and_space` |
| Exists/writable/sufficient-space validation | `validate_destination` probes: real tempfile write+fsync, `disk_usage` headroom | `test_m11.py` destination tests |
| Disconnected drive / dead share / insufficient space | Friendly `DestinationError` messages per cause | `test_validate_endpoint_*`, `test_insufficient_space_fails_clean` |
| No silent fallback | `resolve_dir` returns the configured dir verbatim; failure surfaces as job error, never reroutes | `test_project_override_beats_app_default` |
| Preferences persist across restarts | `app_settings` table (`backup_dir` key) — SQLite, survives restart+reinstall | `test_backup_dir_persists_across_settings_reads` |
| Reset to default | `PUT /api/settings/backup-dir {"dir": null}` + "Use app default" per-project | tests + Settings/Backups UI |
| Compatibility with pre-M1.1 backups | Built-in dir unchanged as default; `backup_runs` history includes legacy paths; v1 manifest format untouched | `test_format_v1_compat_roundtrip` |

## B. Large-project performance & recovery

| Requirement | Implementation | Evidence |
|---|---|---|
| Background ops, unblocked UI/API | All ops run in the durable job engine via `asyncio.to_thread`; API returns job id immediately | all job tests |
| Bounded-chunk streaming | 8 MiB `iter_chunks` for export + import staging; hash+write interleaved | `test_large_file_export_streams_bounded`; **400 MiB blob → 17 MiB peak RSS delta** (measurement, PR body) |
| Real-time bytes/%/file/elapsed/ETA | `ProgressReporter` → `generation_jobs.progress_json` → WS `job.progress` payload + polling endpoints | `08/09-backup-running.png`; packaged job progress_json output |
| Safe cancellation | `cancel_requested` column + `threading.Event` checkpoints between chunks; queued cancels instantly, running cancels at next chunk | `test_cancel_export_mid_run`, `test_cancel_queued_job`, `test_cancel_terminal_job_conflict`; packaged cancel evidence |
| Cancel never corrupts originals | `.partial` unlinked on any abort; staging dir removed in `finally`; original project untouched | packaged: `ls` of dest dir shows no `.partial`; `test_cancel_export_mid_run` re-exports fine |
| Temp files + atomic finalization | `name.atomicpkg.partial` → fsync → `os.replace` | `backup.py::export_package` |
| Incomplete-op detection after crash | `backup_runs` row created 'running' at job start; startup reconcile → 'failed (interrupted)'; `sweep_backup_destinations` removes `.partial`/`.staging` across all configured dirs | `test_interrupted_run_row_reconciled`, `test_partial_package_swept_on_startup` |
| Manifest + sha256 validation before accept | Same v1 rules; all-or-nothing single-commit import | `test_corrupt_package_rejected_before_commit` (deterministic blob tamper) |
| Restore creates new project | Unchanged: `project_id_override=None` path only | `test_restore_creates_new_project_not_overwrite` |
| Missing/disk/permission/corrupt errors | `DestinationError` (friendly), `PackageError`, run rows marked failed with message | `test_insufficient_space_fails_clean` |
| Concurrency protection | Per-(project, op-family) lock in `JobEngine.enqueue` → 409; families: backup, asset | `test_second_backup_op_rejected_while_running` |
| Beginner-friendly errors | Messages like "Drive D: is not connected…", "Not enough space — need about 48.0 MB…" | snapshot/UI error display |
| Safe temp cleanup | `_sweep_dir`/`sweep_staging` only touch `*.partial`/`*.staging` patterns | sweep tests |
| `.atomicpkg` + CAS compatibility | FORMAT_VERSION stays 1; new run/manifest fields additive only | `test_format_v1_compat_roundtrip` |

## C. Performance & scalability

| Requirement | Implementation | Evidence |
|---|---|---|
| Tens of thousands of assets / TB media | Manifest streams rows out of a short read txn; file streaming outside DB txn; dedupe by sha keeps package ≤ unique bytes | design + tests |
| No whole-archive reads | Export/import both chunked; zip container never materialized | 400 MiB → 17 MiB RSS measurement |
| Short DB txns during transfers | Manifest read commits before streaming; import commits once after staging | `export_package`/`import_package` session scope |
| Responsive cancel/progress at scale | Checkpoints every 8 MiB; progress throttled to 0.25 s emits; WS publishes per update | live screenshot ~14–17 s ETA |
| Documented limits | ZIP64 enabled (per-entry >4 GiB OK, zip64 central dir); FAT32 destinations blocked for >4 GiB estimates (warned otherwise) via `psutil` fstype probe | `test_fat32_limit_blocks_large_backup` |

## D. UI

| Requirement | Implementation | Evidence |
|---|---|---|
| Destination selection | Backups page "Backup destination" card (choose for this project / use app default); Settings "Backups" card (app-wide) | `05-backups.png`, `06-settings.png` |
| Available storage info | Free space + estimated backup size in destination card | `05-backups.png` |
| Progress indicators | Live card: %, bytes done/total, rate, ETA, current file; JobsPanel same detail for any op | `08/09-backup-running.png` |
| Cancel control | Cancel button on active backup card + every running job in Activity | `08/09`, `10-installed-app.png` (cancelled job) |
| History + status | Table adds destination dir; failed/interrupted/cancelled states with error text | `05-backups.png`, packaged `GET /backups` |
| Clear errors | Errors/warnings rendered from snapshot + job errors | UI code |
| Restore/recovery actions | Restore-as-new-project; interrupted runs labeled | `05-backups.png` |
| Beginner simple / advanced available | One-click "Back up this project"; overrides + app default tucked behind buttons/Settings | layout |

## E. Testing

| Requirement | Evidence |
|---|---|
| Custom destinations, persistence, overrides, missing/disconnected, insufficient space, streaming, cancel, crash recovery, corruption, concurrency, v1 compat, isolation | `backend/tests/test_m11.py` — **19 tests**, plus 34 M1/M0 regression = **53/53 pass** |
| Packaged Windows verification | NSIS reinstall over M1 DB → real `0002→0003` migration → custom dir set → export → **live cancel at 117/981 MB** → no `.partial` left → cancelled run row; screenshots `10-installed-app.png` |

---

# Milestone 2 — Multi-Character Foundation

Status as of the `feat/m2-storyboard` branch (builds on M1.1).

## A. Character Library

| Requirement | Implementation | Evidence |
|---|---|---|
| Create / edit / duplicate / archive / search / organize | `EntityPage` (Cast tab): search box over name/summary/data, `character.duplicate` command, archive/unarchive, structured editor | `test_character_duplicate`, `test_character_search_*` via API coverage; `docs/m2-evidence/02-character-library.png` |
| Name, identity, appearance, distinguishing features | `FIELD_HINTS.character` structured sections over `profile_json`; non-string values round-trip as editable JSON (never `[object Object]`) | `04-character-editor.png` |
| Personality / behavior / background | Same field set: `personality`, `behavior`, `backstory` | `04-character-editor.png` |
| Facial expressions & emotional states | `LIST_FIELDS.character.expressions` — add/remove rows `{name, notes}` stored as JSON array | `04-character-editor.png` |
| Wardrobe & costume variations | `LIST_FIELDS.character.wardrobe` rows | `04-character-editor.png` |
| Voice profile & speaking style | `voice_profile`, `speaking_style` fields | `04-character-editor.png` |
| Reference images & identity assets | `entity_assets` table + `entity.assets.link/unlink` commands; `ReferenceAssets` picker over ready image assets with role (`reference`/`identity`/`wardrobe`/`voice`/`style`) | `test_entity_asset_link_roles`, `test_entity_asset_isolation` |
| AI model & LoRA associations | `model_ref` field + `loras` CSV list in `profile_json` | `04-character-editor.png` |
| Version history & continuity metadata | `entity_revisions` snapshots (create/update/revert); `continuity_notes` + `visual_anchor` fields reserved for Identity Lock baseline | `test_entity_revert` (M1 suite), `test_m2.py` revert coverage |
| Strict isolation / explicit sharing | All character queries scoped by `project_id`; sharing unchanged (asset-level only) | `test_entity_isolation` (M1), `test_m2` isolation tests |

## B. Multi-character casting

| Requirement | Implementation | Evidence |
|---|---|---|
| Multiple characters per scene/shot | `scene_characters`, `shot_characters` join tables — no hard-coded limit (unique (shot, character) constraint only) | `test_multi_character_cast_state`, `test_scene_cast_multiple` |
| Independent per-cast state | `shot_characters.state_json` / `scene_characters.state_json`: `expression`, `emotion`, `wardrobe_variant`, `blocking`, `action`, `dialogue`, `voice`, `model_ref`, `lora_refs`, `continuity_notes` | `test_cast_state_independent_across_shots` |
| Identity / model / LoRA / reference associations | `model_ref`, `lora_refs`, `reference_asset_ids` keys inside cast `state_json`; entity-asset links carry `role` | cast payload round-trip tests |
| No arbitrary limit | Single test casts 5 characters into one shot; design imposes no cap | `test_many_characters_per_shot` |

## C. Scene & shot management

| Requirement | Implementation | Evidence |
|---|---|---|
| Persistent scenes/shots, stable IDs | `scenes` / `shots` tables, UUID `new_id()` | `test_scene_shot_create` |
| Ordering | Integer `position` + `scene.move` / `shot.move` commands; `_next_position` max+1 (zero-safe) | `test_scene_order`, `test_shot_reorder_and_move_across_scenes` |
| Duplication | `scene.duplicate` deep-copies scene + shots + cast rows + entity-asset links; `shot.duplicate` same at shot scope | `test_scene_duplicate_deep`, `test_shot_duplicate` |
| Editing / archiving | `scene.update`, `shot.update`, `scene.archive`/`unarchive` (shot archiving via scene or `shot.archive`) | update/archive tests |
| Version history | `entity_revisions` rows for every scene/shot mutation (including cast snapshots) via `snapshot_scene`/`snapshot_shot` | `test_scene_version_revert`, `test_shot_revert_restores_cast` |
| Character/world/location assignment | `scene.world_id`, `scene.location_id`, `shot.location_id` FKs + `scene.cast.*` | `test_scene_location_world_links` |
| Referential integrity | `_validate` rejects cross-project/dead refs before mutation; FK CASCADE on entity delete | `test_isolation_rejects_cross_project_cast` |

## D. Visual storyboard

| Requirement | Implementation | Evidence |
|---|---|---|
| Shot cards with type, title, prompt | `StoryboardPage` — scene sections with per-shot cards (type badge, title, prompt preview, cast chips) | `docs/m2-evidence/01-storyboard.png` |
| Drag-and-drop ordering | Hand-rolled HTML5 DnD (no external lib): drag shot card → drop target → `shot.move` command | `01-storyboard.png` (ordered cards), reorder covered by `test_shot_reorder_*` API-side |
| Character assignments visible on cards | Cast initials chips per card from `shot.cast` | `01-storyboard.png` (AV/MK chips) |
| Locations, descriptions, cinematic intent | `scene.intent`, `scene.location_id`, `shot.title/description/prompt` shown on cards and editors | `01-storyboard.png` |
| Manual prompt editing always available | `prompt` field in shot editor → `shot.update`; bottom PromptBar unchanged | shot update tests |
| Scene/shot editor dialogs | Radix dialogs: `New scene`, `Edit`, `Duplicate`, `Archive` per scene; `+ Shot` per scene; full shot editor with cast table | `01-storyboard.png` |

## E. Continuity foundations

| Requirement | Implementation | Evidence |
|---|---|---|
| Structured continuity metadata | Cast `state_json.continuity_notes` + `expression`/`emotion`/`wardrobe_variant` per shot; character `continuity_notes`/`visual_anchor` in `profile_json` | `test_cast_state_independent_across_shots` asserts per-shot wardrobe/expression deltas |
| Scene-to-scene change tracking | Revisions snapshot cast state, so a wardrobe change between shots is diffable in history | `test_shot_revert_restores_cast` |
| Architecture for Identity Lock / Film Memory / Continuity Supervisor | `entity_assets` role taxonomy (`identity`, `voice`), cast `reference_asset_ids`, `model_ref`/`lora_refs` hooks; documented in code | schema + domain comments |
| No false consistency claims | No inference run; copy says "structured metadata" not "AI-consistent" | strings in UI |

## F. Unified Creative Commands

| Requirement | Implementation | Evidence |
|---|---|---|
| All M2 mutations via preview/commit/audit/undo | `SUPPORTED_COMMANDS` + `_validate` + `_commit_storyboard` + `_undo_storyboard`; `CommandType` literals in API schema | `test_m2.py` command-path tests (all mutations via `/commands/commit`) |
| Cross-project protection | `_validate` checks every referenced id's `project_id` before commit | `test_isolation_rejects_cross_project_cast`, `test_isolation_rejects_cross_project_entity_asset` |
| Optimistic concurrency / idempotency | `expected_revision` + `idempotency_key` on every commit (existing machinery) | `test_commands.py` + M2 usage |
| Undo | Scene/shot/cast/entity-asset undo reconstructs rows from `before`/`after` snapshots; timestamps stripped → fresh defaults | `test_undo_scene_create`, `test_undo_cast_assign` |

## G. Testing & delivery

| Requirement | Evidence |
|---|---|
| Multi-char cast, independent state, casting, ordering, storyboard persistence, continuity, versioning/undo, isolation, upgrades | `backend/tests/test_m2.py` — **18 tests**; full suite **72/72 pass** |
| Database upgrade | `0004_m2` guarded for fresh + upgrade; packaged reinstall ran real `0003→0004` on installed DB (backend.log) |
| Packaged Windows verification | NSIS reinstall over M1.1 DB → `0003→0004` migration → character ×2 / scene / shot ×2 / cast command sequence committed via installed backend → storyboard endpoint returned scene + 2 shots + cast row with full state |
| Screenshots | `docs/m2-evidence/` — `01-storyboard.png`, `02-character-library.png`, `03-overview.png`, `04-character-editor.png` |
| Backup round-trip | Format v2 exports scenes/shots/cast/entity_assets; v1 packages still import | `test_backup_roundtrip_preserves_storyboard`, `test_format_v1_compat_roundtrip` (M1.1) |

## Bugs fixed during M2

- `_next_position` falsy-zero bug (`0 or -1` → -1): all new scenes landed at position 0 — fixed with explicit `None` check.
- Undo reinsertion passed ISO timestamp strings into `DateTime` columns — stripped on restore.
- `EntityPage` rendered structured `profile_json` values as `[object Object]` — `fieldText`/`fieldValue` JSON round-trip added.
- `cdp-shots.cjs` updated for M2 routes.

## Tracked outstanding items

See `docs/RELEASE_CHECKLIST.md` (items #5–#15 on GitHub). No M2 requirement is left open; tracked items concern install/VM verification, signing, UNC soak, FAT32 edge coverage, import resume, security review, scalability soak, installer/DB matrix, model approval workflow, first real adapter, and usability testing.

---

# Milestone 2.1 — Character State & Casting UX Hardening

Status as of the `feat/m2.1-casting` branch (builds on M2).

## A. Structured character-state validation

| Requirement | Implementation | Evidence |
|---|---|---|
| Versioned schema for shot-state fields | `app/domain/cast_state.py` — `CAST_STATE_VERSION = 1`; typed fields map to `shot_characters` columns; `ShotCastState` pydantic model documents the wire contract in OpenAPI | `test_state_defaults_when_missing`, schema in `/openapi.json` |
| Identity & reference IDs | `reference_asset_ids` list → `reference_ids_json` column (migration `0005`); every ID validated as a project asset | `test_reference_asset_ids_validated` |
| Wardrobe / expression / emotion / blocking / action / dialogue / voice | `TEXT_FIELDS` type-checked per field — non-strings rejected with `cast.state.<field>: expected a string, got <type>` | `test_state_rejects_wrong_types` |
| Model & LoRA refs | `model_ref` (str) + `loras` (list[str]); aliases `lora_refs`/`lora_json` normalised | `test_state_legacy_aliases_and_ext` |
| Continuity notes | `continuity` object + `continuity_notes`/`continuity_changes` legacy shorthands → `continuity.notes`/`changes` | `test_state_legacy_aliases_and_ext` |
| API + command boundary validation | `_check_cast_state` runs parse in `_validate` before any write; domain functions re-parse defensively; scene cast `role`/`notes` type-checked | `test_state_rejects_wrong_types`, `test_scene_cast_field_validation` |
| Forward-compatible extension fields | Unknown keys → `ext_json` column (preserved, remapped through backups, copied on duplicate/restore) | `test_state_legacy_aliases_and_ext`, `test_shot_duplicate_copies_state_columns` |
| Reject invalid cross-project refs | `reference_asset_ids` scoped per project in `_check_cast_state`; cast rows already scoped | `test_reference_asset_ids_validated`, `test_cross_project_cast_still_rejected` |
| Safe defaults + clear messages | Missing state → typed defaults; errors name the field + expected type | `test_state_defaults_when_missing` |
| Legacy data preserved | No `schema_version` ⇒ legacy v0, normalised the same; newer int versions parse tolerantly; M2-era keys (`action`, `voice`, `continuity_notes`) map onto canonical fields instead of being dropped | `test_state_legacy_aliases_and_ext`, `test_state_newer_schema_version_tolerated` |
| Schema evolution preserved | `schema_version` stamped on writes; partial updates only touch provided keys; explicit `""` clears | `test_state_partial_update_preserves_unset` |

## B. Scene-level casting interface

| Requirement | Implementation | Evidence |
|---|---|---|
| Assign/remove characters at scene level | `SceneCast` panel in scene dialog — add picker, role + scene-continuity fields, remove | `03-scene-dialog-cast.png` |
| Visual cast panel | Per-member card: chip, name, `→ N shots` heir badge, role + notes inputs | `03-scene-dialog-cast.png` |
| Shots inherit scene assignments | `storyboard()` read model emits per-shot `inherited` (scene members without an own row) + `source: "scene"/"shot"` markers | `test_scene_cast_inherited_by_shots` |
| Independent per-shot overrides | An own `shot_characters` row replaces inheritance for that character in that shot only | `test_scene_cast_inherited_by_shots` |
| Inherited vs overridden clearly visible | Shot cards: solid chips = own, dashed dimmed chips = inherited; shot dialog: "Inherited from scene cast" section with "Customize for this shot" + "override" badge on pending rows | `01-storyboard-inherited.png`, `02-shot-dialog-override.png` |
| Prevent accidental changes to other shots | Override writes an own row for THIS shot only; scene-remove confirm states heir count + that overrides stay | `test_scene_cast_remove_drops_inheritance_not_overrides`, ConfirmDialog in `SceneCast` |
| Identity + continuity preserved | Removing an override returns the member to `inherited` (scene row untouched); scene continuity notes field on cast rows | `test_scene_cast_inherited_by_shots`, `03-scene-dialog-cast.png` |
| Commands/audit/undo | All mutations via `scene.cast.*`/`shot.cast.*` commands; undo restores scene cast + downstream inheritance | `test_scene_cast_undo_restores_inheritance` |

## C. M3 preparation

| Requirement | Implementation | Evidence |
|---|---|---|
| Documented thumbnail pipeline | `docs/plans/M3_GENERATION_THUMBNAILS.md` — storage model, generation→asset→thumbnail flow, provenance fields, manual selection, auto-refresh via WS | doc file |
| No generation implemented | None — doc only, per scope | — |

## D. Testing & delivery

| Requirement | Evidence |
|---|---|
| Schema validation, legacy compat, inheritance, overrides, undo, isolation, migration, backup round-trip | `backend/tests/test_m21.py` — **17 tests**; full suite **89/89 pass** |
| Migration evidence | `0005_m21` guarded; `test_alembic_0005_upgrades_0004_db` builds a real 0004-shaped DB; packaged reinstall ran real `0004→0005` on the installed DB |
| Packaged Windows verification | NSIS reinstall over M2 build → `0004→0005` → scene cast + override + legacy-state normalisation + typed rejection all verified via installed backend |
| Screenshots | `docs/m2.1-evidence/` — storyboard with inherited chips, shot dialog cast section, scene dialog cast panel |
| Outstanding items | `docs/RELEASE_CHECKLIST.md` — all prior items retained; new Low item tracked (shot-level ref-ID picker UI, deferred to M3+) |

## Design notes

- **New columns:** `shot_characters.reference_ids_json`, `shot_characters.ext_json` — both flow through `_cols` serialization, duplication, revert-restore, backup v2 remap, and the storyboard API automatically.
- **Inheritance is computed, not copied:** scene cast never writes shot rows, so new shots inherit automatically and scene edits propagate without touching per-shot overrides.
- **Honest limitation:** `reference_asset_ids` has no shot-editor picker (tracked in checklist item 12) — it exists for generation adapters; the validation+storage+provenance path is proven via API tests.

---

# Integration — stacked milestones merged into `main` (M0 → M2.1)

On maintainer approval, the stacked PR chain was integrated into `main` with
merge commits in dependency order — no squash, no rebase; every milestone's
history is preserved:

| PR | Milestone | Merge commit | Contents verified |
|---|---|---|---|
| #2 | M0 / M0.1 | `9263024` | App shell, frozen sidecar, installer |
| #3 | M1 | `e5c0294` | Projects, bibles, CAS, backups, jobs |
| #4 | M1.1 | `00d42c7` | Backup destinations, progress, cancel, recovery |
| #16 | M2 | `0229e52` | Scenes, shots, casting, storyboard, backup v2 |
| #17 | M2.1 | `449ce0a` | Cast-state schema v1, scene cast UX, M3 doc |

Post-integration verification **on `main`** (`449ce0a`):

- Backend suite: **89/89 pass**.
- `tsc -b && vite build`: clean.
- Migration check: fresh DB → head `0005_m21`; `reference_ids_json` +
  `ext_json` present on `shot_characters`.
- Packaging: frozen sidecar + NSIS rebuilt from `main`, installed over the
  existing install; installed DB intact (project rev 18, scene cast +
  inherited rows served correctly); invalid typed state still rejected with
  `cast.state.expression: expected a string, got int (42)`.
- CI: `tauri-check` gap fixed (sidecar resource dir stubbed — real binary is
  only needed at bundle time, which the `package` job already performs).

---

# M3 Phase 1 — real-inference foundation (no weights downloaded)

Implemented on branch `feat/m3-generation` after maintainer authorization
against integrated `main` (`44da44f`). Covers M3 sections A/E.1/E.2/B/C/D/F/G
**architecture and code paths only** — per the approval gate, **no model
weights or runtime binaries have been downloaded**; the proposed first model
is documented in `docs/plans/M3_MODEL_PROPOSAL.md`.

| Requirement | Where | Evidence |
|---|---|---|
| Real local inference adapter (isolated, cancellable, capability-declared) | `app/adapters/inference.py` — `SdCppAdapter`: subprocess `sd` worker, stream-parse progress, terminate/kill on cancel, friendly OOM/backend errors | `test_sdcpp_e2e_fake_worker` (real subprocess → PNG → asset → thumbnail); `test_sdcpp_cancel_kills_worker` |
| Model catalog (name/version/source/license/requirements/capabilities) | `app/domain/model_catalog.py` — 5 model specs + sd.cpp runtime spec | `test_catalog_integrity`; `/api/models` |
| Compatibility engine (compatible/limited/unsupported/unknown/restricted/unavailable + estimates) | `app/domain/compatibility.py` — usable-VRAM policy, headroom, suggestions | 5 classify tests |
| Hardware detection (per-GPU VRAM incl. >4 GB registry fix, free VRAM, Vulkan/CUDA/DirectML, refresh) | `app/domain/hardware.py` | `/api/hardware?refresh=true` on the packaged build |
| GPU scheduler (auto/manual, per-GPU slots, VRAM fit, multi-GPU lanes) | `app/domain/gpu_sched.py`; engine lanes in `app/domain/jobs.py` | 6 scheduler tests incl. simulated dual-GPU; **simulated, not physically verified** |
| Model install/remove (resumable, verified, approval-gated, separate storage root) | `app/domain/model_files.py`, `ops.model.download` adapter, `POST /api/models/{key}/install|remove` | `test_model_install_download_job` (faked network, real job+row+files) |
| Generation workspace (capability-driven UI, GPU select, shot assign, history) | `apps/desktop/.../GeneratePage.tsx`, Model Manager rewrite | `tsc -b && vite build` clean; screenshots TBD in Phase 2/3 |
| Storyboard thumbnails via audited command | adapter `apply_command` → engine commits `shot.update` (origin `generation_job`) | e2e test asserts `thumbnail_asset_id` set |
| Provenance | `Asset.provenance_json` (adapter/job/model/request) + version `media_json` (prompt, seed, params, is_mock) | e2e test asserts both |
| Migration 0006 | `model_installs`, `runtime_installs`, `generation_jobs.gpu_index/model_key`, `project_id` nullable for app-level jobs | fresh DB → `0006_m3`; real `0005→0006` upgrade on the installed-app DB copy (NOT NULL→NULL, data preserved); `test_alembic_0006_tables_exist` |

**Suite:** 115/115 pass (89 carried + 26 new). **Simulated-vs-verified ledger:**
single-GPU e2e pipeline proven with a fake `sd` worker (`ATOMIC_SDCPP_CMD`
override); multi-GPU scheduling proven only on fabricated probes — this
machine has one GPU; all VRAM numbers are labelled estimates in the API/UI.
Real weights + real runtime verification is Phase 2, gated on approval.


## M3 Phase 3 — measured backend selection (2026-11-09)

| Requirement | Where | Evidence |
|---|---|---|
| B.2 CUDA comparison | blocked — `cudart` dep needs separate approval (563 MB) | `docs/m3-evidence/bench.json`; 6 fail-fast jobs, no assets leaked |
| B.3 measured-over-assumed selection | `SdCppAdapter._pick_flavor`: manual → measured → installed → detected; reason in provenance | live: `"selection_reason": "fastest measured backend (median 7.8s over 5 runs)"` on default request |
| Manual backend override (advanced UI) | Generate page Backend dropdown (installed flavors) | `docs/m3-evidence/generate-backend-selector.png` |
| Queued-job reason | `job.message` = scheduler `last_reason` ("no device fits — needs ~X GiB…") | live 8192² job message observed |
| Provenance in asset list | `GET /api/projects/{id}/assets` joins version `media_json` + `provenance_json` | live response + `test_asset_list_exposes_provenance` |
| Pinned runtime release | `meta_json.resolved` = `{asset,url,size,tag,pinned,resolved_from,resolved_at}` | `sdcpp-cuda` row: tag `master-1073-991f825`, `pinned: true` |
| Runtime-only install | `POST /api/runtimes/{key}/install` (no model weights) | CUDA flavor installed via endpoint, Vulkan preserved |

## M3 Phase 4 — CUDA comparison + hardening (2026-11-09, final)

| Requirement | Where | Evidence |
|---|---|---|
| B.2 controlled CUDA-vs-Vulkan benchmark | identical params both backends, 5 warm runs + prime, VRAM/util via nvidia-smi | `docs/m3-evidence/bench2.json`: Vulkan 7.24s median vs CUDA 8.83s |
| cudart dependency (approved) | app-local extract into `sdcpp-cuda/`; provenance in `meta_json.supplements` | sha256 `fe203668…0f38d`; `sd-cli --help` OK |
| B.3 measured auto-selection | `_pick_flavor`: manual > operational+measured(this GPU) > operational > detected | live: `"fastest measured backend (median 7.8s over 5 runs)"` → vulkan; manual cuda → succeeded |
| Only-verified-backends eligible | `smoke_check_runtime` at install + lazy; status→`error` + actionable message | `test_pick_flavor_excludes_broken_installs` |
| Realistic VRAM estimates | `sqrt(px_ratio)` activation scaling | `test_estimate_vram_realistic_scaling`; 8192²→24.6 GiB |
| Benchmark scope | `benchmark.{gpu,runtime_version,params}`; GPU-name match required | `test_benchmark_scoped_to_recording_gpu` |


## M4.0 Phase 1 — video-generation foundation (no weights downloaded)

Architecture + contracts only; the model download gate is
`docs/plans/M4_0_MODEL_PROPOSAL.md`. Suite: **144/144**.

| Requirement | Evidence | Where |
|---|---|---|
| Modular video adapter | `gen.video.sdcpp` (`SdCppVideoAdapter`) — vid_gen argv, multi-component resolution, `--vae-on-cpu` on non-CUDA flavors, `.webm` commit-on-success | `backend/app/adapters/inference.py` |
| Multi-component models | `ModelFile.role`; per-file download/verify (`verify_component`); install completes only when ALL components verify; `installed_component()` role lookup | `backend/app/domain/model_files.py`, `model_catalog.py` |
| Verified video catalog entry | `wan21.t2v.1_3b` — 3 real files, exact bytes + full sha256, Apache-2.0, support=`experimental` (NOT generatable until validated) | `backend/app/domain/model_catalog.py` |
| generate.video command | validation (modality, support tier, components present, i2v image in-project, frames bounds, device); job request incl. CAS-resolved `image_path` | `backend/app/domain/commands.py`, `schemas/api.py` |
| Recovery contracts (M4.1-facing) | `attempt_no`, `execution_scope` (local/remote), `recovery_state` on `generation_jobs`; reconcile marks interrupted + bumps attempts; adapter profile declares `worker_model`/`resumable`/`recoverable_outputs` | migration `0007_m4`, `db/models.py`, `domain/jobs.py`, `adapters/base.py` |
| Video validation | `media_probe` — ffprobe discovery, container-signature fallback, `probe_unavailable` honesty in provenance | `app/domain/media_probe.py` |
| Beginner UI | Generate page Image/Video tabs; `VideoGeneratePanel` (model/prompt/i2v picker/frames/shot attach, progress, `<video>` preview) | `apps/desktop/src/features/workspace/` |
| No unapproved downloads | `support` tiers gate generation; Wan stays `experimental` until real-weight validation | tests `test_m4.py`, `test_m3.py` |

## M4.0 Phase 2 — real Wan 2.1 video validation (2026-11-09)

| Requirement | Evidence | Where |
|---|---|---|
| Approval-gated download | only the 3 approved artifacts; per-file sha256 verify before `installed` | `model_files.verify_component`, install job |
| Real T2V on Vulkan + CUDA | 17 f @ 832×480/24 fps WebM/VP8, 17/17 frames decode-clean (PyAV); Vulkan 33.7 min, CUDA 2.6 min | jobs `196a8e88`, `7c2f6859`; `docs/evidence/m4/` |
| Output commit boundary | asset registered only after probe-validate; cancelled job → no asset, temp purged | `adapters/inference.py`, jobs `9be261e8` |
| Cancellation | job → `cancelling` → `cancelled`; sd-cli terminated; no asset | live test 9be261e8 |
| Restart recovery | mid-flight job reconciled `failed | interrupted…not resumable`; no duplicate asset | live restart, job `d7beacb9` |
| Support tier gating | `experimental`→`tested` only after real decode-validated clips | `model_catalog.py` wan21.t2v.1_3b |
| Retry-hardened downloads | bounded in-stream resume on flaky connections | `model_files.download_to` |

Suite: **144/144**.

## M4.0 Phase 3 — resource safety + packaged integration (2026-11-09/10)

Resource-safety system built in response to issue #28 (orphaned-worker
resource-exhaustion incident). See `docs/plans/M4_0_RESOURCE_SAFETY.md`.

| Requirement | Evidence | Where |
|---|---|---|
| Fail-closed worker supervision | sd-cli spawned suspended, assigned to a Job Object with `KILL_ON_JOB_CLOSE` before resume; assign failure = never launched; orphan reaper on backend exit | `domain/proc_guard.py`, `adapters/inference.py` |
| Memory admission control | preflight estimates per residency mode (`cpu_offload`/`gpu`/`te_cpu`/`disk`) using real component bytes + activation overhead; blocks on insufficient RAM, commit headroom, or VRAM incl. existing reservations; `warn` requires `memory_risk_acknowledged` | `domain/jobs.py`, `domain/compatibility.py`, `domain/gpu_sched.py` |
| Runtime watchdog | samples avail-RAM/commit/VRAM + worker RSS every interval; critical-threshold breach = graceful cancel → bounded tree kill | `domain/jobs.py` watchdog |
| Post-job hygiene | dead-check verifies worker+tree gone; RAM/VRAM reservations released in `finally`; partial output deleted before asset registration | `domain/jobs.py`, `adapters/inference.py` |
| OOM diagnostics | nonzero worker exit tagged `[flavor/residency]`; OOM → friendly error, no asset, no auto-retry | `adapters/inference.py` |
| Production override hygiene | `ATOMIC_ALLOW_EXPERIMENTAL_GEN` absent in frozen builds; Wan moved to `tested` so no override is needed for it | `adapters/inference.py`, `model_catalog.py` |
| Real-hardware gate validation | live preflight BLOCK on the 16 GiB machine: 4.75 GiB avail vs ~5.8 GiB B2 estimate (clean threshold 9.8 GiB); no worker launched; recorded on #28 | `docs/plans/M4_0_RESOURCE_SAFETY.md`, issue #28 |
| Packaged video playback | frozen sidecar serves the validated Wan clip (`200`, `video/webm`, 447 KB) and signature-validates media | `domain/media_probe.py`, asset file endpoint |
| GPL hygiene | PyAV wheel bundles x264/x265 (`--enable-libx264/x265` embedded in shipped DLLs) → `av` excluded from frozen installer; dev/test dep only; production probes via container-signature fallback | `backend/atomic-backend.spec`, `docs/RELEASE_CHECKLIST.md` |

Suite: **167/167** · tsc + vite clean · frozen backend smoke (migrations,
auth, API) · NSIS installer 27.67 MiB without `av.libs`/x264/x265.

## M4.0 merged (2026-11-10)

Merged into `main` via **PR #29**, merge commit
`b5c6af889b5adbdb1433a02b85ea286608f48a0b`. CI green on merge: backend
3.12 + 3.14, frontend, package, tauri-check. Internal development
milestone only — **not a public release**.

Open blockers carried forward (all stay open):

- **#28 — critical**: safety preflight refusal validated on real
  hardware; real Wan inference under the new safeguards NOT yet
  validated; B2/B3 residency modes unverified on hardware. Blocks
  unrestricted video-generation validation and public release.
- **#22** CUDA/NVIDIA redistribution (release blocker) · **#23** M4.1
  persistent workers · **#24** worker stderr diagnostics · **#25**
  multi-GPU hardware validation · **#26** VRAM calibration · **#27**
  catalog expansion · pre-existing #5–#11, #14, #15.

## M4.0.1 — production media engine (issue #31, 2026-11-09)

| Requirement | Evidence | Where |
|---|---|---|
| Approved LGPL binaries | BtbN `n9.0.2-48-gd4070a14ce` lgpl-shared, sha256 `b0981aea…2a6` (3-way verified); no `--enable-gpl`/`--enable-nonfree`; x264/x265 disabled | `backend/ffmpeg-manifest.json`, `scripts/fetch-ffmpeg.ps1` |
| Centralized media service | `binaries` (env→bundled→PATH + toolchain provenance), `supervise` (proc_guard Job Objects, fail-closed, timeout, cancel), `policy` (ValidationSpec tolerances), `engine` (probe/decode/validate) | `backend/app/domain/media/` |
| Full-decode validation | ffprobe metadata → `ffmpeg -xerror -f null` full decode → `nb_read_frames` exact count → duration tolerance; signature never fully validates | `engine.validate()` |
| Commit-boundary wiring | generation jobs validate before CAS registration; frozen builds REQUIRE `fully_validated`; provenance records mode + checks + toolchain | `adapters/inference.py` |
| Process safety | argv-only, `-nostdin`, bounded timeouts, kill-on-close, tree terminate, unsupervised spawn impossible | `media/supervise.py`, `proc_guard.py` |
| Packaging | `vendor/ffmpeg` staged by fetch script → `datas` → `_internal/ffmpeg/`; licenses+NOTICE+BUILDCONF shipped; installer 74.42 MiB | `atomic-backend.spec`, `build-sidecar.ps1` |

Suite: **188/188** (21 new media tests).

Merged into `main` via **PR #32**, merge commit
`15181efc061db272a38594a4b118bb20a50a85ca` (issue #31 closed).
CI green on merge: backend 3.12/3.14 (with approved-FFmpeg fetch),
frontend, package (incl. staged-decoder fail-closed check), tauri-check.
Internal development milestone only.
