# M3 Model Proposal — first real local inference model

**Status:** awaiting approval to download. This document is the Phase 1 → Phase 2
approval gate required by the M3 authorization. **Nothing is downloaded without
explicit approval.** All information below is declarative catalog data; download
sizes are from upstream repositories, VRAM figures are *estimates* (marked as
such in the UI), not guarantees.

## Recommended first model

| Field | Value |
|---|---|
| **Catalog key** | `sd15.fp16` |
| **Model** | Stable Diffusion 1.5 — `v1-5-pruned-emaonly` |
| **Format** | SafeTensors (safe format — no pickle/code execution) |
| **Version** | 1.5 |
| **Official source** | Hugging Face `sd-legacy/stable-diffusion-v1-5` → `https://huggingface.co/sd-legacy/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors` |
| **License** | CreativeML OpenRAIL-M — commercial use allowed with use restrictions (no illegal/harmful content). License URL recorded in the install row. |
| **Download size** | ~2.0 GiB (2,138,000,000 bytes declared; verified at download) |
| **Installed size** | ~2.0 GiB + runtime (~80 MiB) — install dir `models/sd15.fp16/` |
| **Integrity** | No upstream sha256 published for this file → verified by exact declared size + SafeTensors magic bytes; partial downloads resume via HTTP Range. |
| **Estimated VRAM** | ~3.6 GiB at 512×512 batch 1 (estimate; measured at run time) |
| **Requirements** | ≥4 GiB VRAM (8 GiB recommended), ≥8 GiB RAM, ≥3 GiB disk |

## Inference runtime

| Field | Value |
|---|---|
| **Runtime** | `stable-diffusion.cpp` (`sd` binary) |
| **Source** | GitHub `leejet/stable-diffusion.cpp` releases |
| **License** | MIT |
| **Version** | latest release asset at install time (recorded per install) |
| **Backend flavors** | `vulkan` (vendor-agnostic — primary on Windows), `cuda`, `cpu` |
| **Size** | ~80 MiB zipped |
| **Isolation** | subprocess worker per job — model/runtime crashes cannot corrupt the app; cancel = `terminate` then `kill` |

## Expected behavior on this machine (RTX 3060 Ti 8 GB)

- **Compatibility verdict:** `compatible` — usable VRAM ≈ 6.8 GiB (85% of 8 GiB)
  ≥ 3.6 GiB estimate + 0.7 GiB headroom.
- Expected speed: SD1.5 via Vulkan on a 3060 Ti is typically a few seconds per
  image at 512×512/25 steps — **unverified until measured in Phase 2**; the UI
  will report actual duration per job.
- SDXL Base 1.0 is listed as `limited` on this card (est ~7 GiB vs ~6.8 GiB
  usable) — visible in the manager but flagged with reasons and suggestions.

## Alternatives in the catalog (and why not first)

| Model | Status here | Why not first |
|---|---|---|
| `sd15.q8` (GGUF Q8_0, ~1.9 GiB) | compatible | Slightly degraded quality; good fallback if fp16 proves unstable — second candidate. |
| `sdxl.base` (6.5 GiB) | limited | Tight on 8 GiB; defer until fp16 baseline is verified. |
| `sdxl.turbo` | **restricted** | Non-commercial license — UI shows it, install/generate refused. |
| `flux.schnell` | **unavailable** | No verified artifact yet; needs ≥12 GiB VRAM anyway. |

## What lands before any download (Phase 1, this PR)

- Model catalog + compatibility engine + hardware profiles (E.1.1–E.1.5)
- Model Manager UI: statuses, licenses, sizes, compat badges, install/remove,
  per-download progress, storage-location config — **all install buttons gated
  behind an explicit confirmation dialog that states size, destination, and
  license**
- GPU detection (per-GPU VRAM incl. registry fix for >4 GB reporting, free VRAM
  via nvidia-smi, Vulkan/CUDA/DirectML detection, refresh endpoint)
- GPU scheduler: auto/manual device selection, one slot per GPU, VRAM-budget
  fit, multi-GPU parallel lanes (validated on simulated hardware — **not**
  physically verified; this machine has one GPU)
- `SdCppAdapter` subprocess adapter + `generate.image` command → job → CAS
  asset → `apply_command` thumbnail (audited/undoable)
- Generation workspace UI (capability-driven controls), storyboard "✦
  generate" hook, GPU preferences in Settings
- 26 M3 tests incl. fake-worker e2e (subprocess spawn → PNG → asset →
  thumbnail), simulated multi-GPU scheduling, resumable-verified-download job
  path with faked network

## Simulated vs physically verified (honesty ledger)

| Claim | Status |
|---|---|
| Single-GPU e2e pipeline | **Simulated** — fake worker proves the path; real sd.exe + real weights pending approval |
| Multi-GPU parallel scheduling | **Simulated** — scheduler tests use fabricated probes; no multi-GPU hardware available |
| VRAM figures | **Estimates** — from model anchors + activation scaling; UI labels them as such |
| Compatibility verdicts on this RTX 3060 Ti | **Computed** — from measured VRAM (registry) + estimates |

## Approval requested for Phase 2

1. Download `sd15.fp16` weights (~2.0 GiB, HF, OpenRAIL-M) — user-initiated via
   the Model Manager install button.
2. Download `sd.cpp` Vulkan-flavor runtime (~80 MiB, GitHub release).
3. Run real inference on the RTX 3060 Ti; record actual VRAM/duration and feed
   measurements back into the catalog notes.

No paid services. No other models. No Milestone 4 work.
