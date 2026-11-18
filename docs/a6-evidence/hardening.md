# A6 final hardening notes

## Atomicity contract (A6-F2)

`assembly.apply` commits inside the command transaction: the whole
`apply_plan` walk — clip removals, clip inserts, track creation, undo
stack collapse, plan/item row updates — is flushed together and the
endpoint calls `db.commit()` only after the command returns. Two
failure classes, two guarantees:

- **Catastrophic failure** (any exception other than a per-item
  `TimelineError` — DB errors, unexpected `RuntimeError`): propagates,
  the commit is never reached, SQLite rolls back the entire command.
  Guaranteed state: zero partial clips, zero applied records, no undo
  entry, plan stays `draft`. A clean retry applies fully and a second
  retry is an audited no-op.
  Proof: `test_apply_failure_rolls_back_everything` injects a
  mid-apply `RuntimeError` on the second `insert_clip` and asserts all
  of the above on a fresh session.
- **Per-item placement failure** (`TimelineError` from
  `timeline.insert_clip` — lane collision, unusable geometry): the
  designed graceful outcome, NOT partial corrupt state. The item is
  recorded in `applied_json.skipped` with its reason, everything else
  commits, the plan is `applied`. The skip is a durable, auditable
  record — and retry-safe: a skipped item has no managed clip, so a
  subsequent apply (after re-plan) re-attempts only it while clean
  items are kept. Proof: `test_single_insert_failure_records_skip`.

Restart reconciliation: a crash mid-apply leaves either nothing
(rollback) or a fully committed state whose `applied_json` is
authoritative — there is no third "half-committed" state to heal.

## Conflict approval scope (A6-F3)

`resolve_conflicts: "replace"` requires `replace_keys` naming the
exact items approved for replacement. Enforcement:

- Every key in `replace_keys` must be a *previewed* conflict — either
  a plan item carrying `flags.conflicts` or an orphaned managed clip
  (item left the plan). Any other key → the apply is refused before
  any mutation (`test_replace_keys_rejects_unflagged_key`).
- Flagged conflicts not named in `replace_keys` are skipped with
  reason "conflict with manual edits — not approved"
  (`test_replace_keys_scope_to_previewed_conflicts` exercises a mixed
  approve-one/skip-one apply).
- A manual edit made *after* the plan's preview cannot be approved
  into existence: it drifts the timeline signature, `_is_stale`
  refuses the apply, and the user must re-plan — producing a fresh
  preview in which the new conflict is explicitly flagged.
  Proof: `test_post_plan_timeline_edit_refuses_apply`,
  `test_manual_edit_after_apply_stales_plan`.
- Duplicate apply is an audited no-op
  (`test_duplicate_apply_is_clean_noop`); concurrent attempts
  serialize on the single-writer command transaction — the loser sees
  either the applied no-op or a stale refusal, never a double-apply.

## Source freshness (A6-F4)

`Plan.sources_json` fingerprints, per shot: shot id/position/revision,
selected motion-clip id/position/revision/**result asset id + live
asset status** (added this hardening — a deleted or archived result
now stales the plan at plan level, not only per item), clip
`duration_frames` + rational `fps`, dialogue id/position/revision/
result asset id + status/`timing_json`, shot-linked audio link
records; plus scene order/revisions and the full timeline signature
(occupancy + manual edits). `apply_plan` refuses when the live
fingerprint drifts; the plan's *own* apply is exempted only via the
`timeline_after` signature recorded in the same transaction as the
apply — matching that exact post-apply state, nothing broader.
Proof: `test_apply_refuses_stale_sources`,
`test_deleted_result_asset_stales_plan`,
`test_post_plan_timeline_edit_refuses_apply`, plus the 17→19-step
installed acceptance (step 10 rejects a stale approval on-device).

## Undo/redo + review preservation (A6-F5)

The apply collapses every timeline mutation it performs into a single
"Applied first-cut assembly" undo entry (`test_apply_undo_via_
timeline_one_step`, `test_undo_restores_exact_pre_apply_state` — the
latter asserts clip-for-clip identity of the restored timeline). A
manual edit after apply stales the *plan* (see F4) so an unsafe undo
of plan state cannot silently run. Review votes/notes are rows on
`AssemblyItem` keyed by stable `item_key` — they survive re-planning
(`test_review_votes_and_notes_survive_replan`), restart
(`test_restart_persistence`), and backup round-trip
(`test_backup_restore_roundtrip`); removed sources keep their review
rows with a `removed` flag rather than vanishing.

## Load-flake log (A6-F1)

Two recurring load-sensitive failures observed in full-suite runs,
both pre-existing and unrelated to A6 code paths; both pass in
isolation. Tracked, not suppressed:

| Test | Signature under suite load | Standalone |
|---|---|---|
| `tests/test_c15_watch_polling.py::TestDisableReenableRace::test_process_exit_is_bounded_with_stalled_scan` | watch-polling probe assertion ("scan was not scheduled") under CPU saturation | PASS (~14 s) |
| `tests/test_c4e_resources.py::test_disk_preflight_blocks_video_job` | `wait_for_job` 15 s timeout under suite saturation | PASS |

Recurrence: observed in the pre-PR full-suite run on `9c7ea64` and
re-verified standalone-green; the final full-suite result on the
hardened head is recorded in `a6-acceptance.txt`.
