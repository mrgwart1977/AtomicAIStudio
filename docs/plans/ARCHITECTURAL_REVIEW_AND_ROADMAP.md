# Atomic Dream Engine — Architectural Review and Implementation Roadmap

Date: 2026-11-08 · Basis: handoff v0.9 (`docs/handoff/`) · Status: proposal awaiting approval

---

## 1. Repository state

The repository is greenfield. It contains only `docs/handoff/` (10 Markdown specs,
8 architecture SVGs, 2 UI wireframes, 29 design boards, gallery, manifest) and
`.devin/config.local.json`. No source code exists; nothing needs migrating or
preserving beyond the spec itself.

Development environment check (this machine): Node 26.7.0 and Python 3.14.7 are
installed. **Rust/Cargo and FFmpeg are not installed** — both are required by the
specified stack and must be provisioned before Milestone 0 can build.

---

## 2. What the spec actually describes

A modular, offline-first Windows desktop app for AI-assisted filmmaking:

- **Shell:** Tauri 2 (Rust) + React 19/TypeScript + Tailwind/Radix + Zustand.
- **Backend:** Python 3.12+ FastAPI service on loopback, SQLAlchemy 2 + SQLite
  (FTS5) + Alembic, WebSocket job events.
- **AI runtime:** separate managed worker processes (image/video/audio/training)
  behind a provider-adapter interface, GPU scheduler, version-aware capability
  registry; ComfyUI optional.
- **Media:** FFmpeg/FFprobe for probe/transcode/proxy/render/export.
- **Five logical systems:** Production Management, Creative Intelligence,
  AI Generation, Post-Production, Platform Infrastructure — modules, not services.

The spec is unusually disciplined about correctness semantics: project isolation
enforced in the DB/services, immutable versioned records everywhere (character/
world/bible versions, prompt revisions, generation snapshots, sealed approved-shot
manifests), capability honesty (never claim a control a model cannot enforce),
rational timebases, idempotent jobs, explicit authorization/cost gates, and a
single canonical mutation path (Unified Creative Command System).

## 3. Architectural dependency chain

These constraints order the build. Anything skipped early is expensive later.

1. **Project scoping + UUID/FK discipline** — every entity, query, event, cache
   key and job carries `project_id`. Foundation for isolation, sharing,
   branching, memory retrieval and manifests.
2. **Revision/version model** — `ProjectRevision`, entity versions, immutable
   generation snapshots. Required by undo, branching, manifests, cache keys,
   provenance and review comments.
3. **Unified Creative Command System** — preview → approve → commit → audit →
   undo with idempotency and optimistic concurrency. Must exist before the
   assistant, prompt workbench, scene builder or node editor mutate anything.
   Phase 0 contracts, not a later refactor.
4. **Persistent job engine** — durable queue, idempotency keys, WS progress with
   `project_id`, reconciliation of uncertain submissions. Every generation,
   render, training and backup task rides it.
5. **Prompt compilation layers** — project→episode→scene→shot→take defaults,
   prompt-mode fields (append/edit/replace), separate user/scaffold/resolved
   records. Needed before any studio screen generates anything.
6. **Model capability registry** — version-aware capabilities gate every UI
   control, strategy choice and migration report. Mock adapter proves the
   contract; real adapters are verified separately.
7. **Asset store + content-addressed provenance** — hashes, dependency edges,
   pin-by-manifest. Required for cache reuse, portable packages, tamper
   detection and "where-used" impact.
8. **Rational-timebase frame math** — integer ticks + rational FPS shared by the
   timeline, camera solver, performance tracks and review mapping. One
   implementation, not per-feature approximations.
9. **Camera solver + trajectory artifacts** — deterministic, versioned solver
   feeding preview; the three-layer model (intent / sampled trajectory /
   provider request) keeps preview honest about model adherence.
10. **Backup/recovery + sealed manifests** — atomic DB snapshot + media manifest
    + hash verification + second-location policy; the v1.0 release gate.

## 4. Gaps, tensions and decisions the spec leaves open

The documents are internally consistent — amendments explicitly extend rather
than replace, and no hard contradictions were found. The following are open
design points or tensions to resolve before implementation, not silent
simplifications:

| # | Item | Why it matters | Recommendation |
|---|------|----------------|----------------|
| G1 | **SQLite topology** — one app DB with `project_id` scoping vs per-project DBs | Backup/restore "one project independently", shared library, cross-project search, app-level catalog all interact with this | Single app DB + strict scoped queries + per-project backup/export package. Matches "database relationships" language; keeps shared library and FTS sane. |
| G2 | **Backend process model** — how Tauri spawns/monitors Python | Unspecified sidecar strategy; affects startup, crash recovery, installer, dev loop | Tauri sidecar spawning a frozen/embedded Python running uvicorn on an ephemeral loopback port + session token. Prove in M0. |
| G3 | **Python packaging** — PyInstaller/embeddable distro, FFmpeg acquisition | Torch-sized deps make bundling heavy; FFmpeg has LGPL/patent considerations | Ship slim backend (no torch) in installer; models + heavy runtimes download via Model Manager post-install. FFmpeg fetched or bundled with license notices. |
| G4 | **Camera/preview solver location** | 2D/3D preview must be responsive; persistence must match solver output exactly | One solver implementation in the backend (`solve/preview` API); UI renders results. Avoids TS/Python numeric divergence. |
| G5 | **Which real adapters ship in 1.0** | Spec defers to "verified adapters" + compatibility report; hardware unknown | M3 selects ONE local image adapter as the first verified path after hardware survey. Video/voice verified adapters chosen at their milestones. Never pre-commit to a model without testing on target hardware. |
| G6 | **ComfyUI strategy** | "May be integrated"; embedding vs external is a big fork in the road | Adapter interface first; treat ComfyUI as an optional managed external runtime, not embedded, until a verified need exists. |
| G7 | **Screenplay import formats** | "Validated import adapters" without naming formats | Support Fountain + plain text first; Final Draft/PDF later. |
| G8 | **E2E test stack** | Spec requires UI/e2e/usability evidence but names no tool | API-level e2e via pytest+httpx; UI e2e via Playwright against the web UI (Tauri shell smoke-tested separately). |
| G9 | **UI localization framework** | Subtitle/dialogue localization is specced; app-UI i18n is not | Wrap strings via i18next from day one; low cost now, expensive later. |
| G10 | **Update mechanism / telemetry** | Opt-in required, mechanism unspecified | Tauri updater (signed) post-1.0; telemetry off by default. |
| G11 | **Scope pressure** | ~60 feature systems, 100+ proposed tables, ten amendments | The spec itself warns against parallel placeholder modules. Strict vertical slices with a traceability table (see §7). |
| G12 | **"nul"/naming hygiene** | Rename to Atomic Dream Engine applies to future code; `nul` file accidentally committed earlier has been removed | Use `AtomicAIStudio` identifiers from the first commit. |

Tensions worth noting rather than "fixing": beginner simplicity vs. the volume of
advanced controls (resolved by Guided/Standard/Advanced depths — the design
boards show this clearly); all-ten camera modes as v1.0 *scene-preview* scope
even though generated-video adherence is adapter-limited (the three-layer camera
model makes this honest); "verified local generation in 1.0" vs. unknown target
GPU (mitigated by draft tiers, honest capability labels and the compatibility
report rather than promising specific models).

## 5. Version 1.0 vs post-release (consolidated)

Per `PRODUCTION_COMPLETION_AND_RELEASE_SCOPE.md` §Version 1.0, refined into a
checkable backlog:

**Version 1.0 — "a reliable first short film"**
- Standalone Windows install; multiple isolated projects/cast; switching without
  restart; templates.
- Canonical commands, revisions, persistent observable jobs, undo, audit.
- Four-action beginner home; Guided/Standard/Advanced workspaces; prompt editing
  always available; actionable plain-language errors; accessibility basics.
- Structured projects: episodes/scenes/shots, world+character bibles with
  versions, cast assignment (20+/8+/5+ proofs), shared-asset copy/link with
  provenance and local overrides.
- Rules-based production next-step service; budget/resource estimates.
- Verified core local image + video + voice path (at least one adapter each,
  whatever hardware validation supports); direct/world/hybrid prompts; draft/
  final tiers; mock adapters proven by contract tests, never labeled real.
- All ten camera-follow modes in scene preview with trajectory solver and
  capability disclosure; editable camera prompts.
- Stable multitrack core: trim/arrange/mix, source-time mapping, local review
  comments, A/B comparison, validated export wizard.
- Verified backups + recovery wizard + portability checks + immutable
  approved-shot manifests + content-addressed cache.
- Continuity findings with manual routing to supported repair; language/subtitle
  foundation; preset-package schema validation.

**Post-1.0 (accepted scope, explicitly deferred)**
- Plugin SDK; collaborative review service; render farm.
- Trailer generator; layered Poster/Thumbnail Studio.
- Full dubbing/localization studio; community package exchange UI.
- Automated detect→verified-repair loops; advanced multi-pass/VFX; feature-length
  scheduling; expanded lessons/templates; production calendar.

## 6. Proposed repository structure

```
atomic-dream-engine/
├── apps/
│   └── desktop/                  # Tauri 2 shell
│       ├── src-tauri/            # Rust: window, sidecar mgmt, credentials, updater
│       └── src/                  # React 19 + TS UI
│           ├── design-system/    # Tailwind+Radix tokens matching v0.9 boards
│           ├── features/         # home, projects, cast, worlds, storyboard, ...
│           ├── stores/           # Zustand, project-scoped state slices
│           └── api/              # generated typed client + WS subscription
├── backend/
│   ├── app/
│   │   ├── api/                  # FastAPI routers, all /projects/{id}/... scoped
│   │   ├── core/                 # config, logging, security, session tokens
│   │   ├── db/                   # SQLAlchemy models, Alembic migrations
│   │   ├── domain/               # commands, revisions, prompt compiler, manifests,
│   │   │                         # jobs, cache, budgets, camera solver, continuity
│   │   ├── adapters/             # provider adapter interface + mock + real impls
│   │   ├── workers/              # managed inference/training worker processes
│   │   └── media/                # FFmpeg/FFprobe wrapper, proxies, timeline render
│   └── tests/                    # unit + API + isolation + migration + e2e
├── packages/
│   └── contracts/                # OpenAPI/JSON schemas → generated TS client
├── docs/
│   ├── handoff/                  # v0.9 spec (existing, unchanged)
│   ├── architecture/             # ADRs, diagrams-as-code
│   ├── plans/                    # this document, milestone plans
│   └── traceability/             # feature ID → stage → module → test → limitation
├── scripts/                      # dev bootstrap, ffmpeg/model fetch, packaging
└── .github/workflows/            # CI: backend tests, UI build, contracts check
```

Rationale: backend and UI are separate deployable concerns; `packages/contracts`
is the single source for the typed API boundary (the spec mandates typed
contracts and forbids direct provider calls from UI); `domain/` isolates the
command system, prompt compiler and solver from HTTP plumbing so they are
testable without a server.

## 7. Milestone roadmap

Mapped to the spec's Phases 0–9 and consolidated priority tables. Each milestone
ends runnable, tested, and documented; gates cite spec acceptance criteria.

| MS | Spec mapping | Scope | Exit evidence |
|----|--------------|-------|---------------|
| **M0 Foundation** | Phase 0 + v0.5/0.6/0.7/0.8 "contracts Phase 0" items | Monorepo scaffold; Tauri↔FastAPI sidecar; loopback session auth; SQLite+Alembic; core schema (Project, Revision, Job, Command, PromptLayer, Manifest, Cache contracts); persistent job engine + WS; mock adapter; four-action home shell with real prompt bar routed through commands/preview; CI; logging/config; health | Dev build starts on Windows; UI↔API round trip; DB survives restart; job interrupt/resume test; project isolation test seeds |
| **M1 Projects & media** | Phase 1 + PROD, DEP, LIB, PORT, BRANCH(contracts), HEALTH(v0), PM(rules v0), RECOVERY(v1) | Project Dashboard full CRUD/templates/archive/duplicate; settings; switching; storage layout; asset import + FFprobe metadata + thumbnails; episode/scene/shot; backup/restore + autosave/snapshot foundations; bible/world versions; FTS search; health dashboard v0; next-step rules v0 | 3 isolated projects, switch w/o restart, import media, backup→delete→restore, export/import package w/ checksums |
| **M2 Multi-character** | Phase 2 + IDENT, CAST(records), continuity facts | Character Library (grid/list/search); Character Bible 10 versioned sections; batch reference import; SceneCharacter/ShotCharacter; multi-character shot inspector + cast matrix; wardrobe/expression/voice/LoRA assignment; shared-asset copy/link + provenance + local overrides | 20+ chars/project; 8/scene; 5/shot; same-name cross-project isolation; copy+link sharing proofs; snapshot correctness; restart persistence |
| **M3 Image & LoRA records** | Phase 3 + LAB, CATALOG, RESOURCE(v0), PREVIEW(tiers), PROMPT WORKBENCH | Provider/model registry + capability API; Prompt Workbench (direct/world/hybrid, compile/preview/conflicts); Image Studio; draft/final tiers; dataset prep + LoRA training queue + versioning; model catalog + benchmarks; casting sessions; FIRST verified local image adapter (hardware-gated, approval before download) | Prompt-only image without bible; take-scoped coat-change proof; model capability gating; benchmark retained; LoRA version never overwritten |
| **M4 Video generation** | Phase 4 + REPRO, MAN, CACHE, EVAL, STRATEGY, EXTEND, MIGRATE, REPAIR(adapter), NODE(v1) | Video Studio; keyframe workflow; sealed approved-shot manifests; content-addressed cache; reproducible records; take compare; smart rendering strategy; QC/eval; repair adapter; extension/stitch; model migration reports; node editor first slice | End-to-end shot gen w/ progress + recovery; manifest byte-immutability; cache hit/invalidation; infeasible request rejected pre-submission |
| **M5 Scene Designer & cameras** | Phase 5 + all ten follow modes + PERF, MOTION, spatial continuity | 2D blocking; paths/keyframes; full ten-mode follow solver + trajectory artifacts; capability disclosure UI; performance timeline; motion reference binding | Solver unit tests (corner turn, orbit, group frame, lost target, 24/1 + 30000/1001); novice camera task without Advanced Mode |
| **M6 Audio** | Phase 6 + AUDIO cues, casting voice | Dialogue editor; voice profiles; TTS adapter (verified); cue sheets; stems; SFX/ambience; lip-sync integration | Two-character scene dialogue with independent voices synced to timeline; cue edit invalidates only dependents |
| **M7 Editor & finishing** | Phase 7 + DELIVERY, REVIEW_FILM, COMPARE, VFX foundation, REPAIR UI | Multitrack timeline (trim/ripple/roll/slip/slide); transitions/titles; proxies+relink; color; mix; render/export; delivery validator; review mode w/ source mapping; region repair UI; sealed edit graph | Assemble+export complete short; fractional-FPS comment mapping; validator catches bad export |
| **M8 Assistants & providers** | Phase 8 + DIR, FILM, PM(AI), BUD | Conversational assistant → command proposals; Director/Cinematographer plans; story-to-film import→plan→approved execution; provider manager; credential store; cost estimates | Script→multi-character proposal; approve subset; zero unapproved mutations; no paid call without consent |
| **M9 Release hardening** | Phase 9 + OFF, PORT, DEP, RECOVERY(gates), usability gates | NSIS installer; model manager; hardware validation; recovery wizard gates; offline proof; portability; migration tests; traceability matrix; REAL moderated usability sessions (≥5 novices) | Clean-install benchmark workflow; corrupt-backup/full-disk recovery; offline suite; documented usability evidence |

Post-M9 increments: SDK, collaboration, render farm, trailer/poster studios,
dubbing studio, sharing UI, advanced multi-pass/VFX, calendar.

## 8. Recommended first milestone (M0 + M1 slice)

The handoff's own "First Devin task" (§13) and the v0.7 sequencing rule ("one
vertical slice end-to-end before many disconnected features") converge on the
same smallest useful app:

**First deliverable:** a Windows dev build where a user can

1. Open Atomic Dream Engine → four-action home (real layout per board 01).
2. Create projects, rename/duplicate/archive/delete, switch without restart,
   per-project settings — with enforced isolation.
3. Open a project → Character Library: create/edit/archive characters with
   versioned bible sections; create episode→scene→shot; assign multiple
   characters per scene/shot.
4. Type in the persistent prompt bar → routed through the Unified Creative
   Command System → see a dry-run proposal (diff, scope="this take") → commit →
   a **mock-adapter** generation job runs persistently with WS progress → output
   registers as a project asset.
5. Autosave + basic backup/restore proof.

This is the smallest thing that exercises every architectural dependency
(scoping, revisions, commands, jobs, prompts, manifests, assets) while being
demonstrably real software — not 29 placeholder screens. Mock adapter is
explicitly labeled; no model downloads, no cloud calls.

**Why not more:** Phase 0 + the multi-project/multi-character foundation is what
the spec itself bounds the first sprint to; anything with real inference belongs
after the capability registry and hardware survey exist.

## 9. Risk assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Scope volume (60+ systems) causes placeholder sprawl | **High** | Vertical slices only; freeze per-milestone backlog; traceability table; honest stub labels |
| No verified local model exists on target hardware | **High** | Mock adapters prove orchestration early; hardware survey before M3 adapter choice; capability honesty means the app still works without GPU |
| Tauri+Python sidecar packaging on Windows | **High** | Prove sidecar + ephemeral port + session token + restart in M0, before feature code |
| Schema churn with immutable versioning (100+ tables) | Medium | Alembic from day one; additive migrations; contracts stabilize Phase-0 entities first |
| Camera solver determinism (float drift, FPS rounding) | Medium | Integer ticks + rational timebase; golden fixture tests per spec §7 incl. 30000/1001; solver version recorded |
| Beginner UX gates unmeasurable without humans | Medium | Schedule moderated sessions early (M5-ish on a real slice), iterate; keep automated UX tests separate from human evidence |
| FFmpeg/model licensing in redistributed installer | Medium | License screen per spec §11; download-on-demand for large runtimes; checksum + terms display |
| Imported package security (path traversal, archive bombs, code exec) | Medium | Data-only packages; size/node caps; path validation; no script execution — tests in M1 export/import |
| SQLite concurrency under job load | Low–Medium | WAL mode; single-writer discipline; jobs serialize through engine; test concurrent command commits |
| Two runtimes drift (TS types vs Python schemas) | Low–Medium | `packages/contracts` generated client; CI fails on contract drift |

## 10. Blocking questions for the owner

1. **Hardware target:** which GPU/VRAM should the "verified local adapter" path
   target first? (Determines whether M3 picks e.g. an SDXL-class image adapter
   vs CPU-fallback-only, and whether local video generation is realistic on this
   machine.)
2. **Milestone granularity:** deliver M0+M1 as one first milestone (recommended,
   matches the spec's "first Devin task"), or land M0 alone first?
3. **Real-model spend:** confirm approval workflow for the first model download
   (~2–8 GB typical) when we reach M3 — per spec, no downloads without approval.
4. **G1/G2 confirmation:** single app DB + sidecar model OK? (Alternatives
   documented above if you'd rather have per-project DB files.)
5. **Rust toolchain:** OK to install rustup + MSVC build tools and FFmpeg for
   development on this machine? (Required before M0 builds.)

---

## 11. Program C — Universal Cloud AI Integration (parallel workstream)

*Added 2026-11 — amendment, not a revision of §§1–10. All M-series
milestones, gates, and priorities are unchanged.*

A second, parallel milestone family for external AI providers. M-series
continues to own local generation, workers, GPU scheduling and media
validation; C-series owns provider credentials, cloud job orchestration,
cost control, and cloud workflows. Both share the same projects, CAS,
durable jobs, provenance, and command system — a cloud job is just a new
adapter family (`adapter_kind="cloud"`), not an architectural fork.

| Phase | Scope | Gate |
|---|---|---|
| C0 | Feasibility + architecture (research only) | docs/plans/PROGRAM_C_CLOUD_PROVIDERS.md + C0_PROVIDER_FEASIBILITY.md approved |
| C1 | Unified Provider Manager + secure credential store | mock providers + security tests |
| C2 | Cloud text (OpenAI, Anthropic, Gemini, DeepSeek, xAI) | tested w/ authorized creds + spend limits |
| C3 | Cloud image (OpenAI, FLUX, Gemini, Ideogram, Recraft, Leonardo, …) | validated CAS outputs + usage accounting |
| C4 | Cloud video (Kling, Runway, Luma, Veo, Seedance, Wan, Higgsfield, …) | durable remote jobs E2E + cost report |
| C5 | Cloud audio/voice (ElevenLabs, Cartesia, Hume, Murf, Fish, Resemble, …) | validated audio + clone consent |
| C6 | Extended ecosystem (restricted/enterprise/niche/MCP + honest status for D/E rows) | truthful status for all 60 catalog rows |
| C7 | Hybrid local/cloud orchestration (user-authorized routing) | controlled hybrid workflows |

Feasibility snapshot (C0 research, 2026-11): **30 A-class** verified official
APIs, **19 B-class** restricted/gated, **4 C-class** authorized agent/MCP
channels, **3 D-class** external-only, **6 E-class** unavailable/prohibited
(incl. 2 ambiguous names resolved to real providers). 60 catalog rows
normalize to **53 provider accounts**.

Hard rules carried into every C phase: credentials never in the DB/backups/
logs (Windows Credential Manager / DPAPI vault); estimated cost shown in the
command preview before any paid request; unknown remote outcomes reconcile,
never silently resubmit; mock providers in CI — live calls only with
explicit authorization and spend limits; D/E providers show honest status,
never simulated functionality.

Tracking: Program C epic + per-phase issues on GitHub. Documents:
`docs/plans/PROGRAM_C_CLOUD_PROVIDERS.md`,
`docs/plans/C0_PROVIDER_FEASIBILITY.md`.

---

*Generated for owner review. No production code has been written. Awaiting
approval before implementation begins.*
