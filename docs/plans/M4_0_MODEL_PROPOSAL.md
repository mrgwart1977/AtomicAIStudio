# M4.0 Video Model Proposal — Wan 2.1 T2V 1.3B (fp16)

**Approval gate document.** No weights or additional runtimes are downloaded
until this proposal is explicitly approved. Phase-1 architecture, adapter,
command, recovery-contract, UI, and test work is already complete and green
(144/144 backend tests); this document proposes the exact artifacts to make
the pipeline live.

## Recommendation

**Wan 2.1 T2V 1.3B (fp16)** — the only credible local video model for an
8 GB consumer GPU today:

- Apache-2.0 across all three component repositories (commercial use OK).
- ~6.0 GiB total download — one order of magnitude below Wan 14B / LTX-2.x.
- sd.cpp `vid_gen` mode has documented Wan 2.1 support (T2V, I2V, FLF2V,
  VACE, TI2V variants) in the SAME pinned release we already ship for image
  (`master-1073-991f825`) — **no new runtime is required**.
- Text-to-video only at this size. I2V needs the Wan 2.1 **14B** I2V model
  (~28 GiB + encoders) — not viable on 8 GB and not proposed here.

## Proposed artifacts (exact)

Runtime: **stable-diffusion.cpp `master-1073-991f825`** — already installed
(Vulkan + CUDA flavors, verified in M3). No additional runtime download.

| # | Role | File | Repo | Bytes | SHA-256 |
|---|------|------|------|-------|---------|
| 1 | diffusion | `wan2.1_t2v_1.3B_fp16.safetensors` | `Comfy-Org/Wan_2.1_ComfyUI_repackaged` | **2,838,303,560** | `be531024cd9018cb5b48c40cfbb6a6191645b1c792eb8bf4f8c1c6e10f924dc5` |
| 2 | vae | `wan_2.1_vae.safetensors` | same | **253,815,318** | `2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b` |
| 3 | text_encoder | `umt5-xxl-encoder-Q4_K_M.gguf` | `city96/umt5-xxl-encoder-gguf` | **3,655,145,312** | `17cf97a5bbbc60a646d6105b832b6f657ce904a8a1ad970e4b59df0c67584a40` |

**Total: 6,747,264,190 B (~6.28 GiB)** — all three entries are already in the
catalog (`wan21.t2v.1_3b`, support=`experimental`, per-file sha256 enforced at
install; the multi-component download path verifies each file independently
and only marks the model installed when ALL components verify).

Licenses (verified against repo metadata, all Apache-2.0):
- `Wan-AI/Wan2.1-T2V-1.3B` — Apache-2.0
- `Comfy-Org/Wan_2.1_ComfyUI_repackaged` — Apache-2.0
- `city96/umt5-xxl-encoder-gguf` — Apache-2.0 (GGUF conversion of UMT5-XXL)

Alternatives considered and rejected:
- **bf16 variant** (same size, `6f999b0d…`) — no benefit over fp16 here.
- **UMT5 Q3_K_M / Q3_K_S** (~2.7–2.9 GiB) — cheaper, but Q4_K_M is the
  lowest quantization I'd trust for a user-facing default; can be added as
  a variant later.
- **UMT5 Q8_0 / F16** (5.6/10.6 GiB) — no headroom on 8 GB.
- **LTX-2.3 / LTX-2.5** — 22B model + 12B text encoder ≈ 40+ GiB of
  weights; far beyond this GPU and this machine's realistic disk budget.
  Stays `planned`.
- **Wan 2.2 TI2V** — newer combined T2V/I2V model; larger, and its 5B
  variant still needs high-moment VAE paths we haven't verified. Stays
  `planned`.

## Hardware fit (RTX 3060 Ti, 8 GB)

| Component | VRAM impact |
|---|---|
| Diffusion 1.3B fp16 | ~2.6 GiB resident |
| UMT5 Q4_K_M encoder | ~3.4 GiB during encode (offloadable after prompt encode) |
| Wan VAE | ~0.24 GiB — **CPU on Vulkan** (see caveat) |
| Activations @ 480×480×33f | ~1–2 GiB |

Expected to fit **with caveats**, honestly flagged:
- sd.cpp's Wan docs say only **CUDA and CPU** run the Wan VAE reliably —
  other backends need `--vae-on-cpu` (slow decode, still correct). The
  adapter adds `--vae-on-cpu` automatically on non-CUDA flavors, so Vulkan
  remains usable; CUDA runs the VAE on-GPU.
- 8 GB is tight. If real-world runs OOM, the honest outcome is a clear
  error — never a silent quality degrade or undisclosed CPU run.
- CPU-only generation is technically possible and deliberately **not**
  offered: Wan-class CPU inference takes hours-to-days per clip.

## Capabilities delivered on approval

- **Text-to-video**: 480×480 (Wan 1.3B's trained res) at **33/49/81/129
  frames, 24 fps** (~1.4–5.4 s clips). Frame counts follow the model's
  4k+1 temporal stride.
- **Image-to-video**: plumbed end-to-end (adapter arg, CAS path
  resolution, UI source-image picker, validation) — but requires an I2V
  model this proposal does NOT include. The UI only offers i2v when a
  model with `image2video` mode is installed.
- Duration/resolution limits are catalog-declared per model, not
  hardcoded.

## Validation & provenance

- ffprobe validation at the commit boundary: unreadable output never
  registers an asset (degrades to a container-signature check when
  ffmpeg is absent — **ffmpeg is NOT currently installed on this machine**;
  a Phase-2 proposal may add a verified binary).
- Provenance records model/version/runtime/flavor/params/seed/frames/fps/
  probe results in `media_json`.
- Jobs are non-resumable, `execution_scope=local`, `attempt_no` tracked,
  interrupted jobs fail honestly — the M4.1 contracts Phase 1 established.

## Download authorization requested

Downloading the three files above (~6.28 GiB total, Hugging Face, no
account/gate required, zero cost) to:

1. Run the real end-to-end T2V pipeline (Vulkan + `--vae-on-cpu`, and
   CUDA on-GPU VAE).
2. Measure actual VRAM/time vs estimates; mark the entry `tested` only on
   success.
3. Produce real generated clips as evidence.

**Awaiting explicit approval before any download.**
