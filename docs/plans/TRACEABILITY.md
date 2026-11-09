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
