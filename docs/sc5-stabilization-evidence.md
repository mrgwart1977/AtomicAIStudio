# S-C5 stabilization — findings and evidence

Branch: `feat/sc5-stabilization` · Date: 2026-11-14 · Machine: Windows 11,
RTX 3060 Ti 8 GiB VRAM, 16 GiB RAM (~75 % committed during testing), dev
box — measurements are one-machine baselines, not universal guarantees.

This milestone validated the **installed Windows application** through
real WebView2/CDP automation plus live backend probing — not backend
tests alone. It found and fixed one critical packaging defect and two
real UX-blocking defects that source-level tests could not see.

## 1. Findings — defects found by installed-app testing

| # | Defect | Severity | How found | Fix |
|---|--------|----------|-----------|-----|
| F1 | Frozen sidecar missing `httpx` — installed backend crashed at startup (`ImportError` in `rest_providers.py`). Dev installs masked it because `httpx` lives in dev extras. | **Critical** (app dead on fresh install) | Installed-app launch; stderr captured | `httpx` moved to prod deps; CI `package` job now runs a frozen-exe `/health` smoke gate so this class of bug can't recur |
| F2 | `commitCommand` returned the new revision but never resynced the store pin. Any UI action issuing sequential commits deterministically 409'd on the second (`expected 16, current 17` captured live). | **High** (First Film saves fail) | CDP network capture on installed build: one click → `200` rev 16→17 → `409` | Pin-sync centralized in `commitCommand`; monotonic `applyCommitRevision` in the store; regression tests |
| F3 | Stale in-flight `refreshProjects`/`refreshProjectData` GETs could overwrite a newer local revision after a commit landed mid-flight — same 409 class, different path. | High | Step-8 "Build timeline" 409 on the *fixed* build (`expected 32, current 33`) | Monotonic guards in both refresh paths; race regression test |
| F4 | Invalid media (text renamed `.mp4`) imported as `status=ready`, `kind=video`, `media_json={}` — probe failure silently swallowed. | Medium (false-clean asset) | Live import of fake mp4 | Probe result now persisted: `media_json.probe.ok=false` + reason + `warnings[]`; regression test; verified in shipped sidecar |

## 2. 99-step checklist — classification

Classes: **A** = automatable (API/backend evidence suffices) ·
**W** = requires WebView2 interaction · **H** = requires human visual
judgment · **S** = requires special hardware/model availability.
A `+` means a second class also applies.

| Steps | Class | Notes |
|-------|-------|-------|
| 1–7, 9–12 | W+H | Canvas/paint pixel alignment is inherently visual |
| 8 | S | Pen input requires pen hardware |
| 13–16, 19–22 | W | UI action; backend outcome is API-verifiable |
| 17–18, 23–25, 30, 32, 34 | A | sha256/provenance/refusal/fingerprint checks |
| 26–29, 31, 33 | W | Picker/dialog UI; outcomes API-verifiable |
| 35–38 | W+H | Card/badge/compare-dialog wording + visuals |
| 39–41, 44 | W(+A) | Persistence verifiable via API; disclaimer needs UI |
| 42, 45–46 | S+W | Needs an installed video model |
| 43 | A | Stale-preview 422 (verified live) |
| 47, 52 | W | Settings/queue-reason surfaces |
| 48–50 | A(+S) | Scheduler/admission logic; real GPU/drive for full fidelity |
| 51 | A+W | **Verified live** — see §5 |
| 53–54, 56, 58, 61 | A+W | Command layer verified; UI acceptance needs WebView2 |
| 55, 57, 59–60, 62–67 | A | Duplicate-guard/trim/EDL/revert/export all API-verified |
| 68–75 | A(+W) | Composed-prompt determinism/omissions/conflicts |
| 76–86, 88 | W | **Verified live** on installed build — see §3 |
| 87, 89 | W(+H) | Delete-notice; keyboard a11y judgment |
| 90–93 | W(+A) | Cards/dialogs; compat/validate/preflight APIs verified |
| 94–96 | S | Needs installed sd.cpp model + GPU |
| 97 | A | 422 verified by unit test |
| 98 | S(+A) | GPU-less machine; simulated-probe tests cover logic |
| 99 | S | Needs multi-GPU hardware |

**Result:** ~40 steps A, ~41 steps W (of which ~11 need H), ~8 steps S.
All 4 classes now have a repeatable execution path: A via pytest/API,
W via `scripts/cdp-acceptance.cjs` on the installed build, H via the
manual checklist, S documented as requiring the target hardware.

## 3. Installed-app evidence (this milestone, live captures)

- Fresh NSIS install → first launch → sidecar healthy (`/health` ok)
- WebView2 CDP live on `tauri.localhost` — `scripts/cdp-acceptance.cjs`
- Wizard steps 1–5: rename + shape/fps save, character "Ada", scene
  "Rooftop at dusk", shots "Opening wide" + "Close-up rain" — all
  persisted, sequential commits succeed post-F2/F3 fixes
- Native OS file picker: real Win32 "Choose a video clip" dialog
  driven via SendKeys; path landed; MP4 attached to each shot
- Step 8 error banner with expandable technical details (step 88)
- Durable wizard resume observed across driver sessions (step 85)
- Timeline: 2 clips inserted; trim `0–48 → 4–40` verified; move
  `to_index=1` reordered correctly; `ade-edl/1` EDL reflects edits
- Export job `23268b9f` succeeded →
  `…\jobs\{project}\23268b9f-…-export.mp4` — ffprobe-verified
  h264, 640×360, 3.500 s (playable, geometry matches profile)
- `assets.verify` committed clean — CAS integrity confirmed

## 4. Failure/recovery evidence (live, installed backend)

| Scenario | Result |
|----------|--------|
| Backend killed ~0.2 s after export commit | On restart: `failed — interrupted by application stop; not resumable`; **no asset registered** for the job |
| Missing source path | Job `failed — file not found: …` |
| Invalid media (`fake.mp4`) | Job succeeds (CAS ingest is legitimate) but `media_json.probe.ok=false` + `reason` persisted — asset flagged unprobeable, never implied clean |
| Stale approval | `timeline.clip.move` with fingerprint pinned at rev 3 after a bump to rev 4 → **422 "timeline changed since preview — re-preview"** |
| Cancel queued export | `cancelled before it started`; first job completed normally |
| Reopen project after restarts | All entities/timeline/clips persisted across three restarts |
| Sidecar cleanup | Zero `atomic-backend`/ffmpeg/worker processes after app exit (two builds) |
| Insufficient disk | Code-verified: `_disk_preflight` blocks gen jobs honestly (`free < needed + min_disk_free`); live fill-the-disk test impractical — documented |

**Known limitation (documented):** cooperative cancellation cannot
interrupt a fast blocking ingest/hash — a 500 MiB import completed
before cancel landed. Cancellation is deterministic for *queued*
jobs; running-job cancellation is best-effort by design.

## 5. Catalog hardening (verified-only metadata)

- `sdxl.base`: `sd_xl_base_1.0.safetensors` — size corrected to
  **6,938,078,334 B**, sha256 `31e35c80…` (Hugging Face API verified)
- `sdxl.turbo`: `sd_xl_turbo_1.0_fp16.safetensors` — size corrected to
  **6,938,081,905 B**, sha256 `e869ac7d…` (HF API verified)
- `wan21.t2v.1_3b`: `durations_s` added — derived from declared
  frames×fps, no invented values
- FLUX / Qwen entries: kept honest — `(no files)` with `experimental`/
  `planned` support; `gen.image.sdcpp` rejects experimental models and
  `sdcpp-server` is env-gated, so pinning unverifiable artifacts would
  falsely claim installability. Remaining gap documented.

## 6. Compatibility classification & validation severity

- `availability_reason` on every model entry + preflight:
  `insufficient_ram` / `insufficient_vram` / `missing_runtime` /
  `missing_files` / `restricted_license` / `no_downloadable_artifact` /
  `planned` / `unknown` — no more uniform "blocked".
- Validation checks carry `severity`: `blocking` (integrity/security/
  required-runtime), `warning` (optional/advisory), `info`
  (opt-in/skipped disclosures). Report success fails only on blocking;
  rollup counts exposed. UI shows reason labels + severity-aware colors.
- Regression tests: `test_c5e_compat.py` (+5), `ModelManagerPage.test`
  fixtures, `commands.test.ts` (+5 revision/race tests),
  `test_s3_asset_media.py` (+1 probe-failure test),
  `test_m3.py` hash-fixture corrected (all catalog files now hash-pinned).

## 7. Performance baseline (this machine, this workload)

| Metric | Value | Method |
|--------|-------|--------|
| Backend→healthy | 2.63 s cold / 2.57 s restart | spawn→`/health` 200 |
| Desktop→window | 5.64 s | spawn→CDP page live |
| Idle backend RSS | ~129 MB | process working set |
| GPU probe | 2.36 s | `/api/system/resources` first call |
| Compatibility matrix | 32 ms | endpoint timing |
| Video import (65 KB mp4) | ~0.57 s | job commit→terminal |
| Timeline ops | ~40–55 ms | commit latency |
| 720p export (2 clips, 3.5 s) | 1.60 s job / 114 KB out | job duration + ffprobe |
| Reconciliation after kill | <15 s incl. relaunch | observed |

## 8. Resource & job safety

- Job reconciliation on restart: interrupted → honest terminal state ✓
- GPU scheduler fairness: ops-lane separation retained; benchmark moved
  to `gen` lane in C5-E review (no bypass of admission) ✓
- Export releases reservations: terminal-state jobs hold no slots ✓
- Orphan processes: none after shutdown on both builds ✓
- Temp files: job work dirs contain only durable outputs (mp4/png/webm
  artifacts), staging swept ✓
- CAS: `assets.verify` clean; imports dedupe by sha256 ✓
- Disk admission: gen jobs blocked honestly; import ingest failure =
  honest job failure (no partial asset) ✓

## 9. Outstanding release blockers (unchanged)

- Issue #96 — mandatory before live artifact retrieval
- Stage B2b — ON HOLD
- RELEASE_CHECKLIST items #1–#15, #22–#27 as recorded
- Steps 42/45/46/94–96/98/99 need hardware not present on this box
- Steps 1–12, 35–38, 89 need a human visual pass
