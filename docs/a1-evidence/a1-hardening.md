# A1 final hardening — evidence notes

Records the behaviors verified in the A1 final review (F2–F5) and the
as-built limitations. All behaviors are enforced by backend tests in
`backend/tests/test_a1_screenplay.py`.

## F2 — synchronization freshness

A `shotlist.sync` commit never replays a stale preview: the diff is
recomputed at commit time, and a commit pinned to a pre-edit project
revision is rejected with 409 (`test_sync_freshness_stale_preview_rejected`).
A shot edited after the preview is classified `manual_edit` and skipped
unless explicitly approved via `approve_overwrite`.

## F3 — manual-edit preservation

The scaffold-managed set is exactly: shot title, shot_type, description,
location_id, and dialogue-carrying cast rows (fingerprinted in
`continuity_json.shotlist`). Everything else is user-owned and survives
any approved overwrite: prompt, intent, duration_sec, negative prompts /
generation params held in `continuity_json`, thumbnail/source/reference-set
bindings, and non-scaffold cast rows plus user fields on scaffold rows
(`test_user_fields_preserved_on_approved_overwrite`).

Cast-row removal is ownership-checked: a row is deleted only when it is
provably scaffold-owned (its dialogue equals the recorded `cast_spec`
entry) or entirely empty. User-added cast rows are never removed.

## F4 — identity, archiving, deletion

- Shot identity = the SET of element ids in a beat (order-insensitive);
  reorder within a run produces an update, never remove+add.
- Manually archived scaffolded shots are reported in
  `skipped_archived`, never silently recreated
  (`test_archived_shot_not_recreated`).
- Speaker changes update cast rows in place; a speaker with several
  lines in one beat collapses to a single cast row (UNIQUE
  `(shot_id, character_id)`).
- Deleted characters/locations resolve via FK `SET NULL`; the diff
  tolerates unresolved speakers.

## F5 — undo/redo safety and the LINEAR limitation

- `shotlist.undo` skips (never clobbers) a shot whose revision no longer
  equals the `post_revision` the batch recorded — later independent
  edits win (`test_undo_skips_later_independent_edit`).
- `shotlist.redo` re-applies only while a shot still sits at the exact
  revision the undo produced (recorded as `undone_revision` on the
  batch inside the undo command's audit). Older batches without the
  annotation fail safe by skipping.
- Batches live in durable command-audit rows — undo survives a backend
  restart (`test_restart_between_sync_and_undo`).

**Limitation: undo/redo is strictly linear.** `shotlist.undo` walks the
committed scaffold/sync history newest-first and reverses the latest
outstanding apply; `shotlist.redo` re-applies the latest outstanding
undo. There is no per-shot or per-branch undo — undoing an older apply
requires first undoing every newer apply on the shot list. Individual
shots retain their own full `entity_revisions` history and can be
reverted point-in-time via `shot.revert` independently of the batch
chain.

## Known flake — `test_process_exit_is_bounded_with_stalled_scan`

Tracked defect (release checklist #14); NOT fixed, NOT suppressed.
Two observed signatures of the same probe-design race:

| Where | Signature |
|---|---|
| CI run 44019183542 attempt 1, backend 3.12 | `TimeoutExpired` — probe process hung >30s exiting with a stalled scan |
| Local full-suite run, A1 hardening head | `AssertionError: scan was not scheduled` at `_watch_exit_probe.py:51` |

Root cause of the second signature: the probe subprocess runs the real
`ImportWatchService` unpatched; `svc._tick()` legitimately skips
scheduling when `self._pressure() == "critical"` (real host RAM, probed
via `psutil`). While the full suite ran under load, the pressure gate
paused scanning → `_pending` stayed empty → the probe's
`assert svc._pending` failed. The paused path is intended product
behavior; the failure is probe observability, not a product defect.

Rerun of the exact test on an idle box: **PASS** (3.04s).
