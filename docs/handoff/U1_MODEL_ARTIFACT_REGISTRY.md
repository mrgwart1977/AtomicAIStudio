# U-1 — Universal Model Artifact Registry (amendment to v0.9)

Additive foundation for the Universal Model and LoRA Manager. This
document records the schema invariants and the planned cutover; it
extends the spec of record and removes nothing.

## What U-1 ships

- `model_artifacts` table (migration `0027_u1`) — additive, idempotent.
- `app/domain/model_artifacts.py` — catalog sync, install linking,
  read-only view serialization, external-registration helper.
- `GET /api/models/artifacts` — read-only registry view (declared before
  `/api/models/{model_key}`; `install` projected from `model_installs`).
- Model Manager: read-only "Universal Model Library" section.
- `tests/test_model_artifacts.py` — offline regression matrix.

## Identity model — three distinct layers

| Layer | Column | Rule |
|---|---|---|
| Artifact identity | `artifact_id` (PK) | `catalog:<model_key>` for catalog-seeded rows; reserved prefixes `import:` `disc:` `train:` for future sources so no user/imported row can collide with or shadow a catalog artifact. Never derived from content bytes. |
| Content identity | `content_sha256` | Full 64-char SHA-256 of the primary file when the catalog declares it; NULL when undeclared. Indexed for duplicate detection. Two artifacts may share content bytes and remain distinct registry entries (distinct source/version). |
| Installation identity | `install_id` | Association to the current `model_installs` row for the catalog key. Install *state* (status, verified, error) is projected at read time — never copied onto the artifact. |

## Sync invariants

1. `sync_catalog` only creates/updates rows with `artifact_id`
   `catalog:*` AND `source_type="catalog"`. Every other row is out of
   scope by construction.
2. Only catalog-owned fields refresh on re-sync (`display_name`, `kind`,
   `arch`, `family`, `modality`, `fmt`, `role`, `version`, `byte_size`,
   `content_sha256`, `license_json`, `support_tier`, `meta_json`). User
   metadata (`enabled`, `tags_json`) is written once at insert and never
   re-touched by sync.
3. `managed_by="external"` rows are never modified, linked, or deleted
   by catalog synchronization — and no future maintenance path may touch
   their files.
4. `link_installs` updates only `install_id`/`uri` on `catalog:*` rows;
   it never writes to `model_installs`.
5. `model_installs` remains the authoritative install record. The
   artifact table is a read-only projection — no dual-write.
6. Kind mapping: `modality=="lora"` → `adapter`; primary-file role in
   {vae, text_encoder, clip, t5, upscaler} → `auxiliary`; else `model`.
   `arch` = catalog `family` for catalog rows; `unknown` reserved for
   uninspected imports.
7. Adapter role is `lora:<identity|style>` from
   `capabilities["lora_role"]`.

## Preserved gates (explicitly unchanged)

B-1 LoRA limits (≤2, identity+style, SD 1.5), production/tested
execution eligibility, experimental install-vs-generation split,
license/integrity gates, runtime selection, GPU scheduling, install
workflow, backup scope (`model_artifacts` is app-level — not in
`ENTITY_TABLES`).

## Future cutover strategy (documented, NOT implemented)

- **U-2 (secure import)**: `ops.model.import` writes
  `artifact_id="import:<sha12>-<seq>"` rows with `source_type="import"`,
  `managed_by` set by storage location, `support_tier="unknown"`.
  `ModelInstall` stays authoritative for catalog installs; imported
  artifacts carry their own `install_*`-equivalent state in a new table
  (decision deferred: extend `model_installs` with `artifact_id` column
  vs. a separate `artifact_installs` table).
- **U-3 (discovery)**: `disc:` rows, `managed_by="external"`, populated
  by read-only scans.
- **U-4 (compatibility engine)**: consumes `arch`/`family`/`fmt`/`role`
  + `meta_json.requirements`; verdicts are computed views, not stored
  state.
- **U-5 (multi-adapter schema)**: `cast_state` v2 entries gain optional
  `role`/`order`; v1 storage round-trips unchanged.
- **Cutover point**: only after U-2–U-5 stabilize may a separately
  approved migration make `model_artifacts` authoritative for install
  state; `model_installs` would become a compatibility view until fully
  retired. Any such cutover requires its own milestone approval.

## U-1 limitations (honest)

- Registry contains catalog artifacts only — import, discovery, and
  training sources are contract-defined but unpopulated.
- `arch` currently equals catalog `family` — true architecture detection
  arrives with import inspection (U-2).
- No compatibility engine, runtime selection, ComfyUI execution, or
  generalized multi-LoRA behavior — all deferred.
