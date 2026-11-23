# U-4 — Model Compatibility (five-axis static verdicts)

Status: implemented end-to-end (U-4a classification + capability
declarations, U-4b verdict persistence + ops-lane evaluation + APIs,
U-4c desktop surface, U-4d this document). PR #185 open/draft; merge
requires owner approval. Approved baseline `27d1bfa`; migration head
`0030_u4`.

> **What "compatible" means here:** a *static* verdict computed from
> stored evidence — never a runtime execution claim. The strongest
> verdict the system can emit is **"Compatible — static evaluation"**.
> Nothing in U-4 runs, downloads, installs, probes, or verifies a model
> file, and nothing in a verdict grants permission to run inference.
> V5 (actual execution verification) does not exist and is never
> emitted.

## D1 — Architecture

### Scope of evaluation

Only **registered artifacts** (`model_artifacts` rows — `catalog:`,
`import:`, `train:` identities) are ever evaluated. Discovery
candidates (`disc:`/U-3 rows) are unreachable to the evaluator —
unregistered files are never inspected, classified, or fingerprinted.

### Stored-evidence classification (`model_families.py`)

`classify_artifact()` resolves an artifact's model family purely from
stored evidence — it never opens, reads, hashes, or parses a model
file:

- **Catalog artifacts** — high-confidence declared provenance from the
  catalog registry itself (`provenance="catalog"`).
- **Imported artifacts** — the U-2 `import_report_json` evidence:
  safetensors tensor-key signatures + metadata hints, GGUF
  `general.architecture` + KV metadata (`provenance="import"`).
- **Conflicts** — contradictory evidence yields `family_id=None` with
  `conflicts` set → `arch_conflict`. The evaluator never best-guesses.
- Filename inference does not exist. Unknown stays `unknown`.

The registry (`ModelFamilySpec`) declares 16 families: image (`sd1.5`,
`sdxl`, `flux`, `qwen`), video (`wan`, `ltx`, `hunyuan`), LLM
(`llm_llama`, `llm_qwen`, `llm_mistral`, `llm_gemma`, `llm_phi`), plus
auxiliary and adapter-cap entries. `FAMILIES_VERSION` versions the
registry and feeds the ruleset version.

### Runtime capability declarations (`runtime_caps.py`)

`RuntimeCapabilityDecl` + `FamilyCap` are the audited, versioned
(`CAPS_SCHEMA_VERSION`) claims a runtime makes per family — evidence
grades (`tested`/`documented`), format sets, serving modes, tested
dtypes, required components, exact `validated_versions` release pins,
and `policy_gated` flags. Absent combinations are unverified — they
evaluate to `unknown`, never to optimistic support. The full matrix is
in D2.

### Five independent axes

Every verdict evaluates four independent axes plus an evidence level.
**Axes never collapse into each other** — a missing runtime is not an
architecture failure, insufficient hardware is not architecture
incompatibility:

| Axis | Values |
|---|---|
| Architecture | `supported` / `unsupported` / `unknown` (+ `arch_confidence`: `tested` / `documented` / `unverified`) |
| Runtime | `operational` / `installed_unverified` / `not_installed` / `planned` / `unknown` |
| Hardware | `fits` / `limited` / `insufficient` / `unknown` |
| Policy | `eligible` / `restricted` / `blocked` |

Deterministic aggregation (`_aggregate`, frozen F3 contract):
`unsupported` arch → `incompatible`; `unknown` arch → `unknown`;
blocked/restricted policy or planned/not-installed runtime → `blocked`;
`insufficient` hardware → `blocked`; `limited` hardware or non-operational
runtime → `limited`; all clean → `compatible`. `blocking_axes` lists
every non-clean axis — nothing is hidden by the aggregate.

### Evidence levels V0–V4 (V5 deferred)

Non-cumulative per-domain levels (`domain_levels`): `inspection` V1
(a completed U-2 report with recognized fmt — catalog rows are 0, since
family identification does NOT imply file-integrity verification);
`family` V2 (architecture identified); `runtime_static` V3 (this verdict
ran the static evaluation); `hardware` V4 (this verdict ran the hardware
evaluation). Aggregate `level` is capped at 4 — **V5 is deferred and
unemittable** (`test_v5_never_emitted`).

### Persistence (`compat_verdicts`, migration `0030_u4`)

Latest-verdict-per-`(artifact_id, runtime_key)` model —
`uq_compat_artifact_runtime` unique constraint, `ON DELETE CASCADE` to
`model_artifacts` (a deleted artifact leaves zero orphans). Each row
stores all four axes + confidence, derived overall + blocking axes,
domain levels, reasons/warnings/missing/hardware_limits/actions,
point-in-time `evidence_json` samples (never re-consulted on read),
the `fpv1` inputs fingerprint, `ruleset_version`, `job_id`, and
`evaluated_at`. `stale` is **computed live on read** (fingerprint
drift), never stored, and a GET never rewrites a verdict.

### Ops-lane-only writes

The ONLY writer of `compat_verdicts` is the `ops.model.compat_eval`
adapter (`adapters/compat_eval_adapter.py`), registered on the
serialized ops lane (capacity 1) — concurrent evaluation jobs cannot
interleave writes. Per-artifact delete-and-insert runs in ONE
transaction per artifact with a single commit; cancellation checkpoints
sit at artifact boundaries, so a cancelled job never leaves a partially
replaced verdict set. Resume is fingerprint-aware — artifacts whose
stored fingerprint already matches are skipped. No synchronous API
write path exists; `with_for_update` is not used for correctness.

### Fingerprinting (`fpv1`)

Versioned fingerprint over stable inputs only: artifact identity +
stored evidence, runtime version + operational state, capability
version, family-registry and ruleset versions, hardware *configuration*
(GPU set/driver/backends/total RAM), residency policy, required
components/base-model deps. **Volatile samples are excluded by
construction** — free VRAM, available RAM, and probe timestamps can
make a verdict stale-flagged via the fingerprint's config half but
never falsify it silently; they feed warnings and `evidence_json`
instead (`test_fingerprint_deterministic_and_volatile_excluded`).

### Desktop display (U-4c)

A dedicated Compatibility `<section>` on Model Manager — an artifact
selector, the selected artifact's five-axis panel, and an
evaluate-all action. Read-only on mount; evaluation is always
user-triggered through the durable job contract (D5).

## D2 — Runtime capability matrix

Exactly two declarations exist (`RUNTIME_CAPS`, `caps_version` 1).
**Everything absent is unverified** — Diffusers, dedicated LTX/Wan
runtimes, llama.cpp-class runtimes, and all cloud adapters have no
declarations and no claims.

### `sdcpp` — stable-diffusion.cpp

`planned=false`, backends `vulkan/cuda/cpu`, adapter IDs
`gen.image.sdcpp`/`gen.video.sdcpp`.
`validated_versions=("master-1073-991f825")` — **exact release-tag
pins only**; tags are non-semver and cannot be ordered, so any other
installed version is `installed_unverified` →
`runtime_version_unverified`, never treated as validated.

| Family | Grade | Formats | Modes | Dtypes tested | Components | Policy | Notes |
|---|---|---|---|---|---|---|---|
| `sd1.5` | tested | safetensors, gguf (tested) | text2image | F16, F32, Q8_0 | — | — | sd15.fp32 safetensors + sd15.fp16/q8 GGUF, production |
| `sdxl` | tested | safetensors (tested) | text2image | F16 | — | — | **No verified GGUF path is claimed** |
| `wan` | tested | safetensors (tested) | text2video | F16 | diffusion + vae + text_encoder | — | end-to-end validated on Vulkan @ the pinned tag; `min_runtime_version=master-1073-991f825` |
| `flux` | documented | gguf (documented) | text2image | — | — | — | catalog flux.* are `experimental`; upstream claims FLUX GGUF, never validated in-app |
| `adapter:sd1.5` | documented | safetensors (documented) | text2image | — | — | **policy_gated** | LoRA attach declared by the adapter contract but G1/B-2/B-3e-2 holds keep it gated → `hold_gated` regardless of arch fit |

### `comfyui` — ComfyUI (**PLANNED**)

`planned=true` — **zero verified compatibility**; exists so verdicts
can say honestly "recognized, but the runtime is not installable yet"
(`runtime_axis=planned` can never be `operational`). No adapters,
no validated versions. Declared families (all `documented`,
safetensors): `qwen` (text2image), `ltx` (text2video/image2video/
audio2video), `wan` (text2video/image2video; diffusion+vae+
text_encoder required), `hunyuan` (text2video).

### Honest holds and unverified paths

- **LoRA hold**: `adapter:*` FamilyCaps are policy-gated — the policy
  axis emits `blocked`/`hold_gated` even when architecture fits. Active
  while G1/B-2/B-3e-2 hold.
- **Documented ≠ tested**: `documented` grade caps the arch axis at
  `supported (documented)` → overall `limited` with
  `runtime_path_unverified` — never bare `compatible`.
- **Installed ≠ operational**: an installed runtime whose version is
  not in `validated_versions` is `installed_unverified` → `limited`.
- **License**: `license.commercial_ok=false` → policy `blocked`
  (`license_restricted`).

## D3 — Security and trust boundaries

U-4's restrictions are load-bearing and test-enforced:

- **No unregistered discovery-candidate evaluation** — the evaluator
  resolves only `model_artifacts` rows.
- **No model-content reads, parsing, or hashing** — tests monkeypatch
  `open`/`Path.read_*` during live evaluation
  (`test_no_model_content_io_during_eval`).
- **No network, no provider/cloud calls, no process execution** —
  egress surfaces and `subprocess`/`os.system` are monkeypatched in
  tests (`test_no_network_or_execution_during_eval`).
- **No inference, no runtime smoke tests, no admission changes** —
  `gpu_sched.py` and inference adapters are untouched; a verdict never
  changes `enabled`/`support_tier` (`test_verdicts_change_no_eligibility`,
  `test_no_inference_eligibility_in_modules`).
- **No automatic downloads, installs, or model scanning**.
- **No automatic evaluation** — the desktop issues GETs only on mount;
  POSTs are exclusively user-triggered (test-asserted).
- **No artifact-identity or U-1/U-2/U-3 control changes** —
  classification provenance is honest (`catalog` vs `import`); managed
  files untouched.
- **No sensitive data** — no absolute paths, `source_uri`, credentials,
  or provider metadata in verdicts, API responses, or the UI.
- **Issue #178** (Windows filesystem TOCTOU in managed-copy identity
  re-binding) remains **open and unaddressed** — out of U-4 scope.
- Verdicts are **advisory only** — they inform the user; they gate
  nothing.

## D4 — API and job contracts

All four endpoints live under the existing token auth; responses carry
no paths or provider data.

| Endpoint | Behavior |
|---|---|
| `GET /api/models/artifacts/{id}/compat` | Read-only. `{artifact_id, verdicts[]}` — each verdict carries axes, confidence, level/domain_levels, reasons/warnings/missing/limits/actions, `stale` (live-computed), `ruleset_version`, `evaluated_at`. 404 on unknown artifact. **Never writes.** |
| `GET /api/compat/ruleset` | Read-only declaration catalog: `ruleset_version` (`u4:fam{v}.caps{v}`), `families_version`, `caps_schema_version`, `max_verification_level: 4`, `axes`, per-runtime decls (display name, caps version, planned flag, backends, validated versions, per-family caps). No install state, no paths. |
| `POST /api/models/artifacts/{id}/compat/evaluate` | **HTTP 202** → `{job_id, status}` durable handle. Never synchronous. |
| `POST /api/models/compat/evaluate-all` | **HTTP 202** → `{job_id, status}` durable handle; evaluates every registered artifact. |

**Job contract** (`model_compat_eval` on the serialized ops lane):

- **Idempotency** — identical scope + inputs fingerprint → the SAME
  job handle is returned (queued, running, *or* succeeded) — no
  duplicate-submission ambiguity.
- **Conflict** — an in-flight job with *different* inputs → HTTP 409
  with the conflicting `job_id`.
- **Retry** — a failed/cancelled job deterministically re-keys
  (`:retryN`) so an unchanged request can be re-accepted; repeat POSTs
  after a success return the successful handle.
- **Transactions** — per-artifact replace-all (delete + insert) in one
  commit; artifacts are transaction boundaries.
- **Cancellation** — checked at artifact boundaries; pre-start cancel
  writes nothing; mid-run cancel keeps every committed artifact's
  complete verdict set.
- **Deletion** — an artifact deleted mid-job is recorded `artifact_gone`
  without crashing; a scope=artifact job on a missing artifact fails
  cleanly.
- **Errors** — sanitized codes only; no absolute paths in any error,
  event, or result payload.
- A 202 handle grants **no inference permission** — it only schedules a
  static evaluation.

## D5 — Desktop UX contract

`CompatSection` (`apps/desktop/src/features/ArtifactCompatSection.tsx`)
— a **separate selector-based panel** on Model Manager, intentionally
outside the Universal Model Library `<section>`: that surface's button
inventory is a frozen contract (catalog rows carry zero action
buttons), so U-4's UX decision is an artifact selector + per-artifact
panel + evaluate-all action in the Compatibility section header.

- **Five-axis display** — per runtime verdict: architecture (+ tested/
  documented/unverified confidence), runtime install state, hardware
  fit, policy, evidence level V0–V4 with per-domain breakdown.
- **Static labels only** — strongest is "Compatible — static
  evaluation"; "Ready to Run"/"Verified to Run" do not exist in the
  codebase (test-asserted absent).
- **Reason codes translated** to readable explanations; missing
  components, hardware limits, and `install_runtime:*` actions listed.
- **Staleness** — `stale` renders "Evaluation out of date" + explicit
  Re-evaluate; evaluated-at timestamp shown; nothing auto-re-evaluates.
- **Jobs** — POSTs return durable handles tracked through the shared
  jobs store (WS) — the U-3 convention, no second poller. Queued/
  running states show honestly; succeeded refetches; failed/cancelled
  state "previous verdicts are unchanged"; button disabled in-flight
  (no duplicate-submission feedback); 409 → "already in progress."
- **Hardware samples labeled** — `evaluated_at` is annotated
  "measurements from that moment, not current availability"; volatile
  warnings tagged "(measured at evaluation time)"; configuration fit
  stays distinct from point-in-time availability.
- **Version honesty** — "Evaluated version: unavailable" (the field is
  not in the read contract); "Validated runtime versions" shown as a
  separate declared set — **a validated version is never implied to be
  the version evaluated**.

**Known limitation (deferred backend improvement):** a structured
`evaluated_runtime_version` field on `CompatVerdictOut` — the evaluated
version exists only inside the persisted `evidence_json` sample, which
the read contract deliberately does not expose.

## D6 — Evidence and acceptance matrix

### Commits (on `feat/u4-model-compat`, PR #185)

| Phase | Commit | Content |
|---|---|---|
| U-4a | `35dbacf2408767582087d01955f4faad94198a2c` | `model_families.py`, `runtime_caps.py`, `test_compat_u4.py` |
| U-4b | `0be894a66c50cbefad6dac6e1132fb26915a2783` | `compat_verdicts.py`, `compat_eval_adapter.py`, `0030_u4` migration, `models.py`, `routers.py`, `schemas/api.py`, `registry.py`, tests |
| U-4b fix | `129a4168ba3be1c22034421d3ef5d76fe4082082` | three targeted `# noqa: B008` (project convention) — zero net-new Ruff |
| U-4c | `0022d7ab815b3e881d796752f6c6eea548dee21b` | `ArtifactCompatSection.tsx` + tests, `types.ts`, `ModelManagerPage.tsx` |
| U-4c fix | `925441ce6ac6caab5917e6fa14899bdc8eeae827` | honest evaluated-version presentation |
| U-4d | *this commit* | this document |

### Cumulative changed-file inventory — **15 files**

Backend (10): `app/domain/model_families.py`,
`app/domain/runtime_caps.py`, `app/domain/compat_verdicts.py`,
`app/adapters/compat_eval_adapter.py`, `app/adapters/registry.py`,
`app/api/routers.py`, `app/schemas/api.py`, `app/db/models.py`,
`app/db/alembic/versions/0030_u4_compat_verdicts.py`,
`tests/test_compat_u4.py`.
Frontend (4): `apps/desktop/src/api/types.ts`,
`src/features/ArtifactCompatSection.tsx`,
`src/features/ArtifactCompatSection.test.tsx`,
`src/features/ModelManagerPage.tsx`.
Docs (1): `docs/handoff/U4_MODEL_COMPATIBILITY.md`.

### Verification record

- **Migration**: fresh-DB `run_migrations` stamps `alembic_version =
  0030_u4`; `compat_verdicts` + `uq_compat_artifact_runtime` +
  `ix_compat_artifact`/`ix_compat_overall` + `ON DELETE CASCADE` created
  additively. Head `0029_u3` → `0030_u4`. Confirmed again for U-4d.
- **Backend tests**: U-4 suite **78 passed** (69 test fns incl.
  fixtures). Full regression **2480 passed, 11 skipped** (run twice —
  `0be894a` and `129a416` heads).
- **Ruff**: zero net-new findings after the corrective commit —
  `routers.py` B008 count back to the baseline 136; all remaining
  findings pre-existing.
- **Frontend**: focused suite **29 passed**; full Vitest **689
  passed**; `tsc -b && vite build` clean; `cargo check` clean (no Rust
  changes).
- **Exact-head PR CI**: `45082024562` (U-4a `35dbacf`) ✓ ·
  `45092864709` (U-4b `0be894a`) ✓ · `45100817722` (fix `129a416`) ✓ ·
  `45108528640` (U-4c `0022d7a`) ✓ · `45113898782` (fix `925441c`) ✓ —
  `changes` + `backend (3.12/3.14)` + `frontend` success each time;
  `package`/`tauri-check` path-skipped on backend/frontend-only diffs.
  These path-filtered PR runs are **not** the mandatory six-job
  merged-main verification, which runs on the merge commit itself.
- **Security regression**: no-I/O/no-network/no-exec and no-escalation
  tests monkeypatch the primitives and run live evaluations; U-1
  identity, U-2 import controls, U-3 discovery invariants proven
  untouched (`test_registry_and_discovery_untouched`).

### Requirement-to-test mapping

| Requirement | Evidence (test) |
|---|---|
| Schema + unique + cascade | `test_table_exists_and_unique_constraint`, `test_fk_cascade_removes_verdicts` |
| Five axes persisted | `test_all_five_axes_persist` |
| Deterministic aggregation | `test_aggregate_precedence`, `test_deterministic_same_input_same_output` |
| Missing runtime ≠ arch failure | `test_runtime_absence_is_not_arch_incompatible` |
| HW insufficiency ≠ arch failure | `test_hardware_insufficiency_is_not_arch_incompatible` |
| Tested/documented/planned | `test_tested_documented_planned_distinctions`, `test_sdcpp_tested_families_match_catalog_evidence`, `test_comfyui_decl_is_planned_never_verified` |
| Unvalidated version | `test_unvalidated_runtime_version`, `test_validated_versions_are_exact_pins` |
| Unknown dtype/arch | `test_unknown_dtype_never_confirmed`, `test_unknown_architecture` |
| LoRA + license gates | `test_lora_policy_gate_blocks`, `test_license_restricted_blocks` |
| Multi-component deps | `test_multi_component_missing`, `test_wan_catalog_components_required_and_present` |
| V0–V4 / V5 never | `test_domain_levels_non_cumulative`, `test_v5_never_emitted` |
| Fingerprint determinism + volatile exclusion + invalidation | `test_fingerprint_deterministic_and_volatile_excluded`, `test_fingerprint_invalidates_on_inputs` |
| Read-only GET, staleness | `test_get_is_read_only`, `test_get_marks_stale_not_rewrites`, `test_compat_404_for_unknown_artifact` |
| 202 POSTs + dedup + 409 | `test_post_evaluate_returns_202_job`, `test_post_evaluate_all_202`, `test_duplicate_request_same_job`, `test_409_in_flight_different_inputs`, `test_409_evaluate_all_in_flight` |
| Overlap scopes | `test_artifact_and_all_scopes_both_accepted` |
| Cancel + retry + atomicity | `test_cancel_before_start_writes_nothing`, `test_cancel_mid_run_committed_verdicts_survive`, `test_resume_skips_current_artifacts`, `test_per_artifact_atomicity` |
| Artifact deletion | `test_artifact_deleted_mid_job`, `test_scope_artifact_gone_fails_job` |
| No model I/O / network / exec | `test_no_model_content_io`, `test_no_network`, `test_no_model_content_io_during_eval`, `test_no_network_or_execution_during_eval` |
| No eligibility escalation | `test_verdicts_change_no_eligibility`, `test_no_inference_eligibility_in_modules`, `test_classification_has_no_eligibility_fields` |
| U-1/U-2/U-3 regressions | `test_registry_and_discovery_untouched` |
| Classification provenance/conflict | `test_catalog_declaration_is_declared_provenance`, `test_import_provenance_is_labeled`, `test_conflicting_metadata_and_signature_is_conservative`, `test_unknown_architecture_stays_unknown` |
| Overclaim guards | `test_sdcpp_does_not_overclaim`, `test_no_future_runtimes_declared`, `test_lora_capability_is_documented_and_policy_gated` |
| Desktop C6 states ×18, job states, staleness, selector, eval-all, no-auto-eval, version honesty | `ArtifactCompatSection.test.tsx` — 29 tests (`C6.1`–`C6.18`, job-state, `C4` version-contract tests) |

### Known limitations / deferred work

- `evaluated_runtime_version` structured API field — deferred.
- `comfyui` remains a planned stub; no new runtime claims authorized.
- No `restricted` policy emitters beyond license gating yet (axis
  reserved for future policy sources).
- Verdict freshness is user-triggered only (by design — nothing
  auto-evaluates).
- Issue #178 remains open, unaddressed by U-4.
