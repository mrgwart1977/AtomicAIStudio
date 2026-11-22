# U-2a — Secure User-Model Import (reference-only)

Backend/API-only foundation for registering user-owned model files by
**reference**. A file is inspected and hashed in place — never copied,
moved, modified, or deleted — and an `import:` artifact row records what
was seen at registration time. Imported artifacts are descriptive
registry entries only: `managed_by="external"`, `support_tier="unknown"`,
no `model_installs` row, and generation-ineligible under every existing
gate (B-1 LoRA rules, runtime selection, GPU scheduling, backups all
unchanged).

## Schema — migration `0028_u2` (additive)

`model_artifacts` gains three columns:

| column | type | semantics |
|---|---|---|
| `source_uri` | `Text`, NULL | absolute source path, set at insert, immutable. NULL on catalog rows. Detail-view only. |
| `imported_at` | `DateTime`, NULL | registration timestamp |
| `import_report_json` | `JSON`, NOT NULL | full sanitized inspection report (never contains the path) |

No `import_state` column: a row exists (registered) or it doesn't.
Cancellation/failure lives on the job. `uri` stays NULL on imports so
list responses carry no path. Downgrade drops exactly these columns.

## API lifecycle — always asynchronous

```
POST /api/models/import
  body: {path, license{name,url?,commercial_ok,notes?}, family?, role_hint?}

  request shape invalid / path refuses cheap checks → 422 {detail: <code>}
  same resolved source already queued/running          → 409
  otherwise                                            → 202 {job_id}
```

There is **no synchronous reuse path**: every import enqueues a durable
`ops.model.import` job that always performs a fresh full SHA-256 over the
opened handle. A completed job's `result_json` is:

```json
{"artifact_id": "import:<sha12>[:N]",
 "status": "registered" | "reused",
 "report": {fmt, sha256, byte_size, hashed_at, registration_stat,
            suggested_arch, suggested_role, tensor_count, metadata,
            warnings, evidence}}
```

`reused` is returned only when the freshly computed **full** hash AND the
resolved source identity match the stored row — stat fields are never
proof of content identity. Failures after queueing are structured job
errors (`job.error` carries the taxonomy code; `result_json.error`
carries `{code, detail}`), never HTTP errors.

`GET /api/models/artifacts/{artifact_id}` is the authenticated detail
view — the only surface exposing `source_uri` and `import_report`
(local session trust boundary, `X-Atomic-Token`). The list view and all
job surfaces omit the path.

## Safe file-inspection algorithm

1. `path` must be absolute.
2. **Ancestor walk** — every directory component from root through the
   parent, then the file itself: refuse symlink, junction
   (`Path.is_junction()` where available), or any
   `FILE_ATTRIBUTE_REPARSE_POINT` in `st_file_attributes` (Windows).
3. `resolve(strict=True)`; must be a regular file (dirs, devices,
   sockets, FIFOs refused).
4. Post-resolve reparse re-check.
5. Open **once** in binary read mode — the same handle serves `fstat`
   (before), 16-byte sniff, bounded parse, chunked SHA-256 (1 MiB, cancel
   checkpoint per chunk), and `fstat` (after).
6. Post-inspection identity check: `size`, `mtime_ns`, `st_ino`, `st_dev`
   must not have moved → else `path_changed`.

**Documented Windows limitations**: `resolve()` cannot bind a path to the
future handle; `is_symlink()` misses junctions/other reparse types;
`is_junction()` covers junctions only; Windows `st_ino` is an
approximation and timestamps are FILETIME-granular. The check→open race
is narrowed to handle lifetime, **not eliminated** — residual TOCTOU
remains tracked under Issue #178.

## Bounded parsers (no code execution, ever)

- **safetensors**: 8-byte LE header length `n`; `0 < n ≤ 64 MiB`; header
  must fit the file; `json.loads` only. Every tensor entry is
  `{dtype, shape, data_offsets}`; offsets satisfy
  `0 ≤ begin ≤ end ≤ file_size − 8 − n` with checked arithmetic.
  `__metadata__` captured advisory-only.
- **GGUF** (v1–3): bounded kv walk — ≤4096 entries, key ≤256 B, string
  ≤1 MiB, total metadata ≤8 MiB; array element counts validated against
  `file_size` before any read; unknown value types and truncations are
  `malformed_gguf`.
- Sniff order: `GGUF` magic → gguf; byte-8 `{` after a plausible length →
  safetensors; anything else → `unsupported_format` **refusal** (unknown
  content is never registered).
- Suggestion table is signature-only (`conditioner.*`→sdxl,
  `double_blocks`→flux, LoRA key hints→role lora, GGUF `architecture`
  →mapped family). Zero or ambiguous signatures → `unknown`. User
  `family`/`role_hint` are preserved as declarations; detected evidence
  lives in `meta.suggestions` — neither silently overwrites the other.

## Identity & concurrency

- Content identity = full 64-hex `content_sha256`.
- Deterministic ids: `import:<sha256[:12]>`, then `:2`, `:3`, … inside
  the registration transaction — `INSERT`, on `IntegrityError` roll back
  to the savepoint and try the next suffix (bounded, 9 candidates →
  `identity_exhausted`). The PK constraint is the enforcer; ops-lane
  serialization is a convenience, not the mechanism.
- Same source + unchanged content → verified reuse (`reused`).
- Same content, different source → distinct artifacts preserved.
- Content changed at a registered source → **new** artifact; the old row
  is never silently updated.

## Integrity semantics

Registration is a **point-in-time** statement: `import_report` records
`sha256`, `hashed_at`, and `registration_stat` ("these bytes hashed to
this at this moment"). No ongoing-integrity claim; the UI label is
`registered — last inspected {ts}`. Re-verification is deferred to U-4.
`support_tier="unknown"` is permanent in U-2a; no execution affordance.

## Privacy & recovery

- `source_uri` (resolved absolute path) lives in the artifact row and in
  `job.request_json["source_path"]` — required for execution and
  crash-requeue (`resumable=True`; `reconcile_interrupted` re-queues an
  interrupted import and it completes idempotently). `JobOut` never
  serializes `request_json`.
- The path never appears in job events, list responses, audit payloads
  (`model_import.registered` carries `artifact_id` + format only), error
  details, or logs.

## Error taxonomy

`path_not_absolute` · `path_not_found` · `path_not_file` · `path_is_link`
· `path_changed` · `unsupported_format` · `malformed_safetensors` ·
`malformed_gguf` · `metadata_too_large` · `tensor_offsets_invalid` ·
`duplicate_in_flight` (409) · `license_required` · `identity_exhausted` ·
`io_error`

## Deferred

- **U-2b**: Model Manager import dialog, file picker, refresh wiring,
  managed-copy mode.
- **U-3**: directory/multi-file import, filesystem discovery → `disc:`.
- **U-4**: compatibility engine consuming `arch`/`suggestions`;
  re-verification on demand.
- **U-6+**: runtime registration against imported artifacts; ComfyUI
  integration under its own gate.
