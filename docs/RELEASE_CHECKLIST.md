# Release tracking — outstanding items

Central tracker for every known limitation, deferred improvement, and release
requirement. **Rules:** nothing here is removed or marked resolved without
test evidence or a documented resolution. Reviewed and updated at the end of
every milestone. v1.0-blockers are called out explicitly.

Status values: `open` · `in-progress` · `blocked` · `resolved`

| # | Item | Priority | Target | Status | Blocks v1.0 | Issue |
|---|------|----------|--------|--------|-------------|-------|
| 1 | Clean Windows VM install & upgrade verification | High | v1.0 | open | **yes** | #5 |
| 2 | Windows installer code signing & SmartScreen | High | v1.0 | open | **yes** | #6 |
| 3 | Real UNC/network-share backup stress test | Medium | v1.0 | open | **yes** (documented limitation acceptable short-term) | #7 |
| 4 | Interrupted import resume support | Medium | post-v1.0 | open | no | #8 |
| 5 | Security review of test-only env overrides | High | v1.0 | open | **yes** | #9 |
| 6 | Large-project performance & scalability validation | High | v1.0 | open | **yes** | #10 |
| 7 | Installer & DB migration compatibility matrix | Critical | v1.0 | open | **yes** | #11 |
| 8 | Model download approval workflow | High | M3 | open | **yes** (gate before any real adapter) | #12 |
| 9 | First verified local AI adapter | High | M3 | open | **yes** | #13 |
| 10 | Usability testing with target users | Medium | M9 | open | **yes** | #14 |
| 11 | FAT32 >4 GiB guard — edge coverage | Low | v1.0 | open | **yes** (defensible as-is; margin improvement) | #15 |
| 12 | Shot-level reference-ID picker UI | Low | M3+ | open | no (API+schema delivered M2.1; UI deferred) | — |

---

## 12. Shot-level reference-ID picker UI — (no issue yet)

- **What:** `shot_characters.reference_ids_json` is schema-validated,
  project-scope-checked, persisted, backup-remapped, and round-trips through
  the storyboard API — but the shot editor has no picker for it. Shot-level
  identity refs can only be set via API.
- **Why open:** deliberate deferral — the field exists for generation adapters
  (M3+); building a picker before anything consumes it would be dead UI.
- **Acceptance:** shot editor offers a reference-asset picker once a
  generation adapter reads `reference_ids_json` end-to-end.

---

## 1. Clean Windows VM install & upgrade verification — #5

- **What:** M0.1/M1/M1.1/M2 validated on the dev machine (env-scrubbed frozen
  exe + real NSIS install + observed 0001→0002→0003→0004 upgrades). Still needs
  a pristine VM/Hyper-V run: install → launch → all screens → upgrade → uninstall.
- **Why open:** dev-machine validation can't prove a bare Windows box works.
- **Acceptance:** documented VM run with screenshots; install-over-install
  from a previous release verified; zero dev dependencies.

## 2. Windows installer code signing & SmartScreen — #6

- **What:** unsigned NSIS installer → SmartScreen warning on first install.
- **Why open:** needs a certificate decision (EV/OV vs. self-sign + fingerprint doc).
- **Acceptance:** signed install with no SmartScreen, or a documented
  self-sign + fingerprint-verification flow.

## 3. Real UNC/network-share backup stress test — #7

- **What:** UNC paths validate and probe reachability (M1.1), but no real
  NAS/SMB share was soak-tested — large streamed export, mid-write disconnect,
  cancel, latency.
- **Why open:** requires an actual share; behavior under real network
  failure modes is unverified.
- **Acceptance:** export + cancel + mid-write disconnect on a real share;
  friendly errors verified and documented.

## 4. Interrupted import resume support — #8

- **What:** interrupted imports are safely cleaned (staging sweep + failed
  run row) but must restart from zero — verified blobs aren't reused.
- **Why open:** designed for correctness first; resume adds a checkpoint ledger.
- **Acceptance:** re-running an interrupted import skips already-verified
  blobs; crash-mid-import + resume covered by test.

## 5. Security review of test-only env overrides — #9

- **What:** `ATOMIC_SIDECAR_TOKEN` (fixed automation token) and
  `ATOMIC_PYTHON` (interpreter override) ship in the release binary.
- **Why open:** decision pending — keep documented / debug-gate / remove.
- **Acceptance:** decision recorded and enforced in the packaged build.

## 6. Large-project performance & scalability validation — #10

- **What:** architecture targets TB-scale projects (CAS, bounded-memory
  streaming, indexed queries, pagination) but was never load-tested at scale.
- **Why open:** requires a synthetic large fixture; M2 added storyboard
  queries that need the same validation.
- **Acceptance:** project with tens of thousands of assets + hundreds of
  shots; storyboard/assets/backup endpoints stay responsive; memory bounded;
  numbers documented.

## 7. Installer & DB migration compatibility matrix — #11

- **What:** pairwise upgrades verified live (0001→0002→0003→0004), but no
  scripted matrix over all migration pairs, old-installer→new-installer, or
  downgrade refusal.
- **Why open:** needs the scripted harness; highest-risk area for user data.
- **Acceptance:** scripted matrix covering every migration pair +
  installer-over-installer + downgrade behavior, run in CI or documented.

## 8. Model download approval workflow — #12

- **What:** before any model download: name/version/official
  source/license/size+VRAM/intended use/smaller-or-cloud alternatives, and
  explicit approval.
- **Why open:** M3 scope; no downloads are permitted until implemented.
- **Acceptance:** approval surface implemented + enforced on the download
  path; test evidence.

## 9. First verified local AI adapter — #13

- **What:** all generation is the mock adapter; the first real local adapter
  is chosen after a hardware survey in M3.
- **Why open:** deliberate sequencing — never pre-committed.
- **Acceptance:** one real local inference adapter verified end-to-end on a
  target hardware tier.

## 10. Usability testing with target users — #14

- **What:** beginner-friendliness is a core requirement; no moderated
  sessions yet.
- **Why open:** scheduled for late milestones (M9).
- **Acceptance:** moderated sessions; findings filed; critical UX issues fixed.

## 11. FAT32 >4 GiB backup guard — edge coverage — #15

- **What:** FAT32 destinations are rejected when the *estimated* package
  exceeds 4 GiB (unit-tested via monkeypatch). Edge cases: estimate could
  undershoot on text-heavy manifests + incompressible media; the G:\ test
  drive turned out to be a disconnected reader, not a formatted FAT32 volume.
- **Why open:** implemented and tested, but real-volume verification and an
  estimate safety margin are still open.
- **Acceptance:** margin added to the estimate, or a real FAT32 volume verified.

---

### Milestone notes

- **M1.1 →** items 1, 3, 4, 5, 6, 7, 11 carried forward; FAT32 guard (#11→#15)
  implemented with monkeypatched coverage.
- **M2 →** all items re-validated; nothing closed (no new evidence). No new
  issues discovered during M2 beyond those listed. M2 storyboard queries are
  in scope of #10 (scalability validation).
- **M2.1 →** all items re-validated; nothing closed. One new tracked item
  (#12 above): `reference_asset_ids` is validated + persisted at the command
  boundary but has no shot-editor picker yet — deliberately deferred to M3+
  when generation consumes it. Migration chain now runs through `0005_m21`;
  upgrade matrix (#7) must cover `0004_m2 → 0005_m21`.
- **Integration →** PRs #2/#3/#4/#16/#17 merged into `main` as merge commits
  in dependency order (`9263024`, `e5c0294`, `00d42c7`, `0229e52`, `449ce0a`).
  Verified on integrated `main`: 89/89 tests, frontend build, fresh migration
  to `0005_m21`, packaged install + API smoke. One CI gap found and fixed:
  `tauri-check` never staged the sidecar resource dir, so it failed on every
  PR; the job now stubs the directory (bundling still uses the real sidecar
  via the `package` job). All tracked items above remain open as recorded.

---

### Milestone notes (M3 Phase 1)

- **M3 Phase 1 →** inference foundation landed on `feat/m3-generation`:
  model catalog, compatibility engine, GPU detection + scheduler, Model
  Manager rewrite, Generate workspace, `SdCppAdapter` + `generate.image`,
  migration `0006_m3`. **No model weights or runtime binaries downloaded —
  `docs/plans/M3_MODEL_PROPOSAL.md` is the approval gate for Phase 2.**
- Suite now **115/115** (26 new M3 tests). Multi-GPU parallelism is proven on
  simulated probes only; clearly labelled as such — the physical
  single-GPU path needs real weights (Phase 2).
- Item **#8 (approval surface)** now has an implemented enforcement path
  (install endpoint + confirm dialog stating size/destination/license) —
  keep open until exercised with a real download.
- Item **#13 (first verified real local adapter)** — the `SdCppAdapter`
  exists and is fake-worker-verified; stays open until real inference is
  demonstrated on the RTX 3060 Ti.
- All other tracked items remain open as recorded.


### Milestone notes (M3 Phase 2)

- **M3 Phase 2 →** first real local inference verified on the installed
  packaged build: SD 1.5 fp16 GGUF (sha256-verified) + sd.cpp Vulkan
  (`master-1073-991f825`). Three prompts generated (~8 s warm, ~2.25 GiB
  VRAM delta), cancellation + restart persistence + thumbnail integration +
  provenance all verified. Evidence: `docs/m3-evidence/`.
- **#8 (approval surface)** — real download exercised through it; closes.
- **#13 (first verified real local adapter)** — `SdCppAdapter` produced real
  images on the RTX 3060 Ti; closes.
- **New items to track:**
  - Jobs exceeding VRAM stay `queued` with no surfaced reason — add a
    queued-reason field.
  - Asset list endpoint doesn't expose `media_json` provenance (stored on
    `AssetVersion`; list view only).
  - sd.cpp `latest` tag floats — pin a release URL for reproducible installs.
  - CUDA-vs-Vulkan benchmark + auto backend selection pending (B.2/B.3).
- Suite **119/119** after live-bug fixes (sd-cli rename, img_gen mode,
  --seed flag, install-row refresh, installed-flavor preference).

### Milestone notes (M3 Phase 3)

- **Benchmark + selection hardening →** Vulkan measured 5× warm median
  **7.75 s** (prime 24.2 s incl. model load; OS-cache warm — no persistent
  worker). Recorded via `POST /api/runtimes/{key}/benchmark` on the
  flavor-keyed row. Auto-selection verified live: default request resolved
  `vulkan` — `"fastest measured backend (median 7.8s over 5 runs)"`.
- **CUDA comparison blocked:** `cuda12` build installs + verifies, but
  `stable-diffusion.dll` needs `cudart64_12.dll`/`cublas64_12.dll` from the
  separate 563 MB `cudart-sd-bin-win-cu12-x64.zip` — not downloaded,
  awaiting separate approval per download terms.
- **Closed-by-evidence:** queued-jobs now carry a reason message (verified
  live: "no device fits — job needs ~360.6 GiB VRAM…"); asset list exposes
  `media_json`+`provenance_json`; runtime releases resolve pinned tag
  metadata (`meta_json.resolved`); Backend selector shipped in the
  Generate page (`docs/m3-evidence/generate-backend-selector.png`).
- **Bug caught this round:** `RuntimeOut` response model silently stripped
  the `installs` map — the selector had no data. Fixed in schema.
- Suite **128/128** (39 M3 + 89 carried).

### Milestone notes (M3 Phase 4)

- **CUDA comparison done:** `cudart-sd-bin-win-cu12-x64.zip` (563,452,046 B,
  sha256 `fe203668…0f38d`, 3 NVIDIA CUDA-12 runtime DLLs) installed
  app-local after approval. Benchmark: **Vulkan 7.24 s median vs CUDA
  8.83 s** — Vulkan ~18 % faster on the RTX 3060 Ti; CUDA cancel verified.
  NVIDIA EULA: app-local use compliant; bundling in a public installer
  needs the EULA notice — deferred.
- **VRAM estimate fixed:** linear pixel scaling produced an absurd 360 GiB
  on a stress job → `sqrt(px_ratio)` heuristic + regression test.
- **Reliability:** runtime smoke check (`sd-cli --help`) at install + lazy
  at pick; broken installs demoted to `error` with actionable message;
  manual pick of a broken flavor errors immediately; benchmarks are
  GPU-scoped + record runtime version.
- **New tracked gap:** no persistent loaded-model worker (each job pays
  model load); surfacing worker stderr tail in job detail is a UX gap.

### Pre-merge state (M3 final)

- All M3 deliverables verified on the packaged build; suite **128/128**.
- Tracked ahead: #22 CUDA-redistribution release blocker, #23 M4.1
  persistent workers (docs/plans/M4_1_PERSISTENT_WORKERS.md), #24 worker
  stderr in job detail, #25 multi-GPU hardware validation, #26 VRAM
  calibration, #27 catalog expansion; plus pre-existing #5-#11, #14, #15.


### M4.0 Phase 1 (video foundation — pre-download)

- Suite **144/144**; `tsc -b` clean.
- `wan21.t2v.1_3b` catalog entry is `experimental` — NOT generatable until
  the approved download validates end-to-end (proposal:
  `docs/plans/M4_0_MODEL_PROPOSAL.md`).
- ffmpeg/ffprobe absent on dev machine — video validation currently falls
  back to container signatures; bundling a verified ffmpeg is a Phase-2
  proposal item.
- Same tracked blockers: #22 CUDA EULA, #23 M4.1, #24-#27, #5-#11/#14/#15.

### M4.0 Phase 2 (Wan 2.1 validation)
- Real T2V clips validated on Vulkan AND CUDA (17/17 decode-clean frames).
- Wan requires `--offload-to-cpu` at 8 GiB; Vulkan VAE on CPU (~34 min/clip),
  CUDA ~2.6 min/clip. ffprobe still absent — PyAV used as documented
  alternative; bundling ffmpeg/ffprobe remains a separate license/sourcing
  decision. Same open blockers (#22 EULA, #23 M4.1, #24 stderr, etc.).

### P0 Resource-safety incident (issue #28 — M4.0 merge blocker)
- Orphaned sd-cli after backend death identified as primary vector;
  fixed via kill-on-close Job Objects + atexit reaper + post-job dead-check.
- RAM-aware preflight/compatibility, runtime pressure watchdog (never CPU%),
  and configurable headroom thresholds added; see docs/plans/M4_0_RESOURCE_SAFETY.md.
- Controlled real-hardware reproduction still requires owner approval.

### M4.0 Phase 3 — non-inference integration (post-#28)
- Sidecar rebuilt + smoke-verified: migrations, API, auth, PyAV bundled
  (`av>=13`, existing approved dep — PyAV BSD-3; bundled FFmpeg libs are
  LGPL — keep dynamic-linking notices in installer).
- Video playback path verified: `/assets/{id}/file` serves the real Wan
  clip (200, video/webm, 447 KB).
- Honest-failure UX verified end-to-end: B2 job at 4.2 GiB free →
  `preflight memory check failed: insufficient system RAM: job needs
  ~5.8 GiB` — the residency-aware estimate flows to the user.
- ffprobe/ffmpeg still not bundled (separate source/license review).
- Open blockers unchanged: #22 EULA, #28 (merge blocker — preflight
  refusal validated, real-inference stability still unverified), #23-27.

### Licensing review — PyAV/FFmpeg (M4.0 merge audit)
- PyAV wrapper: BSD-3-Clause. **But** the wheel's av.libs bundles FFmpeg
  built `--enable-version3` WITH libx264 + libx265 (both GPL-2.0+).
  Verified by reading --enable flags embedded in the shipped DLLs.
- ACTION: `av` is EXCLUDED from the frozen installer (atomic-backend.spec)
  — the distributed artifact contains no x264/x265/FFmpeg binaries.
  media_probe degrades to container-signature validation in production.
- PyAV remains a dev/test dependency only (never shipped in that form).
- Reintroducing video probing to the packaged app requires either an
  LGPL-clean FFmpeg build or standalone ffprobe under its own license —
  separate approval item. NVIDIA EULA still tracked as #22.

### M4.0.1 — production media engine (issue #31, in progress on branch)

- Approved binary: BtbN/FFmpeg-Builds `ffmpeg-n9.0.2-48-gd4070a14ce`
  `win64-lgpl-shared-9.0`, tag `autobuild-2026-11-09-12-42`, zip sha256
  `b0981aea…2a6` (verified vs publisher manifest + API digest + local
  hash). LGPL-clean config confirmed on the shipped binaries:
  no `--enable-gpl`/`--enable-nonfree`; x264/x265/xavs2/xvid/fdk-aac
  explicitly disabled.
- Shipped: ffmpeg.exe + ffprobe.exe + 7 shared DLLs (no ffplay) +
  LICENSE.txt + NOTICE.txt (source pointer) + BUILDCONF.txt.
- `domain/media/` service: binaries (env→bundled→PATH resolution +
  toolchain provenance), supervise (proc_guard Job Objects, timeout,
  cancel), policy (ValidationSpec + documented tolerances; VFR uses
  measured decoded frames, never fps×duration), engine (probe /
  full-decode `-xerror` / nb_read_frames count / ordered validate).
- Generation commit boundary now requires `engine.validate()` pass;
  signature-only can never mark output fully validated; frozen builds
  REQUIRE full validation (missing bundled decoder = packaging bug =
  fail closed).
- Installer grew 27.67 → **74.42 MiB** (FFmpeg payload ~47 MiB
  compressed).
- PyAV remains dev-only and excluded; gyan.dev builds are GPL —
  dev-machine fixture tool only, never bundled.
- #28 stays open — critical (unchanged); #22 unchanged.

### M4.0 merged — internal development milestone (2026-11-10)

- PR **#29** merged into `main`, merge commit
  `b5c6af889b5adbdb1433a02b85ea286608f48a0b`. CI green: backend 3.12/3.14,
  frontend, package, tauri-check. Suite **167/167** on the merged tree.
- Installer rebuilt post-PyAV-exclusion: `Atomic AI
  Studio_0.1.0_x64-setup.exe` **27.67 MiB** — no `av.libs`, no x264/x265.
- This is an internal milestone only — NOT a public release and NOT a
  declaration that the #28 hardware-stability incident is resolved.
- #28 stays **open — critical**: preflight refusal was validated on real
  hardware (blocked at 4.75 GiB avail vs ~5.8 GiB B2 need), but real Wan
  inference under the new safeguards has not run; B2/B3 residency modes
  are unverified on hardware. Blocks unrestricted video-generation
  validation and public release.
- All other tracked items remain open as recorded (#22, #23–#27,
  #5–#11, #14, #15).
