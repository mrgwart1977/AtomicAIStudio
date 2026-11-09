# M3 Model Proposal — first real local inference model

**Status:** awaiting approval + candidate selection. This document is the
Phase 1 → Phase 2 approval gate required by the M3 authorization. **Nothing is
downloaded without explicit approval.** All information below is declarative
catalog data; download sizes are from upstream repositories, VRAM figures are
*estimates* (marked as such in the UI), not guarantees.

---

## Extensible model catalog (implemented this PR)

The catalog is now capability-aware and multi-modal — not built around one
image model. Every entry declares:

- exact model name, version, family, format, adapter
- official source (provider + repo/URL) and license (name, URL, `commercial_ok`, notes)
- **modality** — `image | video | audio | multimodal`
- **modes** — `text2image | image2image | text2video | image2video | audio2video | …`
- hardware requirements: min/recommended VRAM, RAM, disk, required GPU backends
- download file set (size + URL + optional sha256) — **empty means not installable**
- capabilities: resolutions, aspect ratios, durations, fps, samplers, steps/CFG
  ranges, batch, seed, precision, reference-image, LoRA, character-consistency,
  synchronized audio, multi-shot
- **support tier** — `production | tested | experimental | planned`
- local runtime key + install requirements

**Catalog presence never implies local support.** A `planned` entry can never
be installed (422), generate (422), or be offered to the user as usable — it
exists so the schema, UI, and roadmap carry real per-model metadata now.

### Current entries

| Key | Modality | Modes | Support | License (commercial?) | Runtime | On RTX 3060 Ti |
|---|---|---|---|---|---|---|
| `sd15.fp16` | image | text2image | production | OpenRAIL-M (yes) | sdcpp | compatible |
| `sd15.q8` | image | text2image | production | OpenRAIL-M (yes) | sdcpp | compatible |
| `sdxl.base` | image | text2image | production | OpenRAIL++-M (yes) | sdcpp | limited |
| `sdxl.turbo` | image | text2image | tested | Stability Non-Commercial (**no**) | sdcpp | restricted |
| `flux.schnell` | image | text2image | experimental | Apache 2.0 (yes) | sdcpp | unavailable (≥12 GiB VRAM) |
| `flux.dev` | image | text2image | experimental | FLUX-dev Non-Commercial (**no**) | sdcpp | restricted |
| `qwen.image` | image | text2image | experimental | Apache 2.0 (yes) | comfyui *(planned)* | unavailable (needs ≥12 GiB + new runtime) |
| `ltx.2.5` | video | text2video, image2video, audio2video | **planned** | LTX-2.x Community (yes, <$10M rev) | comfyui *(planned)* | — |
| `wan.2.2.ti2v` | video | text2video, image2video | **planned** | Apache 2.0 (yes) | comfyui *(planned)* | — |
| `hunyuan.video` | video | text2video | **planned** | Tencent Community (yes, regional limits) | comfyui *(planned)* | — |

`ltx.2.5` was verified against the official Lightricks release (Aug 11, 2026;
open weights on `Lightricks/LTX-2.5`, day-0 ComfyUI support, native multi-shot,
synchronized audio, up to 4K50). It is the strongest candidate for the first
video milestone because multi-shot + audio map directly onto Atomic's
storyboard model. `wan.2.2.ti2v` (Apache 2.0, 5B) is the pragmatic fallback if
LTX memory requirements prove too heavy on consumer cards.

Video entries carry metadata only — **no files, no runtime, no inference
code**. The ComfyUI runtime is registered as `planned`; the adapter registry
can add `gen.video.*` adapters in a later milestone without schema changes.

---

## Candidates for the first real model — pick one

All three run through the already-implemented approval-gated download path.
Recommendation follows the table.

| | **A · SD 1.5 fp16** | **B · SDXL Base 1.0** | **C · Qwen-Image** |
|---|---|---|---|
| **Source** | HF `sd-legacy/stable-diffusion-v1-5` | HF `stabilityai/stable-diffusion-xl-base-1.0` | HF `Qwen/Qwen-Image` |
| **License** | CreativeML OpenRAIL-M — commercial OK | OpenRAIL++-M — commercial OK | Apache 2.0 — commercial OK |
| **Download** | ~2.0 GiB SafeTensors | ~6.5 GiB SafeTensors | ~40 GiB (bf16) / ~12–20 GiB GGUF |
| **Runtime** | sd.cpp (ready) | sd.cpp (ready) | **ComfyUI — not built yet** |
| **Min VRAM** | ~4 GiB → **compatible** | ~8 GiB → **limited** (little headroom) | ~12 GiB quant → **unavailable** on 3060 Ti |
| **Speed (est.)** | seconds per 512px image | slower, 1024px native, may need offload | minutes-class, likely OOM at full quality |
| **Quality** | Baseline — good for storyboard drafts | Noticeably better detail | Best prompt adherence + real embedded text (EN/ZH) |
| **M3 risk** | Lowest — proven path end-to-end | Medium — fits, tight | High — new runtime + won't fit this GPU well |

**Recommendation: A (`sd15.fp16`) now, B (`sdxl.base`) immediately after as the
quality upgrade on bigger hardware, C (`qwen.image`) revisited once the planned
ComfyUI runtime lands.** Option A proves the whole production path on this
machine's measured 5.9 GiB free VRAM with headroom to spare; B stays installable
today for users with ≥12 GiB cards; C is honest about needing both a new
runtime and more VRAM.

### Chosen-first details (if A is approved)

| Field | Value |
|---|---|
| **File** | `v1-5-pruned-emaonly.safetensors` (SafeTensors — no pickle/code execution) |
| **URL** | `https://huggingface.co/sd-legacy/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors` |
| **Integrity** | no upstream sha256 → exact declared size + SafeTensors magic; resume via HTTP Range |
| **Est. VRAM** | ~3.6 GiB @512×512 batch 1 (estimate — measured in Phase 2) |
| **Runtime** | sd.cpp `vulkan` flavor (~80 MiB, MIT, GitHub release asset) |

## What landed before any download (Phase 1, this PR)

- Extensible capability-aware catalog (above) + compatibility engine +
  hardware profiles (E.1.1–E.1.5)
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
| Single-GPU e2e pipeline | **Simulated** — fake worker proves the path; real sd.exe + weights pending approval |
| Multi-GPU parallel scheduling | **Simulated** — scheduler tests use fabricated probes; no multi-GPU hardware available |
| VRAM figures | **Estimates** — catalog anchors + activation scaling; labeled in UI |
| Video model metadata | **Verified sources** (licenses/repos checked) but **zero inference validation** |

## Approval requested for Phase 2

1. **Pick a first model** — A (`sd15.fp16`, recommended), B (`sdxl.base`), or
   C (`qwen.image`, deferred-runtime).
2. On approval: download the chosen weights + sd.cpp Vulkan runtime via the
   Model Manager (user-initiated, checksummed, resumable).
3. Run real inference on the RTX 3060 Ti; record actual VRAM/duration and feed
   measurements back into the catalog.

No paid services. No video-model downloads. No Milestone 4 work.
