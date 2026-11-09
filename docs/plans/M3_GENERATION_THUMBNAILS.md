# M3 preparation — generated images → storyboard thumbnails

How locally-generated images will flow into shot cards as thumbnails.
**No generation is implemented yet** — this document defines the pipeline so
M3 adapters land on prepared hooks rather than new plumbing.

## 1. Storage model (already in place)

- `shots.thumbnail_asset_id` → `assets.id` (FK, `SET NULL`). A shot thumbnail is
  a *reference to an asset*, never a copy — so when the asset gains a newer
  version, the thumbnail follows automatically.
- `assets` / `asset_versions` / `asset_files` (CAS) hold the media.
  `asset_versions.media_json` is the open-ended provenance bag.
- `shot.update` already accepts `thumbnail_asset_id` — manual thumbnail
  selection works today via the shot editor's "Thumbnail" dropdown.

## 2. Generation → asset → thumbnail (planned flow)

1. A **generation job** (future adapter, e.g. `gen.image.still`) runs against a
   shot. The job's `output_asset_id` points at the produced asset.
2. On success, the adapter writes the output into CAS, creates
   `asset_versions` with `media_json` provenance:
   `{adapter_id, model, loras, seed, prompt_snapshot, cast_state_digest,
   source: "generated"}` — a shot card can later answer "what made this?"
3. The adapter then commits `shot.update {thumbnail_asset_id}` (or a dedicated
   `shot.set_thumbnail` alias) — going through the command layer so the change
   is audited, versioned, and undoable like every other mutation.
4. `job.progress` → `succeeded` events already trigger `refreshProjectData()`
   in the app shell; the storyboard re-renders with the new thumbnail without a
   page reload. **Automatic thumbnail refresh is therefore free** — the WS
   event bus already pushes it.

## 3. Manual thumbnail selection (already shipped)

- Shot editor → "Thumbnail (image asset)" dropdown lists project image assets.
- Clearing it passes `null` → `thumbnail_asset_id` unset (`SET NULL` safe).
- The shot card falls back to the shot-type badge when no thumbnail is set.

## 4. Provenance & trust rules

| Question | Answer |
|---|---|
| Which version shows? | `assetFileUrl` serves the asset's current `ready` version — newest wins automatically. |
| Can a generated image be replaced? | Yes — generation creates a new `asset_versions` row; the thumbnail follows. Version history keeps the old one. |
| Manual pick vs generated? | `media_json.source` distinguishes `"generated"` vs `"imported"`/`"manual"`; UI can badge generated thumbnails later. |
| Cross-project thumbnails? | Rejected — `_check_storyboard_links` validates `thumbnail_asset_id` belongs to the project. |
| Deleting the asset? | `SET NULL` clears the thumbnail; the card reverts to the type badge. No dangling refs. |

## 5. What M2.1 deliberately does NOT do

- No image generation, no adapter work, no model downloads.
- No automatic assignment of generated assets to shots (adapters will do this
  explicitly through commands, not by convention).
- No thumbnail caching/proxying — CAS reads are local and cheap; proxies are an
  `asset_versions` variant for a later milestone.
