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

### M4.0.1 — production media engine (issue #31, MERGED via PR #32)

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

### Rebrand — Atomic Dream Engine (PR #34, MERGED)

- Product renamed: **Atomic AI Studio → Atomic Dream Engine**
  (`productName`, window title, binary `atomic-dream-engine.exe`,
  bundle identifier `com.atomicdream.engine`, docs/plans). Supplied
  hexagon logo drives the TopBar wordmark and the full Tauri icon set.
- **Data directory unchanged on purpose:** `%LOCALAPPDATA%\AtomicAIStudio`
  is hardcoded in `src-tauri/src/main.rs` and `backend/app/config.py` —
  the name predates the rebrand and is retained so existing databases,
  projects, models, runtimes, CAS assets, and backups keep working.
  The bundle identifier does not drive this path. Do not rename without
  a full migration design (detection → backup → copy → integrity check).
- **Installer coexistence:** the new NSIS installer registers as a
  different product (`Atomic Dream Engine`, new identifier/install dir).
  An old "Atomic AI Studio" install may coexist side-by-side — and both
  point at the SAME `%LOCALAPPDATA%\AtomicAIStudio` data directory and
  `atomic.db`. Implications:
  - Once the new build migrates the DB to `0007_m4`, an old install
    crashes on startup ("can't locate revision") — fail-closed but
    with an opaque error; a friendly newer-revision guard is proposed.
  - There is currently **no single-instance guard**: two backends can
    open the same data dir concurrently. WAL + busy_timeout prevent
    file-level corruption but two JobEngines could double-claim the
    same queued job. A data-dir lock file is proposed (see below).
- **Hardening shipped (issue #36, PR #37):**
  1. `<data_dir>\.atomic.lock` — OS byte-range lock (`msvcrt.locking`)
     held for backend lifetime; auto-released on exit/crash; second
     backend exits before migrations/jobs/workers with a clear
     "already in use" error. The file itself is never the lock and is
     never deleted.
  2. Pre-migration revision check: an `alembic_version` unknown to the
     running build aborts startup with "database was created by a newer
     version" — never downgraded, rewritten, or recreated.
  3. Tauri shell fails fast if the sidecar dies during startup instead
     of waiting out the 30 s readiness timeout.
- WebView2 user-data folder follows the identifier
  (`%LOCALAPPDATA%\com.atomicdream.engine`) — harmless fresh state;
  the frontend persists nothing there.
- Uninstall removes the install dir only; the data dir is never
  deleted by the installer.

### Open security blockers (tracked)

- **#96 — DNS resolution + resolved-IP filtering (release/security
  blocker):** before any real image-provider artifact retrieval may be
  enabled, DNS must be resolved at connection time and every resolved
  address filtered — DNS-rebinding resistance, private/reserved-range
  refusal (loopback, RFC-1918, link-local/metadata 169.254.169.254,
  CGNAT, multicast, unspecified), and no proxy/routing path that
  bypasses the destination check. Cannot be bypassed by provider
  catalog configuration or owner approval. Does not affect the
  current simulated provider boundary (MockTransport — no DNS).

### N1 milestone note (multitrack timeline — N1-A through N1-E merged)

Classification legend: **[auto]** verified automated test ·
**[installed]** verified on installed WebView2 build ·
**[human]** requires human visual judgment ·
**[hw]** hardware/model gated · **[blocked]** blocked ·
**[notrun]** not executed.

- N1-A data foundation (migrations, track model, EDL v2 schema) — **[auto]**
- N1-B multitrack editor + NTSC rates + lane ops — **[auto]** + **[installed]** (12/12)
- N1-C split/ripple/trim/gap ops + revision safety — **[auto]** + **[installed]** (14/14)
- N1-D composite export (`ade-edl/2`, ffmpeg, cancel/recovery) — **[auto]** + **[installed]** (ffprobe-verified composite)
- N1-E undo/redo + stable clip identity + viewport culling — **[auto]** + **[installed]** (8/8 + perf matrix)
- N1 end-to-end audit (18-step workflow in installed app) — **[installed]** `docs/n1-audit-evidence/`
- Installed UI perf 10/100/500/1000 clips — **[installed]** `docs/n1e-evidence/n1e-perf.json`
- Export correctness matrix (fps 24/25/30/24000-1001/30000-1001, gaps, hidden/muted, stacking) — **[auto]** parametrized plan tests + e2e renders
- Reliability (cancel, missing source, hash mismatch, ffmpeg fail, disk guard, reservation release, temp cleanup, phantom-output prevention, orphan-proc) — **[auto]** adapter tests; backend-kill + low-disk on real hardware **[notrun]** (covered by guards/tests, not destructive live runs)
- Frame-accurate compositing beyond automated checks (pixel-level layer order on the N1-D artifact predates the `aa7034e` stacking fix; corrected order is deterministic-test-covered) — **[human]** optional re-render for pixel confirmation
- Human-drag latency, real hands-on UX feel — **[human]** (instrumented CDP timings only)
- Large-project scalability (#10) — partially advanced by the 1000-clip perf evidence; TB-scale validation stays open
- All previously tracked blockers remain open as recorded — incl. **#96** (mandatory live-artifact-retrieval blocker), #5-#15, #22-#27.
- **N1 is development-complete; this is not production release readiness.**

### N2 milestone note (audio editing — N2-A..N2-E merged; N2-F in review)

- N2-A audio foundation (audio-capable lanes reuse `TimelineTrack.kind="audio"`; sample-exact clip model; CAS-pinned sources; probe metadata: codec/rate/channels/layout/sample count) — **[auto]** + **[installed]** (7/7, `docs/n2a-evidence/`)
- Audio import via durable jobs (probe, fail-closed invalid input, cancel/restart reconcile) — **[auto]** + **[installed]** (real WAV `pcm_s16le` 48 kHz stereo, exact 48,000 samples)
- Lane↔asset kind rule (video→audio lane refused, audio→video lane refused, both directions verified) — **[auto]** + **[installed]**
- Honest audio-lane export refusal (backend fail-closed; UI badge discloses reason via title) — **[auto]** + **[installed]**
- EDL v2 carries audio stream identity (rate, channels, layout, samples, exact) without losing source pins — **[auto]** + **[installed]**
- Undo/redo across audio clips with stable clip identity — **[auto]** + **[installed]**
- Mixed 44.1 kHz / 48 kHz sources, sample↔timeline rational conversion — **[auto]**
- Audio-lane rendering/mixing in export — **implemented in N2-E** (see N2-E row below); pre-N2-E refusal replaced by real rendering
- **N2-B waveform envelopes:** `asset.waveform` durable job → streaming decode → deterministic min/max LOD levels in CAS pinned to source sha — **[auto]** + **[installed]** (9/9, `docs/n2b-evidence/`; rerun skips; stale-version manifest → honest 409)
- Waveform canvas inside audio `LaneClip`s — viewport-culled, zoom-aware LOD switching, loading/missing/error+retry states, pointer-transparent — **[auto]** + **[installed]**
- Waveform perf — `[auto]` bench (`backend/bench_waveform.py`): 5 min build 1.5 s; 1 h 12.9 s / 5.7 MB blob / 57 MB peak; 3 h 36.3 s / 4.3 MB blob / 43 MB peak (bucket-capped); 102-clip multi-lane scroll 60 fps / 7 MB heap / 0 revisit fetches **[installed]**
- Waveform timing integrity — absolute source-sample buckets; `clipWaveWindow` clamps to `[in,out]` so trims never display discarded/beyond-out audio; zoom changes LOD never timing — **[auto]** (unit tests incl. nonzero in/out, split halves, 44.1 kHz, 24000/1001)
- Waveform serve-time integrity — CAS blob sha re-verified per slice; corrupt/unparseable/mismatched-source blobs → 409 — **[auto]** + **[installed]**
- Waveform editing installed coverage — move, head+tail trim, split (contiguous source halves), undo/redo stable id, restart persistence, export refusal — **[installed]** (14/14, `docs/n2b-evidence/`)
- **N2-C gain + track controls:** per-clip + per-lane `gain_db` (dB only, −60…+12; linear = 10^(db/20) at mix), lane `solo`; `timeline.clip.gain` + extended `timeline.track.update`; EDL v2 metadata; precedence visible→mute→solo — **[auto]** + **[installed]** (25 backend + 4 UI tests incl. real 0014→0015 upgrade, NaN/±inf refusal, a/v-separation proofs; 14/14 installed `docs/n2c-evidence/` incl. UI solo/mute/reset/locked-mixer/restart)
- **N2-D fades/crossfades/automation:** per-clip source-unit fades (linear + equal-power curves, edge-anchored), explicit `timeline_crossfades` records borrowing source handles (seam-containing windows only; read-time `satisfied` flag), bounded ≤64-pt clip gain automation (linear-in-dB); N2-E effective-gain contract pinned; UI wedges+apex drags+inspector sections — **[auto]** + **[installed]** (28 backend + 3 UI tests incl. seam/coverage/cross-project refusals; 14/14 installed `docs/n2d-evidence/` incl. UI curve-select + keyframe editing + restart)
- **N2-E audio mixing & export:** dedicated audio lanes render through the composite graph (atrim source-units → **asetpts PTS reset** → aeval gain+automation → afade fades/xf → resample-last → sample-exact silence placement → amix normalize=0 → alimiter limit=0.95/level=0/latency=1 → AAC 48k stereo); audibility = visible+mute+solo; satisfied crossfades borrow source handles; detached-xf on audible lane fails closed; audio-only renders black canvas; export UI discloses mix content — **[auto]** (52 backend tests: plan units + real-ffmpeg PCM-measured renders — fps-matrix onsets, click/AAC-pipeline control, xf envelope shapes vs math contract, combined gain stages, limiter ceiling/latency/quiet-honesty, embedded mute/solo/hidden picture+audio, cancel/missing/hash-mismatch/ffmpeg-nonzero/disk-space/cleanup — + 6 exportMode UI tests) + **[installed]**
- N2-E review fix: afade anchors on frame PTS — `asetpts=PTS-STARTPTS` must precede envelope filters or fades on trimmed/head-borrowed streams silently no-op — **[auto]** (regression-pinned in chain-shape test + full-window xf envelope measurements)
- N2-E durability: v2-path cancel mid-render cleans work dir, missing/hash-mismatched/undecodable sources fail closed, ffmpeg nonzero cleans partial output, disk-space estimate refuses pre-render, interrupted-job reconcile on startup — **[auto]**
- **N2-F audio preview + remaining features:** WebAudio preview of audible audio-lane clips — `buildPreviewPlan` ports `composite_plan`'s audio section + `_apply_xf` + `fade_gain`/`automation_gain_db` verbatim; authenticated `fetchAssetObjectUrl` → `decodeAudioData`; per-clip `setValueCurveAtTime` effective-gain envelope (64 Hz, same math, applied once); transport play/pause/stop synced to `playheadF` with rAF; ruler seeks re-schedule while playing (short-range scrub); detached xf fails closed (plan lists it; export still refuses) — **[auto]** (15 previewPlan unit tests: audibility, dB-sum-once, fade/automation/xf envelopes vs math contract, fail-closed) + **[installed]** (transport live-checked: decode+schedule, playhead advances, pause freezes, stop rewinds)
- **N2-F extra fade curves:** `s_curve` → afade `hsin` (sin²/cos², unity-power), `exponential` → afade `exp` (the real FFmpeg contract e^{−11.5129(1−t)}/e^{−11.5129t} — verified against `af_afade.c` and PCM, not guessed) — **[auto]** (PCM-measured full-window renders) + **[installed]**
- **N2-F automation interpolation:** per-clip `automation_interp` (`linear`|`step`), column `0017_n2f`, command payload + preview-time validation, EDL sibling field, aeval step segments, UI select, split/duplicate/snapshot preservation — **[auto]** (command/render/persistence tests) + **[installed]**
- N2-F FILM acceptance (installed): 2 video lanes (embedded audio + muted b-roll) + dialogue/music/sfx lanes @30000/1001 — every curve, both interps, xf, step-duck, gap, mixed 44.1/48k — ffprobe (h264/aac 48k stereo/duration) + decoded-PCM per-element checks + cancel + perf — **[installed]** `docs/n2f-evidence/`
- Embedded video audio in WebAudio preview — **[notrun]** deferred (documented in `docs/n2f-preview.md`); render path covers it
- Per-track automation — **[notrun]** deferred (architecture sketched in `docs/n2f-preview.md`); needs new model dimension + editor
- Preview↔render sample-exactness — **[notrun]** preview is an editorial aid (64 Hz envelope, WebAudio resampling) — documented, not claimed
- Human: audible preview/scrub judgment, transport feel — **[human]**
- Human audio-edit UX feel, drag on audio lanes — **[human]**; audible fade/crossfade/mix judgment vs reference — **[human]** (N2-E renders it; ears required); waveform accuracy vs a known-good editor — **[human]**
- All previously tracked blockers remain open as recorded — incl. **#96**; Stage B2b remains ON HOLD.
