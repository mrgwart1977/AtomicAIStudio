# A8 hardening notes

## Design invariants

1. **No parallel persistence.** Wizard progress is `settings_json.wizard`
   on the project row — written exclusively through
   `project.update_settings` (preview → commit → audit → undo). Stage
   truth is re-derived from live rows on every `/wizard` read; nothing
   the wizard stores can disagree with the data.

2. **Mode is a view flag.** `settings.wizard.mode` ∈ {beginner,advanced}
   selects presentation depth only. Switching writes no entity rows —
   verified in `test_mode_switch_preserves_rows` (backend) and the B7
   installed check (keyframe revision map identical before/after).

3. **Honesty is derived, not asserted.** Generation-state labels
   (planned/draft/simulated/generated/missing/failed/cancelled/stale/
   approved) are computed from `result_asset_id` + job row + revision
   timing at read time. A badge cannot claim "generated" without a live
   result asset; mock adapters can only ever surface `simulated`.

4. **Legacy never silently upgraded.** `settings_json.first_film` is
   read to seed a v2 blob (nearest stage) + a UI notice; the old key is
   never rewritten or deleted. `legacy_first_film` surfaces in the
   derived payload so the UI can disclose the jump.

## Bugs found & fixed during A8

- **`backup.import` dropped packaged `settings_json`** (pre-existing):
  the package-import stub kept `{"import_source": …}` only — provider
  overrides, wizard progress, any user settings were silently lost on
  restore-into-new-project. Fixed in `backup.py` `project_id_override`
  path: source settings now merge over the stub, preserving
  `import_source` provenance. Covered by
  `test_backup_restore_preserves_wizard_settings` + 73 backup
  regression tests.

## Deliberate boundaries kept

- `live_gate.dispatch_admission` — untouched (zero diff).
- Issue #96 — still the mandatory live-retrieval blocker; A8 adds no
  retrieval path.
- `GET /wizard` — read-only; no mutations, no writes.
- No real inference anywhere: every wizard generation commit targets
  mock adapters whose results are labeled simulated.
- Screenplay import: one documented plain-text paste format only; the
  parser keeps unknown lines as action text (nothing dropped) and never
  claims Fountain/FDX/PDF support.
- Reviewed head will be stamped into `docs/a8-evidence/acceptance.md`
  after the installed run.

## CI notes

- `test_edl_v2_serialization_scales` perf-bound flake class (observed
  at A7 on py3.14) is unrelated to A8 — no A8 code touches the EDL
  serialization path.
