# U-2b-2a — Managed-Copy Model Import (backend foundation)

Status: implemented, backend-only. Reference-only import stays the
default; managed copying is behind `ATOMIC_ENABLE_MANAGED_COPY` (OFF).
Approved baseline `de1170c`; migration head `0028_u2` — no schema change.

## What it does

`POST /api/models/import` accepts `mode="managed_copy"` (gated) in
addition to the default `mode="reference"`. A managed-copy job runs the
existing U-2a same-open-handle inspection, then stages + publishes the
verified bytes into app-managed storage:

    <models_root>/imports/<sha256>.<safetensors|gguf>      canonical
    <models_root>/imports/.staging/<job_id>-<nonce>.part   per-attempt

The artifact registers `managed_by="atomic"` with `uri` = the canonical
managed path (app-owned storage, safe to expose). `source_uri` keeps the
original source provenance; `import_report_json.copy` records
`{dest_rel, expected_sha256, verified_sha256, bytes_written,
provenance: "published"|"adopted", published_at, staging_left}` — no
absolute managed path is stored in reports or emitted in events.

## Filesystem invariants (contract §2)

1. Source files are opened read-only and never written, moved, or
   deleted. A second handle is fstat-compared to `registration_stat`
   before and after the copy (`path_changed` on drift).
2. Canonical destinations never receive direct writes — every byte
   lands in a unique staging file first.
3. Publication is `os.link(staging, dest)` only — atomic, create-only,
   no-overwrite. `os.replace` is forbidden.
4. `os.link` capability failures (PermissionError, EPERM/EINVAL/
   ENOSYS/EXDEV/EOPNOTSUPP and winerror 1/50/87/120/4390) refuse
   `managed_copy_unsupported`. No O_EXCL direct-write fallback.
5. Staging names are `<job_id>-<nonce8>.part` created with
   `O_WRONLY|O_CREAT|O_EXCL` mode 0o600 — fresh nonce per attempt, so a
   stale file is always foreign and never deleted.
6/7. Cleanup (`_unlink_if_owned`) unlinks only objects whose live
   fstat `{ino, dev}` still matches the identity recorded at creation.
   Name/sidecar alone never authorize deletion; ambiguous or replaced
   objects are left untouched (`staging_left` recorded).
8. No recursive deletion and no `.staging` sweep anywhere.
9. No canonical managed-file deletion anywhere in U-2b-2a.
10. An existing canonical destination is adopted only after a full
    content re-hash equals the inspected sha256 — the filename is a
    claim, never proof. Mismatch refuses `managed_dest_corrupt`.
11. Post-copy checks before publication: source identity re-bind,
    copied-stream sha256, and byte count vs `byte_size`.
12. All filesystem failures map to sanitized `ImportRefused` codes —
    no source or managed absolute paths in errors, events, or results.

`ctime`/`mtime` are deliberately excluded from ownership identity:
Windows legitimately bumps `st_ctime_ns` on our own writes and on
`os.link`; only `{ino, dev}` prove "same object we created".

## Process ownership

`model_copy.ensure_models_root_lock` runs before any managed mutation:

- Default layout (`models_root` inside `data_dir`): already covered by
  the startup `instance_lock` byte-range lock — no re-acquisition.
- Custom `models_dir`: `acquire_data_dir_lock(models_root)` is held
  lazily on first managed copy for the process lifetime
  (`_MANAGED_LOCKS`), released at shutdown and by the OS on crash.
- Lock contention → `managed_copy_unsupported`; inaccessible root →
  `io_error`. Ordinary startup is unaffected (acquisition is lazy).
- ops-lane serialization (one ops job in flight per process) plus the
  os.link collision contract order all writers.

## Feature gate

`settings.enable_managed_copy` / `ATOMIC_ENABLE_MANAGED_COPY=1`,
default OFF. Checked three times: route enqueue (`422
mode_not_supported`), adapter `validate()` at claim, and execution.
Queued-then-disabled jobs fail closed. Reference import, managed
artifact detail, and release all work with the flag OFF.

## Release

`POST /api/models/artifacts/{id}/release-managed-copy` — synchronous,
transactional, metadata-only:

- Requires `source_type="import"`; non-import artifacts → 422.
- `managed_by`/`uri` detach → `managed_by="external"`, `uri=NULL`;
  `import_report_json.release` records `managed_copy_released_at` and
  `last_uri_rel` (basename only). Source provenance retained.
- Idempotent (`already: true` on repeat). Never touches canonical
  bytes; physical deletion does not exist in this milestone. The
  response never claims disk space was freed.
- Not feature-gated — disabling managed copying cannot strand
  artifacts and cannot enable destruction.

## Detail fields

`GET /api/models/artifacts/{id}` adds `managed_copy` for managed
imports: `{present, dest_rel, provenance, published_at}` — `present`
is a live filesystem probe (missing/disconnected volume → false); the
DB row asserts ownership, not existence.

## Residual risks / platform limits

- Issue #178 stays open: the check→open ancestor TOCTOU is narrowed
  (handle-lifetime re-binds, reparse walks, create-only publish), not
  eliminated.
- `_unlink_if_owned` has a bounded fstat→unlink window (no stdlib
  handle-bound delete on Windows); it is confined to app-private
  `.staging/` per-attempt names and fails closed on any mismatch.
- Orphaned canonical/staging objects accumulate until a separately
  authorized cleanup phase; adoption re-verifies content before reuse.
- Windows hard-link semantics are tested through the real `os.link`
  path plus injected-capability failures; no POSIX-only mock stands in
  for Windows filesystem validation.

## Test coverage

`backend/tests/test_model_copy.py` — 40 synthetic-fixture tests:
happy path, source immutability, dedup, adoption, corrupt-dest
refusal, unsupported hardlinks, disk full, source mutation mid-copy,
staging replacement at publish, cancellation at multiple stages,
crash before/after publish, DB failure after publish, requeue
idempotency, concurrent ordering, lock contention, gate transitions,
release + retry, missing managed dest, privacy/sanitized errors,
no-ModelInstall/execution ineligibility, reparse/junction handling.
