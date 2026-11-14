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
