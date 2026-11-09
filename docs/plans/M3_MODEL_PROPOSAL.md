# M3 Model Proposal — first real local inference model

**Status:** awaiting approval + candidate selection. This document is the
Phase 1 → Phase 2 approval gate required by the M3 authorization. **Nothing is
downloaded without explicit approval.** All figures below were verified
against live upstream metadata on 2026-11-09; VRAM figures remain *estimates*
until Phase-2 measurement.

---

## Pre-download verification (user-requested checks)

### 1. Exact model artifacts — verified live

| Entry | Repo | File | Verified size | SHA-256 | Exists/downloadable |
|---|---|---|---|---|---|
| `sd15.fp16` | `second-state/stable-diffusion-v1-5-GGUF` | `stable-diffusion-v1-5-pruned-emaonly-f16.gguf` | **2,132,586,944 B (1.99 GiB)** | `da017009aa86a3f46468857d95833435fe55e4a2856afab76ba7722ef8ee4d8b` | ✅ 200 |
| `sd15.q8` | `second-state/stable-diffusion-v1-5-GGUF` | `stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf` | **1,763,578,176 B (1.64 GiB)** | `d0555243938c62faeefb4ac93f6c7a053ad373a4290c5256bce229aeb193bf94` | ✅ 200 |
| `sd15.fp32` | `stable-diffusion-v1-5/stable-diffusion-v1-5` | `v1-5-pruned-emaonly.safetensors` | **4,265,146,304 B (3.97 GiB)** | `6ce0161689b3853acaa03779ec93eafe75a02f4ced659bee03f50797806fa2fa` | ✅ 200 |

**Corrections this review caught:**
- The earlier `sd15.fp16` URL pointed at the **fp32** 4.26 GB file — no official
  fp16 safetensors exists; the true fp16 artifact is Second State's `f16.gguf`.
- The earlier `sd15.q8` filename (`stable-diffusion-v1-5-Q8_0.gguf`) **404'd**;
  the real file is `stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf`.
- `sd-legacy/stable-diffusion-v1-5` is a redirect alias; the canonical repo is
  `stable-diffusion-v1-5/stable-diffusion-v1-5` (the one sd.cpp's README cites).

**Official vs conversion:** the fp32 safetensors is the **official** Stability
artifact. The f16/Q8_0 GGUFs are **community conversions** by Second State
(the de-facto GGUF source referenced by sd.cpp docs) of those same weights —
same OpenRAIL-M terms. sd.cpp also supports convert-mode if a local conversion
path is ever preferred.

### 2. Runtime compatibility

| Field | Value |
|---|---|
| **Release pinned** | `master-1071-cfdf3cf` (published 2026-11-09 — queried live) |
| **Binary inside zip** | `sd-cli.exe` — **not** `sd.exe` (renamed; catalog fixed) |
| **Format support** | `.safetensors` AND `.gguf` natively (README §Model Formats) |
| **CLI flags verified** | `-m/--model`, `-p/--prompt`, `-n/--negative-prompt`, `-o/--output`, `-W/-H`, `--steps`, `--cfg-scale`, **`--seed`** (not `-s` — that is `--strength`; adapter fixed), `--batch-count`, `--sampling-method`, `-M/--mode` |
| **Windows assets** | `sd-master-cfdf3cf-bin-win-vulkan-x64.zip` **30.0 MB** · `-win-cuda12-x64.zip` **367.0 MB** · `-win-cpu-x64.zip` **18.2 MB** |
| **Asset matching fix** | patterns tightened to `win-vulkan`/`win-cuda`/`win-cpu` — bare `vulkan`/`cuda` also matched Linux zips and the 563 MB `cudart-` bundle |

**Backend first: Vulkan.** 30 MB vs 367 MB, zero CUDA-runtime dependencies,
vendor-agnostic (consistent with the no-NVIDIA-assumption mandate), and
sd.cpp's Vulkan backend is mature for SD-class models. CUDA remains a
one-click alternative in the Model Manager; CPU is the no-GPU fallback.

### 3. Licensing

- **License:** CreativeML OpenRAIL-M (repo metadata: `license:
  creativeml-openrail-m`; canonical text:
  `huggingface.co/spaces/CompVis/stable-diffusion-license`).
- **Commercial use:** allowed — the license permits commercial use subject to
  the use restrictions (no illegal/harmful content categories, documented in
  the catalog + shown in the install dialog).
- **Redistribution:** the license permits copying/sharing weights with the
  license attached — but Atomic AI Studio **does not bundle weights** anyway
  (requirement G.6). Each user downloads separately from Hugging Face — the
  compliant-by-construction path.
- **UI surface:** license name + notes + `commercial_ok` display on every
  catalog card and in the install-approval dialog; license name recorded in
  each `model_installs` row.

### 4. Performance expectations — honest labeling

- The "2–4 s per 512×512 image" figure was an **unverified estimate** — it is
  labeled as such in the catalog/UI.
- Phase 2 will record per job: actual duration, peak VRAM (nvidia-smi where
  available), resolution, steps, sampler, GPU index, runtime flavor — stored
  in job progress + asset provenance.

### 5. Extensible catalog — delivered this PR

Modalities (`image|video|audio|multimodal`), modes, support tiers
(`production|tested|experimental|planned`), compat statuses
(`compatible|limited|unsupported|unknown|restricted|unavailable|planned`),
install states (`not_installed|downloading|verifying|installed|failed|removed`).
Entries: SDXL, FLUX (schnell + dev), Qwen-Image, LTX-2.5 (official release
verified Aug-2026), Wan 2.2 TI2V-5B, HunyuanVideo — video entries carry
metadata only; **zero video weights or inference code in M3**.

### 6. Final download proposal (pending your pick)

| Field | Recommended (A) | Alternative (B) |
|---|---|---|
| **File** | `stable-diffusion-v1-5-pruned-emaonly-f16.gguf` | `stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf` |
| **URL** | `huggingface.co/second-state/stable-diffusion-v1-5-GGUF/resolve/main/…f16.gguf` | same repo, `…-Q8_0.gguf` |
| **License** | CreativeML OpenRAIL-M (commercial OK w/ use restrictions) | same |
| **Size** | 1.99 GiB, sha256-verified | 1.64 GiB, sha256-verified |
| **Disk** | ~2 GiB + ~30 MiB runtime | ~1.7 GiB + ~30 MiB |
| **VRAM (est.)** | ~3.6 GiB peak — fits 3060 Ti w/ headroom | ~3.0 GiB — fits everywhere |
| **Runtime** | sd.cpp `master-1071-cfdf3cf`, `sd-cli.exe`, Vulkan x64 | same |
| **Limitations** | community conversion (not Stability-official); SD1.5-era fidelity; no img2img/ControlNet in M3; est. figures unverified | same + slightly lower fidelity vs fp16 |
| **Official alternative** | `sd15.fp32` (4.26 GB Stability artifact) if provenance purity outweighs size — classifies *limited* on 8 GiB | — |

## What landed before any download (Phase 1, this PR)

- Extensible capability-aware catalog + compatibility engine + hardware profiles
- Model Manager UI: modality/support badges, licenses, sizes, compat reasons,
  install/remove, per-download progress, storage-location config — **install
  gated behind an explicit approval dialog**; planned/experimental/unavailable
  entries cannot be installed
- GPU detection (registry VRAM >4 GB fix, free VRAM via nvidia-smi,
  Vulkan/CUDA/DirectML, refresh) + GPU scheduler (auto/manual, slot per GPU,
  multi-GPU lanes — simulated tests only)
- `SdCppAdapter` subprocess adapter + `generate.image` command → job → CAS
  asset → `apply_command` thumbnail (audited/undoable)
- Generation workspace UI (capability-driven), storyboard "✦ generate" hook,
  GPU prefs in Settings
- 30 M3 tests incl. fake-worker e2e, planned-model rejection at install +
  command layers, simulated multi-GPU scheduling, faked-network download job

## Simulated vs physically verified (honesty ledger)

| Claim | Status |
|---|---|
| Single-GPU e2e pipeline | **Simulated** — fake worker proves the path; real sd-cli + weights pending approval |
| Multi-GPU parallel scheduling | **Simulated** — fabricated probes only |
| VRAM figures | **Estimates** — catalog anchors + activation scaling; labeled in UI |
| Artifact metadata | **Verified live** — sizes + sha256 from HF tree API, release assets from GitHub API |
| Video model metadata | **Verified sources**; **zero inference validation** |

## Approval requested for Phase 2

1. **Pick a first model** — A (`sd15.fp16` GGUF, recommended), B (`sd15.q8`,
   smallest), or the official `sd15.fp32` safetensors.
2. On approval: download the chosen weights + sd.cpp Vulkan runtime via the
   Model Manager (user-initiated, checksummed, resumable).
3. Run real inference on the RTX 3060 Ti; record actual VRAM/duration/backend
   and feed measurements back into the catalog.

No paid services. No video-model downloads. No Milestone 4 work.

---

# Phase 2 — executed 2026-11-09 (SD 1.5 fp16 GGUF + sd.cpp Vulkan)

User approved Option A. Downloads ran through the Model Manager approval flow
on the installed packaged build (NSIS, `%LocalAppData%` data dir).

## Downloaded artifacts (measured, not catalog values)

| Artifact | Resolved version | Bytes | Integrity |
|---|---|---|---|
| `stable-diffusion-v1-5-pruned-emaonly-f16.gguf` | — | 2,132,586,944 | sha256 `da017009…4d8b` verified at install |
| `sd-master-008b9a2-bin-win-vulkan-x64.zip` | release `master-1073-991f825` | ~30 MB | GitHub-hosted release asset; `sd-cli.exe` presence verified post-extract |

Note: `latest` resolved to `991f825` at install time (newer than the
`cfdf3cf` referenced in Phase 1 verification) — the resolver records the exact
commit per install; reproducibility would require a pinned release URL.

## Real generation measurements — RTX 3060 Ti, Vulkan backend

| Run | Prompt | Steps | Seed | Duration | VRAM |
|---|---|---|---|---|---|
| cold (model load + gen) | cabin at dusk | 25 | 424242 | **69.2 s** | — |
| warm #1 | red dragon on mountain | 20 | 42 | **7.65 s** | — |
| warm #2 | fishing boat, fog | 25 | 7777 | **8.86 s** | **2,306 MiB delta** (2,061→4,367 MiB total) |
| warm #3 | ancient library | 20 | 31415 | ~8 s (succeeded) | — |

Measured model+inference footprint: **~2.25 GiB** over baseline — catalog
estimate 3.6 GiB was conservative. Prior "2–4 s" estimate was ~2× optimistic;
real warm time ≈ **8 s** at 512×512, 20–25 steps, euler_a.

## Verification results

| Check | Result |
|---|---|
| sha256 of weights | verified at install, `install.verified=True` |
| Runtime provenance | release zip from GitHub, `sd-cli.exe` confirmed inside |
| Real t2i inference | ✅ 3 distinct prompts, coherent PNGs (see `docs/m3-evidence/gen1-dragon.png`) |
| Cancellation | job cancelled at 24 % — worker process killed, VRAM back to baseline |
| CAS asset storage | 4 generation assets persisted, `origin=generation` |
| Provenance | full `media_json` on `AssetVersion`: model/flavor/prompt/params/seed/duration/GPU/timestamp |
| Storyboard thumbnail | ✅ shot `62c3acfb` shows generated image on the board |
| Restart persistence | model + runtime rows + assets survive app restart |
| Missing model | `sd15.q8` generation → honest "not installed" 422 |
| Restricted/planned gates | SDXL Turbo 422, video models 422 — verified |

## Bugs found by live Phase 2 testing (all fixed)

- `sd.exe` → `sd-cli.exe` rename in current sd.cpp releases (exe_name + test fake)
- `-M txt2img` → `-M img_gen` (mode renamed upstream); `-s` is strength, not seed → `--seed`
- `install_row()` reuse path left stale `file_name`/`install_root`/`version` — refresh on reuse
- Default runtime-flavor resolution picked an uninstalled flavor when the
  hardware probe result differed from install state → `_pick_flavor` now
  prefers **installed** runtime flavors over probe-order guesses (B.3)

## Known limitations (Phase 2, honest)

- Jobs that exceed VRAM stay `queued` indefinitely with no surfaced reason —
  needs a queued-reason field in a later milestone.
- Asset **list** API doesn't join `AssetVersion.media_json`; provenance is
  stored but only visible via DB/detail endpoints.
- Release tag floats (`latest`) — pin a release for reproducible installs.
- CUDA comparison + auto-benchmark selection NOT done — tracked for later.
- `2–4 s` estimate retired: real ≈ 8 s warm, ~70 s cold.
