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
