# Program C — Universal Cloud AI Integration (C0–C7)

Status: **C0 planning only** (authorized: research, docs, issues, planning PR).
C1–C7 require separate authorization. No credentials, paid calls, or live
integrations may be implemented under this document.

Program C extends the existing mega-plan — it does not renumber, rewrite, or
reprioritize any M-series milestone. M-series owns **local** generation,
workers, GPU scheduling, and media validation; C-series owns **external**
providers, credentials, cloud job orchestration, cost control, and cloud
workflows. Both share the same projects, CAS assets, durable jobs, provenance,
command system, and configuration surfaces.

Feasibility research for all 60 catalog entries: `C0_PROVIDER_FEASIBILITY.md`.
Multi-method access architecture (bridge for non-API providers):
`C1_5_ACCESS_BRIDGE.md`.

---

## 1. Milestone definitions

| Phase | Name | Priority | Depends on | Exit gate |
|---|---|---|---|---|
| C0 | Cloud architecture & feasibility | High | M4.1 remote contracts | This document + matrix reviewed/approved |
| C1 | Unified Provider Manager + secure credentials | Critical infra | C0 approved | Provider Manager works with mock providers; security tests pass |
| C1.5 | Universal AI Access Bridge | High (coverage) | C1 | Strategy registry + assisted/import workflows proven on mocks; no automation beyond documented permission |
| C2 | Cloud text generation | High | C1 (+C1.5 for assisted paths) | Approved providers tested w/ authorized creds + explicit spend limits |
| C3 | Cloud image generation | High | C1 + asset infra (M1) | Validated images in CAS w/ provenance + usage accounting |
| C4 | Cloud video generation | High | C1 + media engine (M4.0.1) + durable remote jobs | E2E authorized generation w/ validation + cost report |
| C5 | Cloud audio & voice | Medium | C1 + audio asset infra | Validated audio, secure connections, usage tracking; clone-consent controls |
| C6 | Extended provider ecosystem | Medium | C1 + C1.5 + relevant C2–C5 | Every catalog row has a truthful integration status |
| C7 | Advanced hybrid orchestration | Low (future) | mature C2–C5 + stable local exec | User-controlled hybrid workflows; automation only where the strategy permits |

Execution note: C0 may proceed in parallel with M4.1 runtime work. Cloud work
must never delay, replace, or weaken local AI milestones.

---

## 2. Provider registry architecture

A **declarative provider catalog** (data, not code) so new providers/models
are config additions, not schema changes:

```
ProviderSpec         — one row per unique provider account (51 audited)
  key                e.g. "openai", "google", "alibaba-dashscope"
  display_name       "OpenAI", "Google (Gemini/Imagen/Veo)"
  access_class       A|B|C|D|E            (from feasibility matrix)
  modalities         {text, image, video, audio}
  auth_kind          api_key | oauth_pkce | oauth_server | mcp_oauth | none
  credential_fields  [{name, secret: bool, required, hint}]   (schema-driven UI)
  region             optional region/endpoint selector (DashScope, Kling)
  base_url           default endpoint (overridable for gateways)
  job_model          sync | async_poll | webhook | mcp_session
  idempotency        native_key | request_id | none
  cancel             endpoint | best_effort | none
  pricing_model      per_token | per_image | per_second | per_credit | subscription
  docs_url, tos_url, privacy_url
  status_reason      human text for non-A rows ("Enterprise plan only", ...)
  catalog_aliases    rows that resolve to this account (Juan Prime→wan-prime, …)
  strategies         [AccessStrategy] — all permitted access methods for
                     this provider, best-first; see C1_5_ACCESS_BRIDGE.md §3
                     (official_api | aggregator_api | mcp_connector |
                     browser_authorized | desktop_plugin | assisted_workflow
                     | import_export | external_info)

ModelSpec (cloud)    — extends the existing model_catalog concept
  provider_key → ProviderSpec
  remote_model_id    e.g. "gpt-image-1", "veo-3.1", "wan3.0-video-prime"
  capability_schema  JSON-schema of *genuinely supported* parameters
                     (seed/aspect/duration/voice…) — drives the UI honestly
  cost_table         pricing inputs for the estimator
```

- The **requested 60-entry catalog** is the display layer; normalization maps
  duplicate brands (Gemini Images→Google, Lucid Origin→Leonardo…) to one
  credential/account while preserving every requested row's presence.
- `CapabilityProfile.adapter_kind` gains `"cloud"` alongside
  `local|mock` — profiles expose `provider_key`, `access_class`,
  `requires_cloud=True`, honest status text. Unavailable providers render as
  disabled cards with their reason, never as functional adapters.
- **Channel types** (per strategy, not per provider — a provider may carry
  several): REST adapters (A/B), aggregator adapters (authorized gateways:
  fal/Replicate/OpenRouter), agent-channel adapters via MCP (C: Suno,
  OpenArt, invideo, Descript), official desktop CLIs (invideo, Descript),
  assisted-workflow entries (T2: prompt-prep → user generates → import),
  manual import/export (T3), and external-only entries (T4: guidance text +
  "Open in browser", never simulated generation). Tier rules and the
  per-provider recommended strategy table live in `C1_5_ACCESS_BRIDGE.md`.

## 3. Settings schema — Settings → AI Providers (C1)

Schema-driven, no per-provider hand-written UI:

- Browse/search/filter by modality + access class; each catalog entry has a
  detail view generated from `credential_fields` + `capability_schema`.
- Per-provider: enable/disable, credentials (see §4), model picker +
  per-task default, per-project overrides, connection test, rate-limit /
  timeout / retry config, usage + estimated-cost readout, privacy summary
  (what leaves the machine), region selector where relevant.
- Integration status per provider/strategy (from `C1_5_ACCESS_BRIDGE.md` §8):
  `Connected — Official API | Connected — Authorized Connector |
  Connected — Approved Browser Integration | Assisted Workflow |
  Manual Import | Enterprise Access Required | Unavailable` — driven by the
  best enabled strategy + a live connection test where a credential exists.
- Assisted-workflow providers additionally show their T2 settings: preferred
  site/region, prompt template, import directory, default asset type,
  import behavior, project destination, provenance prefs. Manual/external
  rows show an honest reason (`"Sudowrite — no public API; assisted
  workflow available"`), never a fake generation surface.

## 4. Secure credential storage

- **Store: Windows Credential Manager** (DPAPI-backed) via the Tauri shell —
  the OS-native secret store for this app's threat model. Fallback (dev /
  CI): a DPAPI-encrypted `credentials.vault` under the data dir; plaintext
  files are never an option.
- Secrets **never** enter: `atomic.db`, `.atomicpkg` backups, logs, job
  `request_json`, diagnostics bundles, or the frontend process (keys are
  held backend-side only; the UI stores only presence/metadata).
- Per-provider key rotation + multiple named accounts where useful
  (personal/work OpenAI). Connection tests return `ok/fail` only — never
  echo key material.
- Backup/restore treats credentials as **local machine state**: exports
  carry "credential configured: yes/no" metadata, never the secret.

## 5. Cloud job lifecycle — reuses M4.1 contracts

`domain/workers/remote.py` already defines the shape C needs:

1. **Durable submission intent** recorded in `generation_jobs.progress_json`
   *before* the remote call (provider, model, idempotency key, est. cost).
2. Submit → `remote_job_id` persisted immediately on receipt.
3. **Unknown outcome** (timeout/disconnect post-submit): never silently
   resubmit — reconcile via provider status/query endpoint; only resubmit
   when the provider confirms non-receipt (or supports idempotency keys:
   Canva, Stripe-style). Duplicated paid work is a defect class of its own.
4. Poll with bounded backoff / consume webhooks where offered; cancel via
   provider `cancel_url` where available (Higgsfield) else best-effort.
5. Download → validate through the **existing media engine** (ffprobe/M4.0.1)
   → **CAS commit** with `provenance_json` carrying provider, remote job id,
   model, params, cost actuals — identical to local outputs.
6. Paid-request gate: every cloud job requires the command system's
   preview→approve step *with estimated cost shown* — no background spend.

## 6. Cost-control architecture

- `cost_table` per cloud `ModelSpec` → **pre-flight estimate** displayed in
  the commit preview (tokens→USD, per-image/per-second/per-credit→USD).
- Actuals captured from provider responses/usage endpoints where offered;
  else estimate marked as estimate.
- **Budgets**: per-project cap + per-session cap; hard refusal at cap,
  not a warning. `spend_ledger` rows (project_id, provider, units, est/actual)
  — a new table **requiring an approved migration** (fold into a future
  0008+ proposal; C0 does not ship schema).
- No auto top-up flows; no provider can be invoked without an enabled flag +
  present credential + passed pre-flight estimate.

## 7. Privacy & consent design

- Per-provider `privacy_summary`: what data leaves the machine (prompt text,
  reference assets, project context), retention posture, region.
- **Data-out preview** in every cloud command: "This sends: prompt + 2
  reference images to OpenAI (US)." — consent recorded per-job in provenance.
- Voice cloning / likeness (ElevenLabs, Resemble, HeyGen, DupDub): consent
  gate mirroring provider requirements — explicit per-voice consent record.
- Never silently upload local assets: reference images/bibles only attach
  when the user adds them to that request.
- Providers whose ToS prohibits automation (Midjourney) or wrappers
  (Suno/Udio) are never proxied — E-class honest status.

## 8. Local/cloud routing architecture

- Jobs carry `execution_target: local|cloud` chosen explicitly in the
  command UI — default stays **local** for anything a local model can do.
- A cloud `ProviderAdapter` implements the *same* `ProviderAdapter`
  interface as local adapters (validate → run → progress → output → CAS),
  so the job engine, CAS commit, attempt history, and provenance are
  provider-agnostic. Cloud "worker" = the remote job; supervision = the
  remote-contract lifecycle, not WorkerPool.
- C7 routing (cost/quality/privacy-aware selection + authorized fallback)
  builds on this — C0 establishes only that routing is a *user decision
  recorded per job*, never silent.

## 9. Dependency map — M-series touchpoints

| C needs | Existing M-series asset | Status |
|---|---|---|
| Durable jobs + idempotency + reconciliation | `generation_jobs`, `JobEngine`, attempts (M1/M4.1) | Ready |
| Remote-provider safety contract | `domain/workers/remote.py` (M4.1 mock) | Ready — extend for REST/MCP channels |
| CAS + provenance + validation | CAS (M1), ffprobe media engine (M4.0.1) | Ready |
| Command preview→approve→audit | Unified Creative Commands (M2) | Ready — cost fields added to preview |
| Settings surface | Settings/app_settings (M1) | Extend — schema-driven provider section |
| Secret store | **none today** | **New — C1 builds it (biggest gap)** |
| Usage/budget ledger | none | **New — needs approved migration** |
| UI provider manager | Model Manager page patterns (M3) | Extend |
| Worker/pool/GPU | `domain/workers/*` (M4.1) | Cloud jobs bypass WorkerPool by design |

**Conflicts:** none — C-series adds a parallel adapter family + settings
section; no M-series table or behavior changes except the additive
`adapter_kind="cloud"` and future approved migrations.

## 10. Implementation roadmap (proposed order)

1. **C1** (critical): ProviderSpec catalog + registry (incl. strategies),
   credential vault, Settings→AI Providers UI, mock providers, security
   tests. Foundation for everything else.
2. **C1.5**: AccessStrategy registry, four capability tiers, assisted-
   workflow components (PromptPreparer, SiteLauncher, ImportWatcher,
   ClipboardImport, ProvenanceTagger), browser/desktop permission model,
   ToS-evidence records — proven entirely on mocks; no browser automation
   shipped without documented provider permission.
3. **C2**: text — OpenAI, Anthropic, Google, DeepSeek, xAI (all A-class,
   uniform OpenAI-ish shapes except Anthropic) + assisted paths for the
   no-API writing tools (Sudowrite, QuillBot, Hemingway via C1.5).
4. **C3**: image — OpenAI Images, FLUX, Gemini Images, Ideogram, Recraft,
   Leonardo (+ Qwen/xAI/Meta/Freepik as bandwidth allows) + Midjourney/
   Canva assisted workflows via C1.5.
5. **C4**: video — durable-remote-job layer first; Kling, Runway, Luma,
   Veo, Seedance (via chosen official path), Wan-cloud, Higgsfield +
   assisted paths where a plan-gated API isn't reachable.
6. **C5**: audio — ElevenLabs, Cartesia, Hume, Murf, Fish, Resemble,
   Speechify, Lovo, DupDub + clone-consent + Suno MCP + Udio honest-status.
7. **C6**: restricted/enterprise/niche (Jasper, Writer, Canva, Firefly,
   Synthesia, HeyGen, LTX, MiniMax, TaskAGI, TTSMaker, WellSaid, Descript,
   Copy.ai, Writesonic, Notion, Anyword, Rytr, OpenArt-MCP, Suno-MCP) —
   leans on C1.5 for every non-API row; truthful status for all 60.
8. **C7**: hybrid routing/orchestration after C2–C5 mature; automates only
   where the underlying access strategy permits — T2/T3 stay user-driven.

Every phase: mock providers in CI; live paid tests only with explicit
authorization + spend limits.

## 11. Proposed GitHub issue structure

- **Epic issue** "Program C — Universal Cloud AI Integration" (this doc).
- **C0** planning (closed by this PR once approved).
- **C1** Provider Manager + credential vault.
- **C1.5** Universal AI Access Bridge (strategy registry + assisted/import
  workflows + browser/desktop permission model).
- **C2** cloud text; **C3** cloud image; **C4** cloud video; **C5** cloud audio.
- **C6** extended ecosystem (uses C1.5 for non-API rows); **C7** hybrid
  orchestration.
- Each carries its exit gate + the standing rules: no unapproved paid calls,
  honest status, mock-first testing.
