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
