# S5 — Mask editor manual acceptance checklist (Windows, packaged app)

Run against the installed `Atomic Dream Engine` (or `npm run tauri dev`
with the dev backend on :8788). Every step uses an existing project
containing at least one image asset.

## Canvas and painting

| # | Step | Expected |
|---|------|----------|
| 1 | Cloud Studio → Inpaint mode → pick a source image | MaskEditor mounts; source renders fit-to-canvas |
| 2 | Paint a stroke with the brush | Overlay appears under the pointer at default opacity; stroke lands only inside image bounds |
| 3 | Mouse-wheel zoom in/out | Overlay stays pixel-aligned with the source at every zoom level |
| 4 | Shift-drag or middle-drag to pan | Mask geometry does not shift relative to the image |
| 5 | Paint at 4x zoom, then zoom out | Stroke covers exactly the painted source pixels — no scaling artifacts |
| 6 | Eraser over a painted region | Only erased pixels return to unmarked; deterministic, no ghost residue |
| 7 | Brush-size slider | Cursor ring resizes; painted diameter matches |
| 8 | Pen input (if hardware present) | Same behavior as mouse; no pressure-dependent drift |
| 9 | Opacity slider | Overlay transparency changes; exported mask unaffected |
| 10 | Undo (Ctrl+Z / button) ×N | Each step restores the exact prior bitmap |
| 11 | Redo ×N | Exact forward restoration; a new stroke clears the redo stack |
| 12 | Export with an empty mask | Export button disabled / refused — empty masks never submit |

## Export, job, and lineage

| # | Step | Expected |
|---|------|----------|
| 13 | Export & Import | Button busy-state; PNG at exact source dims → upload-source → asset.import durable job |
| 14 | Job completes | Mask auto-selected; notice appears; no manual refresh needed (WS-driven, bounded fallback) |
| 15 | Assets page | `painted-mask.png` present as a project-owned asset |
| 16 | Commit inpaint (BFL fill model) with the mask | Preview → cost shown → approve → job succeeds; output is a NEW asset |
| 17 | Source sha256 after generation | Unchanged — source and mask are never mutated |
| 18 | Output asset provenance | Records source + mask lineage |
| 19 | Rapid double-click on Export | Single job — busy gate blocks the second submit |
| 20 | Switch source image mid-session | Mask selection clears; stale approval fingerprint invalidates |

## Outpainting

| # | Step | Expected |
|---|------|----------|
| 21 | Outpaint mode → set larger target dims | Nine-anchor picker + placement preview matches backend `compute_pad` |
| 22 | Two different anchors (e.g. center, bottom_right) | Preview placement matches the resulting job's `pad` values |
| 23 | Undersized/non-expansion dims | Refused at preview and commit |
| 24 | Dims beyond 4096 or invalid aspect | Refused |
| 25 | Change dims/placement after approval | Approval fingerprint invalidates — re-preview required |

## C4 — reference sets, shots, and storyboard

| # | Step | Expected |
|---|------|----------|
| 26 | Character page → create a named reference set, link 2 reference images | Set appears with ordered thumbs; links carry set membership |
| 27 | Reorder refs / edit a weight / pin a seed | Order and values persist after reload; seed shown as guidance, not an identity guarantee |
| 28 | Character page → duplicate the character | Copy carries its sets + memberships; deleting a set ungroups links — assets are never deleted |
| 29 | Cloud Studio img2img → pick the reference set | Preview lists `resolved_references`; approve → job provenance records set id + resolved asset ids |
| 30 | Edit the set (add/remove/reorder/seed) after preview → commit the stale approval | Refused 422 — re-preview required |
| 31 | Storyboard → shot dialog → bind the set + a source image | Pickers show cast-scoped sets + project images only; binding persists on the shot row |
| 32 | Bind a set, then remove its character from the effective cast, then generate | Refused — invalid bindings never fall back to unrelated references |
| 33 | Shot dialog → Generate (simulated) → preview → approve | Cost shown; job runs through the normal engine; output asset appears linked to the shot |
| 34 | Preview shot.generate → edit the shot's prompt/cast → commit the stale approval | Refused 422 — shot-state changes invalidate the approval |
| 35 | Shot card after generation | Latest generated image on the card; status badge gone once succeeded; `✦n` output count |
| 36 | Inject/encounter a failed generation | Card shows the failed badge — never a pending-looking or succeeded-looking state |
| 37 | Reorder shots with ◀▶ buttons and keyboard arrows (no drag) | Order changes persist via `shot.move`; focus ring visible; cards are Tab-reachable |
| 38 | ⇄ prev on a shot card | Comparison dialog: shared cast, enters/leaves, wardrobe/expression diffs, location change, continuity-field diffs — worded as metadata only |
| 39 | Shot dialog → continuity guidance fields | Appearance/wardrobe/lighting/camera/mood persist in `continuity_json`; disclaimer visible |
| 40 | Restart the app → reopen the storyboard | Generation status, latest outputs, and bindings all restored from durable rows — no transient state |

## C4-D — shot video workflow

| # | Step | Expected |
|---|------|----------|
| 41 | Shot dialog → video section, no video model installed | Section hidden entirely — no dead control |
| 42 | Shot dialog → video: pick installed model, set frames/fps/motion → Preview | Backend cost + proposal shown; nothing runs until approve |
| 43 | Preview → edit the shot's prompt or source → approve the stale preview | Refused 422 — re-preview required |
| 44 | Shot with bound source image → video section | Copy says "image-to-video from the bound source frame"; a t2v-only model selection is rejected at preview |
| 45 | Approve → watch the shot card | Job badge shows video job status; on success a teal `▶n  xs` clip badge appears next to `✦n` — card never downloads the full clip |
| 46 | Fail/cancel a video job | Failed badge; clip count unchanged; a later success adds exactly one clip |

## Memory measurements

Undo history is bounded by `UNDO_MAX = 20` snapshots AND
`UNDO_MAX_BYTES = 64 MiB` (MaskEditor.tsx). Each snapshot is
`width × height × 4` bytes at source resolution.

| Source size | Bytes/snapshot | Max undo depth (byte cap) | Retained max |
|-------------|----------------|---------------------------|--------------|
| 512 × 512   | 1 MiB          | 20 (count cap)            | 20 MiB       |
| 1024 × 1024 | 4 MiB          | 16                        | 64 MiB       |
| 2048 × 2048 | 16 MiB         | 4                         | 64 MiB       |
| 4096 × 4096 | 64 MiB         | 1                         | 64 MiB       |

Representative check (2048² image, 20 brush strokes): retained history
holds only the newest 4 snapshots — memory stays ≤ 64 MiB regardless of
stroke count, oldest snapshots are released for GC. The offscreen mask
bitmap itself is one `w×h×4` allocation (16 MiB at 2048²) freed on
unmount.

**Limitation:** very large sources get a shallow undo stack (4 steps at
2048², 1 at 4096²). Stroke-vector compaction would restore deep undo —
deferred to a later stabilization PR; do not add a vector editor here.

## Remaining limitations

- jsdom cannot run canvas 2D, so pointer→pixel paths are covered by
  `editorMath` unit tests + the packaged API smoke, not automated UI
  interaction — this checklist is the manual cover.
- MaskEditor import uses the shared WS feed + bounded poll fallback
  (S4); a permanently offline socket still completes via fallback.
- Reference-image tray selection is covered in CloudImagePanel tests;
  per-model reference limits are enforced backend-side.
