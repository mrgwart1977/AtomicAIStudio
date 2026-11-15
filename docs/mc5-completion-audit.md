# M-C5 Completion Audit — Atomic Dream Engine

Audit scope: Project → Character → Scene → Shot → Media Import/Generation →
Storyboard → Timeline → Export → Finished Film, plus the C5-E
model/runtime compatibility surface.

Audit-only milestone — no new features implemented. All measurements were
executed on Windows against the frozen PyInstaller backend
(`atomic-backend.exe`, built at the C5-E head `427fcb7`) and the installed
desktop app. Live providers were never contacted; no models were
downloaded; no benchmark was run.

Test evidence layers are kept separate throughout:

- **Code-level** — pytest suite (1158 passed, 11 skipped) + vitest (189).
- **Frozen-backend** — scripted HTTP driver against `atomic-backend.exe`
  with packaged ffmpeg (`scripts`-equivalent driver run 2026-11-15).
- **Installed-app** — launched the installed `atomic-dream-engine.exe`,
  observed window + sidecar lifecycle.
- **WebView2 UI** — not executed this session; the 99-step checklist
  remains open (§6).

---

## 1. End-to-end workflow audit

Verified at code level and, where marked, re-verified on the frozen build.

| Concern | Status | Evidence |
|---|---|---|
| Project ownership | ✔ | `project_id` FKs on all project rows; cross-project access tests refuse |
| Entity relationships | ✔ | character→cast→shot→scene→storyboard via durable rows |
| Character consistency guidance | ✔ | C5-C composer + `continuity_json`; deterministic, omission-disclosing |
| Source asset-version pinning | ✔ | export pins `expected_edl_sha` + timeline revision; stale → 422 |
| Timeline revisions | ✔ | monotonic revisions; revert creates new revision |
| Export EDL integrity | ✔ | `ade-edl/1` deterministic JSON, sha256-pinned at preview→commit |
| Durable jobs | ✔ | `generation_jobs` rows; idempotency keys; attempt tracking |
| CAS provenance | ✔ | imports + renders registered as content-addressed assets, origin-tagged |
| Restart recovery | ✔ | frozen-backend restart test: jobs reconciled, wizard state + assets + trims + renders persisted |
| Error handling | ✔ | plain-language titles + expandable technical detail in wizard; honest failed badges |
| Capability-based UI | ✔ | method picker disables uninstalled/planned models; simulated labelled |

### Frozen-backend re-verification (this audit)

Full wizard wire sequence executed against `atomic-backend.exe` with the
packaged ffmpeg: project → `project.update_settings` (`first_film` +
unrelated key) → character → scene → 2 shots → cast assign → 2×
`asset.import` (video, job poll) → `timeline.create` → 2×
`timeline.clip.insert` (shot:asset dedup) → `timeline.clip.trim` →
`timeline.export` (preview-pinned commit, job poll) → rendered
`M-C5 Audit Film export.mp4` asset → settings merge intact →
**process kill + restart → all state persisted**. All steps PASS.

## 2. Windows packaged validation

Executed (installed-app + frozen-backend evidence layer):

| Step | Executed | Result |
|---|---|---|
| Fresh installation | partial | NSIS artifact built by CI `package` job; app already installed from prior build — installer execution itself not re-run this session |
| Application startup | ✔ | installed `atomic-dream-engine.exe` spawned → window visible in **5.64 s** (pre-C5-E build — C5-E adds no startup-path changes) |
| Sidecar lifecycle | ✔ | `atomic-backend.exe` spawned as app child; **terminated cleanly on app exit — no orphan process** |
| Native video file picker | not executed | requires WebView2 interaction (manual item) |
| First Film entry / project / character / scene / shot creation | ✔ (API layer) | frozen-backend driver, all pass |
| Media import | ✔ (API layer) | 2× mp4 via packaged ffmpeg → CAS assets |
| Timeline editing | ✔ (API layer) | create + 2 inserts + trim |
| Export | ✔ | 1.598 s job → rendered mp4 asset |
| Output playback | not executed | requires UI/player (manual item) |
| Restart persistence | ✔ | wizard state, assets, trim, render all durable |
| Shutdown | ✔ | app exit kills sidecar; no orphans |

WebView2 UI steps were not substituted with API tests — they remain open
in §6.

## 3. Model / runtime compatibility matrix

Machine under test: 1× NVIDIA RTX 3060 Ti (8 GB VRAM), 16 GB RAM at ~75 %
physical load (4.1 GB free, pressure `warn`), zero runtimes installed,
zero models installed. All values below are the actual frozen-backend
`/api/compatibility` + per-model `/validation` responses.

| Model | Modality | Runtime | Support | Availability | Classifier status | Reason (verbatim) | Validation fails |
|---|---|---|---|---|---|---|---|
| sd15.fp16 | image | sdcpp | production | blocked | unsupported | insufficient available system RAM: ~2.6 GB needed at runtime, 3.4 GB free | capability_contract¹ |
| sd15.q8 | image | sdcpp | production | blocked | unsupported | insufficient RAM (~2.5 GB needed, 3.4 GB free) | capability_contract¹ |
| sd15.fp32 | image | sdcpp | production | blocked | unsupported | insufficient RAM (~3.2 GB needed, 3.4 GB free) | capability_contract¹ |
| sdxl.base | image | sdcpp | production | blocked | unsupported | needs ~8.0 GB VRAM; best usable device has ~6.5 GB + insufficient RAM | — |
| sdxl.turbo | image | sdcpp | tested | blocked | restricted | Stability AI Non-Commercial Research license — no production use | — |
| flux.schnell | image | sdcpp | experimental | blocked | unavailable | no verified downloadable artifact on a supported runtime | catalog_metadata² |
| flux.dev | image | sdcpp | experimental | blocked | restricted | FLUX.1-dev Non-Commercial License | catalog_metadata² |
| qwen.image | image | comfyui | experimental | blocked | unavailable | no verified downloadable artifact | catalog_metadata², runtime_available³, capability_contract⁴ |
| wan21.t2v.1_3b | video | sdcpp | tested | blocked | unsupported | insufficient RAM (~8.3 GB needed, 3.4 GB free) | param_combinations⁵ |
| ltx.2.5 | video | comfyui | planned | planned | planned | future milestone — catalog-visible only | capability_contract⁴ |
| wan.2.2.ti2v | video | comfyui | planned | planned | planned | future milestone | capability_contract⁴ |
| hunyuan.video | video | comfyui | planned | planned | planned | future milestone | capability_contract⁴ |

Footnotes (root causes — all honest disclosures, not crashes):
¹ `gen.image.sdcpp-server` compatible-adapter not registered (env-gated
behind `ATOMIC_PERSISTENT_INFERENCE_ENABLED=1` — intentional).
² Zero declared `files` — no downloadable artifact pinned.
³ Runtime `comfyui` registered as planned — no installable build.
⁴ `gen.*.comfyui` adapter unregistered (planned runtime).
⁵ `fps` options declared without `durations_s` — catalog metadata gap.

Runtime matrix: `sdcpp` (master-1073-991f825, backends vulkan/cuda/cpu —
all `backend_available`, none installed); `comfyui` (planned).

Tested vs untested: no model is installed, so **zero capabilities are
tested** on this machine; every row above is catalog/hardware
classification, correctly labelled. Benchmarks: not run (explicit opt-in,
and nothing is installed to benchmark).

## 4. Performance baseline (measured, frozen backend)

| Metric | Value |
|---|---|
| Backend spawn → healthy (migrations) | 2.63 s |
| Installed-app spawn → window visible | 5.64 s |
| Backend idle RSS | 128.7 MB |
| Restart → healthy (same data dir) | 2.57 s |
| Restart RSS | 128.2 MB |
| Hardware probe (GPU detect) | 2.36 s (first call; WMI/NVML) |
| `/api/system/resources` | 0.029 s |
| `/api/compatibility` (12-model matrix) | 0.032 s |
| project.create | 0.038 s |
| character.create / scene.create | 0.044 / 0.034 s |
| video import job (640×360 mp4, ~1.5 s) | 0.568 / 0.561 s |
| timeline.create / clip.insert / clip.trim | 0.036 / ~0.045 / 0.054 s |
| timeline.export job (2 clips → 720p mp4) | 1.598 s |
| export preview | 0.027 s |

Estimates vs measured: table above is all **measured** on the frozen
build. Model VRAM/RAM figures in §3 are explicitly labelled **estimates**
(`basis: "estimate"`) by the API; no measured inference numbers exist —
nothing is installed.

## 5. Release-readiness assessment

**Critical** — none found. Security gates intact: session-token auth on
all endpoints; app-level model ops never carry `project_id`; download
sources are catalog-pinned (no user URLs); runtime binaries resolve via
trusted `RuntimeInstall` rows; no arbitrary CLI args accepted; benchmark
errors surface only exit codes (no raw worker logs persisted);
provider/live-transport gates unchanged.

**High** — none found. The complete filmmaking loop, restart recovery,
and project isolation all verified at code + frozen-backend level.

**Medium**

- M1 — Catalog gaps surfaced by validation (working as designed):
  `flux.schnell`/`flux.dev`/`qwen.image` declare zero downloadable files;
  `wan21.t2v.1_3b` declares `fps` without `durations_s`; sd15 entries
  list the env-gated `gen.image.sdcpp-server` as a compatible adapter.
  Impact: honest "unavailable"/warning flags in UI, but noisy check lists.
  Remediation: pin verified artifacts or downgrade support tier; add
  `durations_s`; mark gated adapters as such in catalog metadata.
- M2 — `capability_contract` check treats unregistered (planned/env-gated)
  adapters as issues rather than informational. Remediation: split
  "adapter planned/gated" (info) from "adapter should exist but doesn't"
  (warning).

**Low**

- L1 — NSIS installer execution + WebView2 flows unverified end-to-end
  (manual items below).
- L2 — Video-model benchmark coverage: harness is scoped to
  `gen.image.sdcpp`; video/comfyui models get honest refusals.
- L3 — Worker-RSS is the only portable memory metric; VRAM peaks remain
  unmeasured (documented on results).
- L4 — 4K mask-undo limitation stands (documented in checklist).

## 6. Manual acceptance status (99-step checklist)

Zero steps newly executed this session. Classification for future
execution:

**Human-required (WebView2/pointer/visual/keyboard — 47 steps):**
1–12 (canvas paint/zoom/pen), 14, 20–22, 26–28, 35–38, 41, 45–47, 52–54,
56, 58, 62, 64, 76–84, 86–87, 89–91, 93–96, 98–99.

**Automatable via scripted driver (frozen backend / TestClient — 52
steps; evidence already exists for most via the test suite):**
13, 15–19, 23–25, 29–34, 39–40, 42–44, 48–51, 55, 57, 59–61, 63, 65–75,
85, 88, 92, 97. These carry UI-observation expectations too, so
automation supplies backend evidence; sign-off still needs a human pass.

Notable: step 51 (kill-app mid-job) and 85 (restart resume) now have
frozen-backend evidence from this audit. 4K mask-undo limitation
preserved (shallow undo ≥2048² documented).

## 7. Safety boundaries observed

No live API requests, no credential-value access, no owner approval
files, no live permits, no provider CLI execution, no paid activity, no
automatic model downloads, no real provider transport. Stage B2b ON HOLD.
Issue #96 remains the mandatory blocker before live artifact retrieval.

---

# Next milestone proposal (planning only — NOT started)

Priority order per owner direction. Suggested sequencing reuses the
existing command/job/CAS/adapter infrastructure throughout; every phase
keeps the preview→commit→audit discipline and project-scoped isolation.

## Phase N1 — Advanced video timeline editing

- **Scope:** multi-clip rearrangement on tracks, ripple/roll/slip trims,
  split-clip, copy/paste clips, clip-level speed (retime via ffmpeg
  `setpts`/`atempo`), gaps; EDL schema v2 (`ade-edl/2`) preserving v1
  readers.
- **Dependencies:** none new — extends C5-A/C5-B timeline domain +
  ffmpeg engine.
- **Risks:** EDL versioning must stay deterministic; retime changes
  audio sync math; revision pinning already in place.
- **Reused infra:** `timelines` domain, `ade-edl`, TimelineExportAdapter,
  job engine, EDL-sha pinning, storyboard→timeline send flow.
- **Tests:** unit EDL diff/pin tests, ffmpeg retime golden-file probes,
  frozen-backend driver extension, ~10 new checklist items.
- **PR boundary:** one PR — `feat/n1-timeline-adv`.

## Phase N2 — Multi-track audio & synchronization

- **Scope:** audio tracks on timelines (dialogue/music/sfx lanes),
  clip↔audio linking, volume/pan per clip, mute/solo, A/V sync offset,
  waveform-lite duration visualization (no DSP), audio assets via
  existing `asset.import`, export mixes via ffmpeg `amix`.
- **Dependencies:** N1 (track model generalization). Audio asset kind
  exists in catalog/import path — verify probe coverage.
- **Risks:** sync drift with retimed clips; `amix` channel-map
  correctness across mono/stereo sources; sample-rate normalization.
- **Reused infra:** timeline domain, asset.import pipeline, CAS,
  ffprobe helpers, export job staging.
- **Tests:** synthetic multi-track mix (ffmpeg-generated wavs), loudness
  normalization probe, frozen-backend e2e with mixed A/V timeline.
- **PR boundary:** one PR — `feat/n2-audio-tracks`. (Split
  waveform/real-DSP into a later phase.)

## Phase N3 — Transitions & effects

- **Scope:** cross-dissolve/fade/dip transitions between adjacent clips
  (ffmpeg `xfade`), per-clip fade in/out, title-card clips (generated
  via ffmpeg `drawtext`), simple filters (grayscale, brightness).
- **Dependencies:** N1 (multi-clip adjacency semantics); export engine
  needs xfade graph assembly.
- **Risks:** `xfade` requires re-encode of overlapping spans (perf);
  `drawtext` font availability in packaged ffmpeg (bundle a font or
  ship fontconfig path); filter determinism across ffmpeg builds.
- **Reused infra:** EDL (transition records), export engine graph
  builder, preview/commit for transition params.
- **Tests:** xfade golden outputs, boundary-frame exactness, packaged
  ffmpeg font probe in CI.
- **PR boundary:** one PR — `feat/n3-transitions`.

## Phase N4 — Character & visual consistency improvements

- **Scope:** strengthen C5-C: per-character seed guidance surfacing,
  refset-driven prompt anchors, wardrobe/appearance diff warnings
  across scenes, consistency report card (shots vs bible), optional
  LoRA slot plumbing in catalog capabilities (`lora` flag already
  declared for sd15.*).
- **Dependencies:** none new for guidance; actual LoRA weight support
  depends on runtime capability validation (sd.cpp `-lora` args exist —
  verify worker contract).
- **Risks:** guidance ≠ guarantee — all UI copy must keep the disclaimer;
  LoRA paths must stay catalog/approved-storage confined.
- **Reused infra:** prompt composer, continuity_json, refsets, approval
  fingerprints, compat_report capability contracts.
- **Tests:** composer determinism + warning-surface tests, refset
  anchor injection tests, LoRA-path confinement unit tests (mock).
- **PR boundary:** one PR — `feat/n4-consistency`; LoRA arg plumbing
  split to its own PR if worker contract changes.

## Phase N5 — Beginner-friendly production workflows

- **Scope:** extend First Film: scene templates (intro/outro beats),
  shot-list suggestions, narration-text→shot prompts (text only, no
  TTS), progress recap card, "redo step" repair flow, richer resume
  notices.
- **Dependencies:** C5-D wizard state model (done); benefits from N1–N3
  polish but not blocked.
- **Risks:** scope creep — keep it template/guidance-only, no new
  generation modes.
- **Reused infra:** wizard state machine, settings persistence,
  reconcileState notices, plain-language error layer.
- **Tests:** component tests for template flows + resume notices;
  ~8 checklist items.
- **PR boundary:** one PR — `feat/n5-workflows`.

## Phase N6 — Local video-model compatibility & performance

- **Scope:** comfyui runtime graduation path (install spec → binary
  resolution → smoke → per-model capability registration), video-model
  benchmark support in `ops.model.benchmark`, VRAM residency modes for
  video weights, catalog artifact pinning for flux/qwen/wan tiers.
- **Dependencies:** C5-E compat engine + benchmark harness (done);
  catalog gaps M1 must be remediated first; owner approval required for
  any actual downloads.
- **Risks:** comfyui is GPL-3.0 (bundling implications for NSIS
  distribution — review license compatibility); video VRAM estimates
  are weakest link; multi-GB downloads need resume/verify.
- **Reused infra:** runtime registry, model_files verify/download,
  benchmark adapter (gen lane), compat_report classes, preflight UI.
- **Tests:** fake-comfyui-worker benchmark tests, catalog-pinning
  unit tests, license-gate tests.
- **PR boundary:** two PRs — `feat/n6a-catalog-pins` (metadata only)
  then `feat/n6b-comfyui-runtime` (runtime plumbing). No downloads
  without separate approval.

### Suggested order

N1 → N2 → N3 (timeline capability builds cleanly), N4 & N5 can
parallel after N1, N6 last (largest infra risk + license review + the
only phase needing new external binaries).
