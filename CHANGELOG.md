# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses semver once public releases begin.

## [Unreleased]

### Added
- C3-C-1 — Simulated sync image-provider expansion. Implemented
  contracts for three additional providers on the existing mock
  boundary (`IMAGE_REST_KEYS` now also includes them; Cloud Studio
  picks them up automatically):
  - `gemini-images` — `gemini-2.5-flash-image` via the documented
    `:generateContent` shape (contents→inlineData, usageMetadata) plus
    `imagen-4.0-generate-001` / `-ultra-generate-001` via `:predict`
    (instances→predictions). Ten documented aspect ratios; $0.039/$0.04/
    $0.06 per image per Google's published rates.
  - `recraft` — `recraftv4_1` + `recraftv3` via
    `/v1/images/generations` (b64_json + reported `credits` API units;
    settles credits×usd_per_credit: $0.035/$0.04 raster).
  - `ideogram` — `ideogram-3` via the documented v2 multipart endpoint
    (`Api-Key` auth; prompt/aspect_ratio/rendering_speed/seed/
    negative_prompt fields). Sync responses return temporary artifact
    URLs, retrieved through the provider's own injected transport under
    a declared host allowlist + byte cap; expired/foreign URLs keep the
    durable `artifact_pending_retrieval` outcome for reconcile — never
    resubmitted.
  - Framework: `EndpointSpec` gains documented `content` ("multipart")
    and `artifact_hosts` fields; `RestRemoteProvider` gains bounded
    `fetch_artifact` (https-only, suffix-matched hosts, streamed byte
    cap — all inside the injected transport, zero egress). Billing
    gains `per_credit` settlement from reported or declared credit
    usage; every new cost_table row carries a `checked` freshness date
    and `source` URL.
- C3-C-2 — BFL FLUX async contract (`bfl-flux`/`flux-pro-1.1`):
  POST `/v1/{model}` → `{id, polling_url}`, poll
  `GET /v1/get_result?id=` through the documented Pending→Ready
  lifecycle; `result.sample` temporary URL retrieved via
  `fetch_artifact` on `delivery.bfl.ai`. Moderation/failure statuses
  map to provider_failed (exposure held); `Task not found` polls to
  the ambiguous-timeout hold — never a resubmission. No documented
  cancel endpoint → `UnsupportedRemoteOp`. `flux-kontext`
  (editing model) stays outside the contract with no cost_table —
  commit fails closed. **Adobe Firefly remains intentionally
  unimplemented**: the API is async + IMS OAuth and Adobe publishes
  no per-image API USD price, so cost admission would have to
  guess — catalog documents this; provider renders disabled.
- C3-C hardening — remote artifact retrieval (`fetch_artifact`):
  no provider auth headers are sent to delivery hosts (pre-signed
  URLs are self-authenticating); user-info URLs, non-443 ports,
  deceptive suffix hosts, non-https schemes, loopback/private
  destinations (via the allowlist), redirects (never followed),
  oversized Content-Length, and streamed bytes beyond the image cap
  are all refused before/while reading. BFL `polling_url` and
  provider failure text no longer persist to durable progress rows;
  retried attempts can never auto-resubmit a non-idempotent
  contract (`job_attempt_no` guard).
- C3-B — Cloud Studio image UI (`CloudImagePanel` in the Cloud
  workspace tab). Surfaces `generate.image.cloud` end-to-end:
  catalog-driven provider/model pickers gated by the implemented
  contract list (`GET /api/providers/image-contracts`), prompt +
  negative prompt, catalog-declared sizes/quality tiers, optional
  seed, per-image estimate + synthetic budget headroom, and the
  preview → approve → commit workflow. Any parameter change after a
  preview discards the pending approval (fingerprinted payload), a
  fresh preview is required, and commits send the exact previewed
  payload. Catalog providers without an implemented contract render
  disabled with the reason — never implied operational.
  - Simulation transparency everywhere: SIMULATION banner on the
    form, the approval card, and every job card; the approve action
    reads "generate simulated image"; results are labeled
    `provenance: simulated` + "deterministic test PNG, not provider
    output"; financial rows always say `(synthetic — no real charge
    exists)`.
  - Job cards split generation status (queued/running/succeeded/
    failed) from financial status (reservation open/settled + held-
    for-reconcile); `artifact_pending_retrieval` and `ambiguous`
    outcomes get explicit "reconcile required" wording. Remote
    temporary URLs are never rendered — the panel surfaces no
    `image_url` field at all.
  - Assets: succeeded jobs show the generated thumbnail (authenticated
    `/file` URL) plus "View in Assets" — it navigates to the asset the
    C3-A job already registered; there is no second import path and no
    duplicate asset creation. Scene/shot/character associations flow
    from pickers → request metadata → asset media_json, and linked
    names render on the job card.
  - Backend additions are minimal and read-only in effect: an
    `image-contracts` catalog endpoint exposing `IMAGE_REST_KEYS`, and
    `sizes`/`qualities` metadata on `gpt-image-1` so the UI offers
    only real options. No new commands, no financial changes, no live
    paths.
  - 25 frontend tests (provider gating, invalidation, duplicate
    submit, sim labeling, status/financial split, asset navigation,
    URL non-exposure, responsive grids) + 3 backend contract tests;
    evidence screenshots in `docs/c3b-evidence/`.
  - Review-hardening: the preview response now carries a structured
    `cost_estimate` — the authoritative figure the approval surface
    renders (recomputed through the same pure catalog functions
    `_validate` applied; client-side hints are labeled preliminary).
    Previews the backend could not price disable approval outright.
    Thumbnails load via header-authenticated fetch → object URL
    (`fetchAssetObjectUrl`) — the session token never appears in an
    image URL; missing/denied images fail to a safe placeholder.
    The 10-tab workspace nav scrolls horizontally at the 1100px
    minimum instead of overflowing the header (`min-w-0` +
    `overflow-x-auto`; tabs are `shrink-0` real buttons with
    `aria-current`, so keyboard focus auto-scrolls them into view).
    Job fixtures/tests now cover artifact-success-with-open-hold so a
    succeeded job is never read as settled.
- C3-A — cloud image generation on simulated transports
  (`generate.image.cloud` → `cloud.image.sim`). OpenAI Images sync
  contract (`POST /v1/images/generations`) exercised over
  `httpx.MockTransport` fixtures — documented request/response shapes,
  zero egress, dummy credentials, deterministic test PNGs. Contract is
  declarative `EndpointSpec` data (`domain/workers/rest_image_providers.py`)
  so async/poll/cancel providers (FLUX-style) plug into the same
  lifecycle the text adapter already drives; temporary-URL payloads and
  provider-specific errors are handled at the contract seam.
  - Extensible image pricing (`cloud_cost.estimate_image_cost`): one
    billing `unit` per cost_table — `per_image` (flat or quality-tiered
    `rates`), `per_mpixel`, `per_mtok`, `per_credit`. Missing table,
    missing `checked` evidence, unknown unit, or unestimable input
    fails closed (commit refuses `pricing_unavailable`). Unspecified
    quality reserves the highest documented tier — worst-case bound,
    never an under-estimate. Catalog cost tables added for
    `gpt-image-1` (tiered) and `gemini-2.5-flash-image` (flat $0.039).
  - Financial lifecycle identical to cloud text: spend-guarded commit,
    `spend.admit` reservation at estimate (or `max_cost_usd`), settle at
    `images_returned` × catalog rate (source `simulated`), 4xx release,
    ambiguous/unusable billing → open hold for explicit reconcile —
    never a fabricated charge. Job is non-resumable (remote scope
    reconciles, never auto-resubmits).
  - `domain/image_safety.py` — payload gate before CAS: bounded bytes
    (16 MiB, not the 2 MiB text cap — no live HTTP limits changed),
    PNG/JPEG/WebP allowlist, header+full decode verify, 64 MP pixel cap
    + 8192-dimension limit (decompression-bomb refusal before decode),
    SHA-256 digest, safe re-encoded PNG thumbnail.
  - Filmmaking metadata passes through request_json → AssetVersion
    media_json: scene/shot/character/world/location ids, prompt +
    negative prompt, seed, requested vs actual dimensions, reference
    asset — no schema migration needed.
  - Provenance: asset carries adapter id, job id, remote provider,
    `provenance_confidence: simulated` — `verified_remote` is
    structurally unreachable (no `provider_verified` evidence can be
    produced by a mock transport).
  - Review-hardening: URL-only responses classify as
    `artifact_pending_retrieval` (remote succeeded, retrieval
    unsupported in C3-A) — distinct from `ambiguous` and `failed`,
    reconcile-required, with the temporary URL persisted in
    `progress_json` for a future retrieval workflow; never resubmitted.
    Encoded payloads are capped before base64 decode (≈4/3 inflation
    bound) so oversized strings never materialize. Pricing `checked`
    evidence must be a real date within 90 days — stale/malformed/
    future-dated pricing fails closed. Asset-registration failures fail
    the job cleanly (rollback of uncommitted asset rows, work-dir
    purge) instead of stranding the job `running`. Signed/temporary
    remote URLs persist in `progress_json` for reconcile but are
    bearer references — `domain/redact.py` strips them (and raw
    payload keys) from every outbound surface: `JobOut` responses and
    `job.progress` websocket events show a redaction marker while the
    durable row keeps the real value; outcome classification
    (`artifact_pending_retrieval`) is unaffected.
  - 63 regression tests: preview/commit/idempotency/concurrency,
    budgets, fail-closed pricing incl. staleness, image-safety matrix
    incl. decompression bomb + pre-decode cap, CAS/provenance/job
    linkage, settle/release/hold billing paths, artifact-pending
    retrieval, registration-failure isolation, sim-vs-live accounting
    isolation, explicit reconcile, restart sweep, zero-egress
    construction, no credential reads, sensitive remote-URL redaction
    across REST + websocket paths.
- C2 exit gate Stage B2a — DeepSeek-only live-validation permit
  (`domain/live_permit.py`), a durable single-use authorization for a
  future owner-approved real request. `cloud.live.validate_arm` writes
  an armed permit naming provider/model/project/credential-ref/prompt/
  token and cost limits; `cloud.live.validate_run` consumes it in the
  same atomic transaction as `spend.admit()` — a tampered or
  mismatched record fails closed with no reservation. Dispatch runs
  through a post-commit hook on a separate session so the command's
  write lock is released first. No permit is ever shipped armed; DeepSeek
  remains the only provider the seam can reach, and the real transport
  stays off until Stage B2b owner approval.
  - Independent owner authorization: `validate_arm` and `validate_run`
    additionally require owner-approval FILES under
    `<data_dir>/owner/` (`arm-approval.json`, `exec-approval.json`) —
    written by the owner on disk, never by the app. An authenticated
    session alone cannot authorize; revocation is file deletion or
    `not_after` expiry. The exec approval must name the armed permit's
    sha256 fingerprint — the Stage B2b execution lock; nothing in this
    build creates one, so `validate_run` fails closed even with an
    armed permit.
  - Durable run state machine: the GenerationJob row is created inside
    the command transaction (`validating`) and transitions
    `validating → dispatching → succeeded|failed|unknown` on committed
    writes. Neither state is engine-schedulable ("queued" only) nor
    restart-swept ("running" only) — a crash freezes an auditable
    state and can never auto-resubmit; unknown outcomes hold the
    reservation for explicit reconcile.
  - Command idempotency hardened: replaying a committed
    `idempotency_key` with a different type/project/payload now fails
    409 instead of silently replaying; a true replay never re-runs the
    post-commit dispatch hook.
  - Pre-submission evidence check: `run_validation` verifies durable
    evidence (armed permit + zero open live reservations) before
    dispatching — never inferred from a job's status. An open live
    reservation means a prior dispatch crossed the billable boundary
    with unknown outcome: the new run fails closed
    (`uncertain_prior_dispatch`) until explicit reconciliation.
  - Approval filesystem hardening: `load_approval` rejects symlinks
    and non-regular files, and on POSIX refuses files writable by
    group/other. Threat model documented: defense against app-level
    reachability only, not owner-level filesystem compromise.

### Changed
- DeepSeek catalog + permit model updated to current official API
  reference (verified 2026-11-13): the retired `deepseek-chat` /
  `deepseek-reasoner` ids are replaced by `deepseek-flash`
  ($0.30/$1.20 per Mtok, peak) and `deepseek-v4-pro`
  ($1.32/$3.96 per Mtok, peak). The single-use permit now binds
  `deepseek-flash`.

### Fixed
- Live settlement fails closed on missing, empty, partial, malformed,
  null, or negative provider usage: the DeepSeek response parser no
  longer fabricates `{0,0}` usage, and `_usage_cost_micro` returns
  None for unusable reports. The financial exposure is then held as
  an OPEN reservation with `settlement_state="usage_unavailable"` —
  never reported as a provider-confirmed charge, never silently
  released; explicit documented reconciliation is the only exit.
  Generation outcome and provenance stay separate: an authenticated
  valid response still returns `succeeded` with verification
  evidence while billing remains unresolved.
- C2 exit gate Stage B1 — production-capable live transport
  infrastructure, still unreachable from every shipped surface
  (`domain/workers/live_transport.py`). `LiveHTTPTransport` enforces the
  full egress stack per request: https + exact documented host +
  no-userinfo allowlist, public-IP-only DNS resolution (loopback,
  private, link-local, multicast, reserved all refused), redirects
  refused outright with credentials never forwarded, `trust_env=False`
  proxy/netrc isolation, provider-scoped connect/total deadlines, and a
  bounded response stream (2 MiB cap). `live_dispatch()` is the sole
  orchestration path — no command, router, env var, or config reaches it:
  `dispatch_admission` → atomic `spend.admit` commit BEFORE transport
  construction → gate re-check (kill/disable between admission and send
  leaves the reservation open for reconcile) → `vault.get_fields` read
  only inside the boundary → submit → classify → settle on confirmed
  success / release only on confirmed refusal / HOLD on any ambiguous
  outcome (never auto-resubmitted; explicit `spend.reconcile` only).
  `RestRemoteProvider` accepts the marked live wrapper while keeping
  refusal of every other transport — the simulated Cloud Studio path is
  unchanged. Provenance contract corrected: `verified_remote` now
  requires authenticated transport evidence + approved endpoint identity
  + job/request correlation + response fingerprint + audit linkage on a
  genuinely live transport — `remote_job_id` is recorded when supplied
  but is neither required nor sufficient, and is never fabricated.
  Test-only: validated entirely through `httpx.MockTransport` + dummy
  vault secrets; `transport_is_live` is False for injected transports so
  no `verified_remote` artifact can be produced in B1.
  Review hardening: the real inner transport (`_SecureHTTPTransport`)
  pins its connection pool to `_ValidatedBackend`, an httpcore network
  backend that dials only `resolve_public()`-validated IP literals —
  the hostname never reaches the socket layer, closing the DNS
  TOCTOU/rebinding gap while TLS verification + SNI remain bound to
  the allowlisted hostname (httpcore passes `server_hostname` to
  `start_tls` independently). Streaming-body enforcement proven across
  chunked responses, mid-stream failures, and post-refusal cleanup;
  oversized/unusable responses after send stay ambiguous — never
  truncated to success. Merge hardening: `_SecureHTTPTransport` now
  installs exactly one `AsyncConnectionPool` (no unused default pool
  ever allocated, no proxy path exists), `_assert_pool_internals()`
  verifies the pinned httpx/httpcore private-internal contract at
  construction and raises `DependencyContractError` on any incompatible
  upgrade, and unlisted 4xx responses stay ambiguous — only documented
  refusal statuses release reservations.

### Added (earlier)
- C2 exit gate Stage A — live-connectivity safety infrastructure,
  mock-only, no live transport wired (`domain/live_gate.py`). Durable,
  audited provider+model-scoped opt-ins via new commands
  `cloud.live.{enable,disable,disable_all,resume}` and a read-only
  `GET /cloud/live-status` endpoint. Enablement validates: catalog
  evidence freshness, model capability + cost table, credential-ref
  presence in the local vault (value never read), project/global live
  budgets, and per-provider/global spend caps ($0.10/req, $0.15/provider,
  $0.75 total proposed — enforced as admission checks, not provider
  billing guarantees). The complete financial gate is additionally
  enforced inside `spend.admit()` under the BEGIN IMMEDIATE write lock,
  so concurrent independent connections cannot overbook caps and no
  stored opt-in, restored database, env var, config, or payload can book
  live exposure without the gate. `generate.text.cloud` routes any
  future live-reporting adapter through `dispatch_admission` before
  booking — the independent execution boundary. Egress allowlist (`validate_request_url`) +
  per-provider timeout/retry policy constants + `verified_remote`
  evidence contract prepared for Stage B; the global kill switch and
  per-provider disable refuse all future live dispatch, survive
  restart, and never release outstanding reservations. New
  "Live exit gate — Stage A" panel in Cloud Studio
  (`features/workspace/LiveGatePanel.tsx`) exposes the state honestly:
  no live dispatch exists in this build.
- C1.5 assisted text workflows in Cloud Studio — a dedicated
  "Assisted text — no API integration" panel
  (`features/workspace/AssistedPanel.tsx`) for catalog providers whose
  only documented text path is user-driven (T2 `assisted_workflow` /
  T3 `import_export`, `automation_permission != "prohibited"`):
  Sudowrite, QuillBot, Jasper, Copy.ai, Writesonic, Notion AI,
  Anyword, Hemingway. Guided rail (Prepare → Review & copy → Open
  provider → Create externally → Return & import); optional
  character/world/location/scene context is composed locally and shown
  verbatim before anything is copied. All steps are explicit user
  gestures riding the existing audited bridge commands
  (`bridge.prepare` / `bridge.launch` / `bridge.import` /
  `bridge.watch.scan`) — the backend only validates launch
  destinations; the client shell opens HTTPS URLs itself. Capability
  reports gate every control; stale evidence refuses dispatch; T3-only
  providers degrade honestly to the manual paste/file-import path.
  Imports remain `provenance_confidence="user_reported"` external
  assets — never simulated REST output, never verified remote.
  AssetsPage now labels those assets "assisted" and shows the external
  provider/method in details.
- C2 text provider completion — the remaining documented A-class text
  providers now carry REST contract implementations over
  `httpx.MockTransport` only (no live transports, no egress):
  - `google-gemini` — Gemini `generateContent`
    (`POST /v1beta/models/{model}:generateContent`, `x-goog-api-key`
    header auth): synchronous; `candidates[].content.parts[].text` +
    `usageMetadata` parsed; the documented response carries NO id so
    `remote_job_id` stays honestly `None`.
    https://ai.google.dev/api/generate-content
  - `deepseek` — chat completions (`POST /chat/completions`, Bearer):
    synchronous OpenAI-compatible shape; `choices[0].message.content`
    + `usage`. https://api-docs.deepseek.com/api/create-chat-completion
  - `xai` — chat completions (`POST /v1/chat/completions`, Bearer):
    synchronous; xAI's deferred-completion retrieval applies only to
    requests explicitly made deferred, so no poll/cancel is wired.
    https://docs.x.ai/developers/rest-api-reference/inference/chat-completions
  All three declare `idempotency="none"` (no documented server-side
  dedupe) and `lifecycle="sync"` — `query()` returns None and
  `cancel()` raises `UnsupportedRemoteOp`, never invented endpoints.
  Shared error taxonomy: 4xx refusal classes release the reservation
  (confirmed pre-processing), 5xx/409/transport/malformed outcomes
  hold it for explicit `spend.reconcile`. Simulated fixtures in
  `rest_simulated.py` emit the documented response shapes with
  synthetic usage; Cloud Studio surfaces all three via the existing
  catalog-driven selectors — no UI redesign.
- C2.0d Cloud Text Generation UI — new project workspace "Cloud" tab
  (`features/workspace/CloudPage.tsx`, "Cloud Studio" in the left nav):
  catalog-driven provider/model pickers (A-class + documented T1
  `official_api` text strategies only), prompt editor, required
  `max_cost_usd` cap, explicit `execution_target="cloud"` opt-in,
  heuristic cost estimate labeled as an estimate, and a data-out
  disclosure. Every mutation — `generate.text.cloud`, `spend.budget_set`,
  `spend.budget_remove`, `spend.reconcile` — flows through
  preview → approve → commit → audit; nothing bypasses the command
  system. A persistent `SIMULATION — No external AI provider
  contacted.` banner renders on the page, in the approval panel, on
  every remote job card, and in asset details for simulated
  provenance; OpenAI/Anthropic are labeled simulated API integrations.
  Remote job cards show the honest lifecycle (intent → submission →
  polling only where documented → outcome / needs reconciliation) with
  no invented progress; cancellation is offered only where the provider
  documents it (sync lifecycles explain reconcile-instead). Spend views
  separate simulated (synthetic) from live accounting across
  session/day/month/lifetime scopes — limit, reserved exposure,
  recorded charges, and available capacity. The reconciliation queue
  lists ambiguous/interrupted jobs with per-job reserved exposure and
  drives `spend.reconcile` outcomes through the same approval flow.
  Minimal read-only backend additions: `spend.summary` now returns raw
  budget rows (project vs global targets) and a new
  `GET /api/projects/{id}/spend/reservations` endpoint lists per-job
  holds. No credential fields, no live transports, no verified_remote
  UI state.
- C2.0c REST Provider Adapter Contracts — documented provider HTTP
  contracts behind injected, in-memory transports ONLY:
  `domain/workers/rest_providers.py` declares per-provider
  `EndpointSpec`s (OpenAI Responses `background` mode → real `resp_*`
  operation id → documented `GET` poll + `POST` cancel; Anthropic
  Messages → synchronous completion with `msg_*` response id — no
  poll/cancel invented) plus a classified `RemoteError` taxonomy
  (validation/authentication/forbidden/rate_limited/conflict/
  server_error/transport/malformed → `none_confirmed` release vs
  `unknown` hold — never auto-resubmitted). `rest_simulated.py`
  provides `httpx.MockTransport` fixtures emitting documented response
  shapes. `RestCloudTextAdapter` (`cloud.text.rest`) drives the same
  durable remote-job contract as the mock; `generate.text.cloud`
  routes by `provider_key` (openai/anthropic → REST contract, all
  others → in-memory mock). Provider-reported usage is priced
  locally at catalog rates (exact integer tokens → Decimal → micro-USD
  ceiling), always `source='simulated'` with
  `cost_is_provider_billed=False` — never a provider invoice. HARD
  EGRESS BOUNDARY: transports are required constructor arguments and
  only `httpx.MockTransport` is accepted — no production transport
  factory, default client, or real network path exists; all outputs
  remain `simulated` provenance (`verified_remote` unreachable without
  a real authorized transport, which C2.0c deliberately omits).
  Remote-scope jobs now skip the local-RAM weights preflight (no local
  weights to estimate — watchdog/RAM admission for local work
  unchanged).
- C2.0b Durable Spend Ledger + Budget Gates — migration `0008_c2` adds
  `spend_budgets` (per-project/global caps over session|day|month|
  lifetime scopes), `spend_reservations` (one open hold per remote job;
  open → settled | released, terminal states trigger-locked), and
  `spend_charges` (append-only; corrections are signed `adjustment`
  rows; UPDATE/DELETE aborted by trigger). All money is INTEGER
  micro-USD; USD crosses the boundary once via Decimal CEILING —
  no float arithmetic in admission. `generate.text.cloud` commits now
  run budget-check + reservation + job enqueue atomically inside a
  scoped `BEGIN IMMEDIATE` transaction (spend commands only — all
  other commits stay deferred); concurrent independent connections
  serialize on SQLite's writer lock + busy_timeout. Ambiguous remote
  outcomes hold the reservation open at worst-case exposure — release
  only via confirmed no-charge paths or the audited `spend.reconcile`
  command (confirmed_charge / confirmed_no_charge / still_unknown;
  overage requires `accept_overage` and is recorded uncapped).
  Environment is adapter-derived (`mock` → `simulated`), never client
  input; simulated and live consumption are summed separately against
  the same limits. `environment='live'` admission with no
  `max_cost_usd` bound is refused. Billing session id rotates per app
  run (persisted for attribution); day/month keys are UTC — a charge
  inherits its reservation's accounting period so a later settlement
  never migrates consumption. Exactly one primary settlement charge
  per reservation is enforced by partial unique index (engine and
  manual reconcile can never both land); adjustments unrestricted.
  `project.delete` is refused while open spend reservations exist —
  an orphaned hold would count against global budgets forever with no
  reconcile route. Financial rows deliberately use snapshot (non-FK)
  project/job references so history survives `project.delete` — the
  documented exception to the FK rule. `GET .../spend/summary` reports
  per-scope limits vs consumption, environments segregated.
- C2.0a Mock Cloud Text Generation Lifecycle — the remote-job contract
  exercised end-to-end on `MockRemoteProvider` with ZERO network egress
  and zero credentials. `generate.text.cloud` (audited command,
  `provider_key` + explicit `execution_target: "cloud"` — local stays
  the default) is eligibility-gated on a documented T1 official-API
  text strategy; preview warns with the data-out surface, a clearly
  labeled cost ESTIMATE (catalog `cost_table`, `checked` dates —
  enforced by the C2.0b ledger/budgets added above), and
  the mock/simulated limitation. `MockCloudTextAdapter`
  (`adapter_kind="mock"`, `execution_scope="remote"`) persists the
  submission intent to `progress_json` BEFORE submitting, records
  `remote_job_id`, polls with a bounded budget, honors cancel via
  best-effort remote cancel, and treats every UNKNOWN outcome
  (ambiguous submit, unreachable, timeout) as `reconcile_required` —
  never an automatic resubmit (the durable `idempotency_key` is the
  dedupe key, injected by the engine). Ambiguous outcomes
  (`remote_outcome: "ambiguous"`) are durably distinguished from
  confirmed provider failure (`"provider_failed"`) in `progress_json` —
  the UI and reconciliation tooling can never read an unknown outcome
  as a provider refusal. Remote-scoped jobs reconcile as
  `needs_reconciliation` on restart and are non-resumable — a crashed
  remote job can never silently re-bill. Output lands through the
  normal CAS pipeline as kind="text" with `provenance_confidence:
  "simulated"` + `remote_id_synthetic: true`; `verified_remote` is
  decided centrally by `remote.provenance_confidence()` and is
  structurally unreachable without `provider_verified` evidence from a
  real authorized transport (C2.0c+). `RemoteProvider.idempotency`
  declares the dedupe contract (`native | request_id | none`) —
  per-provider mappings land in C2.0c. `CapabilityProfile` gains
  `execution_scope`; `JobEngine.enqueue` stamps it from the adapter
  profile; engine injects `job_idempotency_key` into adapter requests.
  Catalog `ModelSpec.cost_table` added for the five A-class text
  providers (openai/anthropic/google-gemini/deepseek/xai — estimate
  rates with checked dates). No new migrations — remote intent/ids
  live in existing `progress_json`/`execution_scope` columns.
- C1.5 §5 Desktop CLI Detection, Phase 1 — read-only discovery only.
  Providers with officially documented local CLIs (`openai`,
  `anthropic`/`claude`, `google-gemini`/`gemini`) now carry a catalog
  `cli{}` record: executable names, supported platforms, install-hint
  directories, and a `https` docs_url as evidence. `validate_catalog`
  enforces safe basename-only executable names, valid platform tokens,
  no-traversal hint dirs, and mandatory evidence. The audited
  `bridge.cli.detect` command (explicit user action — never automatic)
  performs `shutil.which` PATH lookup plus exact-filename checks in
  catalog-declared hint dirs, one level deep, no recursion. Symlinks and
  Windows reparse points are rejected, not followed; results report
  `detected` / `not_found` / `unsupported_platform` with an
  `skipped_untrusted` count. **Nothing is ever executed** — no
  subprocess, no shell, no `--version`; a found path is unauthenticated
  and unverified. Durable audit keeps only provider/status/platform/
  counts — absolute paths exist only in the live response. Detection
  resolves no strategy and promotes no tier; `verified_remote` remains
  deferred. Providers UI gains a "Local CLI" section (visible only when
  a CLI is documented) with a "Check installation" button, found-path
  display, and a standing honesty warning.
- C1.5 ImportWatcher Stage B — opt-in background polling: a single
  app-level `ImportWatchService` runs one shared asyncio task (default
  60 s interval, 30 s floor) over providers whose persisted
  `settings.import_behavior == "poll"`. Opt-in only via audited
  `bridge.watch.enable` / `bridge.watch.disable` commands (enable
  requires a currently-valid designated folder). All filesystem work
  runs on one worker thread off the event loop; one scan per provider in
  flight — a still-running scan is never overlapped. Each poll
  re-validates the ORIGINAL designated folder with the same Stage A
  logic (symlink/junction/traversal rejection, containment, extension
  gating, entry/candidate bounds); an invalid folder marks the watcher
  `degraded` with a reason, stops scanning it, and only resumes when
  that same folder validates again — never silently re-anchors. Polling
  pauses under critical memory pressure and while a `gen.*` inference
  job runs. Candidates live in a bounded in-memory cache (200/provider,
  2000 global, newest-first eviction) surfaced read-only via
  `GET /api/providers/{key}/watch` with per-project dedup; they can
  never become assets without an explicit per-file `bridge.import`.
  Polling ticks write no durable audit rows; enable/disable are audited
  commands carrying no absolute paths. Every pending scan is tagged
  with its session's (generation, folder fingerprint): disable,
  re-enable, or folder redesignation orphans the in-flight scan —
  tracked to completion but its results are discarded and can never
  publish into a newer session's cache. Shutdown cancels the task,
  waits a bounded grace (5 s) for in-flight scans, cancels queued work,
  and lets any wedged filesystem call finish on the worker — a raw
  daemon `threading.Thread` + FIFO queue rather than
  `ThreadPoolExecutor`, because `concurrent.futures._python_exit` joins
  pool workers at interpreter shutdown regardless of daemon flags.
  Verified by a subprocess-level exit probe: with a stalled worker the
  backend process exits in bounded time, not just `stop()`. Entry and
  candidate bounds limit work items, not wall-clock; a stalled syscall
  is documented as beyond cooperative cancellation. The Providers page refreshes watch status every ~12 s
  while auto-check is enabled (read-only GET, one request in flight,
  never triggers a scan, silent on transient backend errors). Still
  stat-only, non-recursive, and never auto-imports.
- C1.5 ImportWatcher Stage A — manual candidate scanning: providers can
  designate an `import_dir` in settings; `bridge.watch.scan` performs an
  audited, bounded, non-recursive, stat-only enumeration (never reads
  file contents, rejects symlink/junction/traversal paths, skips
  anything resolving outside the folder, dedupes already-imported
  files). Candidates display with "provider origin unverified" labeling
  and import only via per-file explicit clicks through `bridge.import`
  (`from_watch` revalidates against the live filesystem at import time —
  symlink swaps, deletions, folder escapes, and ineligible extensions are
  re-rejected). Folder (re)designation writes a path-minimized app-level
  audit event; scan audit records carry candidate counts, never paths.
  Background polling (Stage B) is not implemented — no watch loop exists.
- C1.5 Bridge UI + desktop integration: Settings → AI Providers now shows
  the per-feature capability report (`catalog_documented` honesty labels,
  stale/`needs_recheck` warnings, required user actions, prohibited and
  T1-unimplemented states) and the canonical T2 assisted workflow —
  prepare-and-copy prompt, validated site link with exact destination
  display, explicit click-to-open via the Tauri Opener plugin
  (`opener:allow-open-url` scoped to `https://*`, no execute/path
  permissions), and file/clipboard import with user-reported external
  provenance. Browser open is desktop-only, gesture-only; the backend
  remains side-effect-free.
- C1.5 Access Bridge dispatch + capability reporting: `GET
  /api/providers/{key}/capabilities` (per-feature tier/method/strategy,
  `catalog_documented` verification label, dispatch vs blocked actions),
  and `bridge.prepare`/`bridge.launch`/`bridge.import` commands through
  preview → commit → audit. Prepare renders prompt templates for manual
  paste; launch opens only validated catalog HTTPS URLs in the user's
  browser; import routes user-selected files/clipboard text through the
  `external_source` provenance path. Prohibited/stale/T1 strategies are
  refused — no programmatic provider calls, no browser automation.
- C1.5 Access Bridge foundation: enriched AccessStrategy records across
  the 60-provider catalog (strategy_id, ToS evidence + verification
  dates, auth/cost/reliability), catalog integrity validation,
  staleness demotion (`needs_recheck`, enable blocked while stale),
  `cloud-assisted`/`cloud-external` adapter kinds, and `external_source`
  provenance tagging on asset import (`origin=external`,
  `provenance_confidence=user_reported` — `verified_remote` stays
  reserved for T1 remote job ids). No live provider contact.
- Provider Manager foundation (C1): 60-entry catalog, DPAPI credential
  vault, Settings → AI Providers UI, project overrides — **management
  foundation only; no live cloud generation**
- `scripts/validate.ps1`: local validation entry point (backend on
  Python 3.12 + 3.14, frontend build, optional Tauri/package checks)
- Optimized CI: path-gated jobs, timeouts, concurrency cancellation,
  dependency + FFmpeg caching, Linux frontend job
- Open-source preparation: Apache-2.0 LICENSE, third-party notices,
  contributing/conduct/security docs, issue templates

### Fixed
- Worker shutdown lifecycle: reservations release only after confirmed
  OS process exit (#51/#52)
- Windows process liveness: signaled-object check fixes
  `GetExitCodeProcess` STILL_ACTIVE race (#54)
- CI executor-thread starvation on unkillable workers (poll-based waits)

## Notes

Historical milestone detail lives in `docs/plans/` and `docs/*-evidence/`.
A public release history starts at `v0.1.0`.
