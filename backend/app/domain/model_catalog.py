"""Model + runtime catalog.

The catalog is declarative metadata — what the app KNOWS about, not what
is installed. Entries carry source, license, sizes, requirements, and
capability declarations so the Model Manager can explain every model
before anything is downloaded. New adapters/providers register new
entries here; nothing in the engine is hard-coded to one model.

License truthfulness: `commercial_ok` reflects the license text at the
time of writing. Restricted licenses stay in the catalog (visible,
explained) but cannot be installed for production use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MIB = 1024 * 1024
GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class ModelFile:
    name: str
    size_bytes: int
    url: str  # primary download URL (may be mirrored via request override)
    sha256: str | None = None  # verified when the source publishes one
    # M4: multi-component models (video pipelines need a diffusion model +
    # VAE + text encoder + optional CLIP-vision). Role identifies the
    # component to the adapter's argv builder. "diffusion" is the primary
    # weight file; every spec needs exactly one.
    role: str = "diffusion"  # diffusion | vae | audio_vae | text_encoder | clip_vision | upscaler


@dataclass(frozen=True)
class ModelSpec:
    key: str
    display_name: str
    family: str
    version: str
    adapter_id: str  # inference adapter that serves this model
    fmt: str  # safetensors | gguf | onnx | ...
    source: dict[str, Any]
    license: dict[str, Any]  # name, url, commercial_ok, notes
    files: tuple[ModelFile, ...]
    requirements: dict[str, Any]  # min_vram_bytes, rec_vram_bytes, min_ram_bytes, min_disk_bytes, backends
    capabilities: dict[str, Any]  # tasks, sizes, seed, steps, cfg, samplers, batch, ref_images, lora, precision,
    #                              resolutions, durations_s, fps, audio, character_consistency
    runtime_key: str = "sdcpp"
    # Engineering support tier — catalog presence does NOT imply local support:
    #   production   — validated end-to-end in this app
    #   tested       — works through a verified runtime path, not fully validated
    #   experimental — real model, runtime path not integrated yet
    #   planned      — future milestone; visible in catalog, never installable
    support: str = "production"
    modality: str = "image"  # image | video | audio | multimodal
    modes: tuple[str, ...] = ("text2image",)  # text2image | image2image | text2video | image2video | audio2video | ...
    # VRAM estimate anchors (ESTIMATES, not measurements): baseline peak at
    # 512x512, split into weight-resident + activation parts.
    vram_baseline_bytes: int = 0
    vram_weight_bytes: int = 0
    notes: str = ""

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


@dataclass(frozen=True)
class RuntimeSpec:
    key: str
    display_name: str
    version: str
    license: dict[str, Any]
    # GitHub releases resolution — queried at install time (explicit action),
    # asset chosen by name pattern per backend flavor. release_api is the
    # *latest* endpoint; when release_tag is set the pinned tag endpoint is
    # used instead so installs are reproducible (see RELEASE_CHECKLIST).
    release_api: str
    exe_name: str
    backends: tuple[str, ...]
    est_bytes: int
    release_tag: str | None = None
    asset_patterns: dict[str, str] = field(default_factory=dict)  # flavor -> filename substring
    notes: str = ""


RUNTIMES: dict[str, RuntimeSpec] = {
    "sdcpp": RuntimeSpec(
        key="sdcpp",
        display_name="stable-diffusion.cpp",
        version="master-1073-991f825",
        license={
            "name": "MIT",
            "url": "https://github.com/leejet/stable-diffusion.cpp/blob/master/LICENSE.txt",
            "commercial_ok": True,
            "notes": "Open-source inference engine (ggml). Vendor-agnostic via Vulkan; CUDA and CPU builds exist.",
        },
        release_api="https://api.github.com/repos/leejet/stable-diffusion.cpp/releases/latest",
        # Pinned for reproducible installs — this is the release the Vulkan
        # and CUDA flavors were verified against on the RTX 3060 Ti.
        release_tag="master-1073-991f825",
        asset_patterns={
            # must include the platform tag — bare "vulkan"/"cuda" would also
            # match Linux zips and the optional "cudart-" CUDA-runtime bundle.
            "vulkan": "win-vulkan",  # sd-master-…-bin-win-vulkan-x64.zip (~30 MiB)
            "cuda": "win-cuda",      # sd-master-…-bin-win-cuda12-x64.zip (~367 MiB)
            "cpu": "win-cpu",        # sd-master-…-bin-win-cpu-x64.zip (~18 MiB)
        },
        exe_name="sd-cli.exe",  # renamed from sd.exe in current releases (README: ./bin/sd-cli)
        backends=("vulkan", "cuda", "cpu"),
        est_bytes=80 * MIB,
        notes="Subprocess worker — full isolation per generation job; kill = clean cancel.",
    ),
    # PLANNED — not integrated in M3. Qwen-Image, LTX-Video, Wan, and
    # HunyuanVideo are not sd.cpp architectures; their path is ComfyUI
    # (embedded Python + PyTorch) or a dedicated runtime in a later
    # milestone. Listed so catalog entries can name their runtime honestly.
    "comfyui": RuntimeSpec(
        key="comfyui",
        display_name="ComfyUI (planned)",
        version="planned",
        license={
            "name": "GPL-3.0",
            "url": "https://github.com/comfyanonymous/ComfyUI/blob/master/LICENSE",
            "commercial_ok": True,
            "notes": "Local node-graph inference server used for non-sd.cpp models (Qwen-Image, video models).",
        },
        release_api="https://api.github.com/repos/comfyanonymous/ComfyUI/releases/latest",
        asset_patterns={"cuda": "portable", "cpu": "portable"},
        exe_name="run_nvidia_gpu.bat",
        backends=("cuda", "cpu", "directml"),
        est_bytes=1600 * MIB,
        notes="PLANNED runtime — required by Qwen-Image and all video catalog entries; not installable in M3.",
    ),
}


# Canonical SD 1.5 repo (sd-legacy/stable-diffusion-v1-5 redirects here).
# The ONLY official weight file is the fp32 safetensors (~4.26 GB). fp16 and
# quantized artifacts are community conversions — Second State's GGUF repo is
# the standard source referenced by sd.cpp documentation.
_HF_SD15 = "https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5/resolve/main"
_HF_SD15_GGUF = "https://huggingface.co/second-state/stable-diffusion-v1-5-GGUF/resolve/main"

_SD15_CAPS = {
    "tasks": ["text2image"],
    "negative_prompt": True,
    "seed": True,
    "sizes": {"min": 256, "max": 1024, "step": 8,
              "presets": [
                  {"w": 512, "h": 512, "label": "1:1 square"},
                  {"w": 768, "h": 512, "label": "3:2 landscape"},
                  {"w": 512, "h": 768, "label": "2:3 portrait"},
                  {"w": 640, "h": 640, "label": "1:1 large"},
              ]},
    "steps": {"min": 1, "max": 150, "default": 25},
    "cfg": {"min": 1.0, "max": 30.0, "default": 7.0},
    "samplers": ["euler_a", "euler", "heun", "dpm2", "dpm++2m", "dpm++2s_a", "lcm"],
    "schedulers": ["default", "karras"],
    "batch": {"min": 1, "max": 4, "default": 1},
    "ref_images": False,   # txt2img only — no img2img/IP-Adapter in M3
    "lora": True,
    "precision": ["fp16", "q8_0"],
    "gpu_selection": True,
    "cpu_fallback": True,
}


MODELS: dict[str, ModelSpec] = {
    # fp16 weights as a GGUF — a Second State community conversion of the
    # official fp32 checkpoint (same OpenRAIL-M terms). Verified size + sha256.
    "sd15.fp16": ModelSpec(
        key="sd15.fp16",
        display_name="Stable Diffusion 1.5 (fp16, GGUF)",
        family="sd1.5",
        version="1.5-f16",
        adapter_id="gen.image.sdcpp",
        fmt="gguf",
        source={
            "provider": "huggingface",
            "repo": "second-state/stable-diffusion-v1-5-GGUF",
            "url": f"{_HF_SD15_GGUF}/stable-diffusion-v1-5-pruned-emaonly-f16.gguf",
        },
        license={
            "name": "CreativeML OpenRAIL-M",
            "url": "https://huggingface.co/spaces/CompVis/stable-diffusion-license",
            "commercial_ok": True,
            "notes": "Community fp16 conversion of the official OpenRAIL-M weights — "
                     "commercial use allowed with use restrictions (no illegal/harmful content).",
        },
        files=(ModelFile(
            name="stable-diffusion-v1-5-pruned-emaonly-f16.gguf",
            size_bytes=2_132_586_944,  # verified via HF tree API
            url=f"{_HF_SD15_GGUF}/stable-diffusion-v1-5-pruned-emaonly-f16.gguf",
            sha256="da017009aa86a3f46468857d95833435fe55e4a2856afab76ba7722ef8ee4d8b",
        ),),
        requirements={
            "min_vram_bytes": 4 * GIB,
            "rec_vram_bytes": 8 * GIB,
            "min_ram_bytes": 8 * GIB,
            "min_disk_bytes": 3 * GIB,
            "backends": ["vulkan", "cuda", "cpu"],
        },
        capabilities=_SD15_CAPS,
        vram_baseline_bytes=int(3.6 * GIB),
        vram_weight_bytes=int(2.2 * GIB),
        notes="The proven mid-range baseline: fast, small, well-supported, commercially usable.",
    ),
    "sd15.q8": ModelSpec(
        key="sd15.q8",
        display_name="Stable Diffusion 1.5 (Q8_0 quantized)",
        family="sd1.5",
        version="1.5-q8_0",
        adapter_id="gen.image.sdcpp",
        fmt="gguf",
        source={
            "provider": "huggingface",
            "repo": "second-state/stable-diffusion-v1-5-GGUF",
            "url": f"{_HF_SD15_GGUF}/stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf",
        },
        license={
            "name": "CreativeML OpenRAIL-M",
            "url": "https://huggingface.co/spaces/CompVis/stable-diffusion-license",
            "commercial_ok": True,
            "notes": "Community quantization of the same OpenRAIL-M weights.",
        },
        files=(ModelFile(
            name="stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf",
            size_bytes=1_763_578_176,  # verified via HF tree API
            url=f"{_HF_SD15_GGUF}/stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf",
            sha256="d0555243938c62faeefb4ac93f6c7a053ad373a4290c5256bce229aeb193bf94",
        ),),
        requirements={
            "min_vram_bytes": 3 * GIB,
            "rec_vram_bytes": 6 * GIB,
            "min_ram_bytes": 8 * GIB,
            "min_disk_bytes": 3 * GIB,
            "backends": ["vulkan", "cuda", "cpu"],
        },
        capabilities={**_SD15_CAPS, "precision": ["q8_0"]},
        vram_baseline_bytes=int(3.0 * GIB),
        vram_weight_bytes=int(2.0 * GIB),
        notes="Smallest footprint — the safest first install on <8 GB GPUs; slightly reduced fidelity.",
    ),
    # The ONLY official Stability artifact — fp32 safetensors. Larger, but the
    # most provenance-pure option; sd.cpp loads it directly.
    "sd15.fp32": ModelSpec(
        key="sd15.fp32",
        display_name="Stable Diffusion 1.5 (official fp32 safetensors)",
        family="sd1.5",
        version="1.5",
        adapter_id="gen.image.sdcpp",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "stable-diffusion-v1-5/stable-diffusion-v1-5",
            "url": f"{_HF_SD15}/v1-5-pruned-emaonly.safetensors",
        },
        license={
            "name": "CreativeML OpenRAIL-M",
            "url": "https://huggingface.co/spaces/CompVis/stable-diffusion-license",
            "commercial_ok": True,
            "notes": "The official Stability release — commercial use allowed with use restrictions.",
        },
        files=(ModelFile(
            name="v1-5-pruned-emaonly.safetensors",
            size_bytes=4_265_146_304,  # verified via HF tree API
            url=f"{_HF_SD15}/v1-5-pruned-emaonly.safetensors",
            sha256="6ce0161689b3853acaa03779ec93eafe75a02f4ced659bee03f50797806fa2fa",
        ),),
        requirements={
            "min_vram_bytes": 6 * GIB,
            "rec_vram_bytes": 8 * GIB,
            "min_ram_bytes": 8 * GIB,
            "min_disk_bytes": 5 * GIB,
            "backends": ["vulkan", "cuda", "cpu"],
        },
        capabilities={**_SD15_CAPS, "precision": ["fp32"]},
        vram_baseline_bytes=int(6.5 * GIB),
        vram_weight_bytes=int(4.4 * GIB),
        notes="The official fp32 checkpoint — larger and heavier than fp16; choose this for "
              "provenance purity, fp16 GGUF for everyday use.",
    ),
    "sdxl.base": ModelSpec(
        key="sdxl.base",
        display_name="SDXL Base 1.0",
        family="sdxl",
        version="1.0",
        adapter_id="gen.image.sdcpp",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "stabilityai/stable-diffusion-xl-base-1.0",
            "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors",
        },
        license={
            "name": "OpenRAIL++-M",
            "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/blob/main/LICENSE.md",
            "commercial_ok": True,
            "notes": "Commercial use allowed with use restrictions.",
        },
        files=(ModelFile(
            name="sd_xl_base_1.0.safetensors",
            size_bytes=6_940_000_000,
            url="https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors",
        ),),
        requirements={
            "min_vram_bytes": 8 * GIB,
            "rec_vram_bytes": 12 * GIB,
            "min_ram_bytes": 16 * GIB,
            "min_disk_bytes": 8 * GIB,
            "backends": ["vulkan", "cuda", "cpu"],
        },
        capabilities={**_SD15_CAPS,
                      "sizes": {"min": 512, "max": 1536, "step": 8,
                                "presets": [{"w": 1024, "h": 1024, "label": "1:1 square"},
                                            {"w": 1152, "h": 896, "label": "9:7 landscape"},
                                            {"w": 896, "h": 1152, "label": "7:9 portrait"}]},
                      "steps": {"min": 1, "max": 150, "default": 30}},
        vram_baseline_bytes=int(7.0 * GIB),  # at 1024x1024 native res
        vram_weight_bytes=int(5.4 * GIB),
        notes="Higher quality at 1024px. Tight on 8 GB cards — limited mode may need smaller sizes or offload.",
    ),
    "sdxl.turbo": ModelSpec(
        key="sdxl.turbo",
        display_name="SDXL Turbo",
        family="sdxl",
        version="turbo-1.0",
        adapter_id="gen.image.sdcpp",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "stabilityai/sdxl-turbo",
            "url": "https://huggingface.co/stabilityai/sdxl-turbo/resolve/main/sd_xl_turbo_1.0_fp16.safetensors",
        },
        license={
            "name": "Stability AI Non-Commercial Research",
            "url": "https://huggingface.co/stabilityai/sdxl-turbo/blob/main/LICENSE.md",
            "commercial_ok": False,
            "notes": "Research/non-commercial only — cannot be used for production output.",
        },
        files=(ModelFile(
            name="sd_xl_turbo_1.0_fp16.safetensors",
            size_bytes=6_940_000_000,
            url="https://huggingface.co/stabilityai/sdxl-turbo/resolve/main/sd_xl_turbo_1.0_fp16.safetensors",
        ),),
        requirements={
            "min_vram_bytes": 8 * GIB,
            "rec_vram_bytes": 12 * GIB,
            "min_ram_bytes": 16 * GIB,
            "min_disk_bytes": 8 * GIB,
            "backends": ["vulkan", "cuda", "cpu"],
        },
        capabilities={**_SD15_CAPS, "steps": {"min": 1, "max": 8, "default": 2}},
        vram_baseline_bytes=int(7.0 * GIB),
        vram_weight_bytes=int(5.4 * GIB),
        support="tested",
        notes="1-4 step generation, but licensed non-commercial — visible for awareness, not installable.",
    ),
    "flux.schnell": ModelSpec(
        key="flux.schnell",
        display_name="FLUX.1 [schnell]",
        family="flux",
        version="1-schnell",
        adapter_id="gen.image.sdcpp",
        fmt="gguf",
        source={
            "provider": "huggingface",
            "repo": "black-forest-labs/FLUX.1-schnell",
            "url": "https://huggingface.co/black-forest-labs/FLUX.1-schnell",
        },
        license={
            "name": "Apache 2.0",
            "url": "https://huggingface.co/black-forest-labs/FLUX.1-schnell/blob/main/LICENSE",
            "commercial_ok": True,
            "notes": "Permissive license; but the model needs far more VRAM than consumer cards.",
        },
        files=(),
        requirements={
            "min_vram_bytes": 12 * GIB,
            "rec_vram_bytes": 24 * GIB,
            "min_ram_bytes": 24 * GIB,
            "min_disk_bytes": 25 * GIB,
            "backends": ["vulkan", "cuda"],
        },
        capabilities={**_SD15_CAPS, "sizes": {"min": 512, "max": 2048, "step": 8, "presets": [
            {"w": 1024, "h": 1024, "label": "1:1 square"}]}},
        vram_baseline_bytes=int(14.0 * GIB),
        vram_weight_bytes=int(11.9 * GIB),
        support="experimental",
        notes="High-end tier — listed so the manager shows an honest 'unsupported on this GPU' story.",
    ),
    "flux.dev": ModelSpec(
        key="flux.dev",
        display_name="FLUX.1 [dev]",
        family="flux",
        version="1-dev",
        adapter_id="gen.image.sdcpp",
        fmt="gguf",
        source={
            "provider": "huggingface",
            "repo": "black-forest-labs/FLUX.1-dev",
            "url": "https://huggingface.co/black-forest-labs/FLUX.1-dev",
        },
        license={
            "name": "FLUX.1-dev Non-Commercial License",
            "url": "https://huggingface.co/black-forest-labs/FLUX.1-dev/blob/main/LICENSE.md",
            "commercial_ok": False,
            "notes": "Non-commercial research/personal use only — production output requires a paid BFL license.",
        },
        files=(),
        requirements={
            "min_vram_bytes": 12 * GIB,
            "rec_vram_bytes": 24 * GIB,
            "min_ram_bytes": 24 * GIB,
            "min_disk_bytes": 25 * GIB,
            "backends": ["vulkan", "cuda"],
        },
        capabilities={**_SD15_CAPS, "sizes": {"min": 512, "max": 2048, "step": 8, "presets": [
            {"w": 1024, "h": 1024, "label": "1:1 square"}]}},
        vram_baseline_bytes=int(14.0 * GIB),
        vram_weight_bytes=int(11.9 * GIB),
        support="experimental",
        notes="Higher quality than schnell but non-commercial — visible for awareness, not installable.",
    ),
    "qwen.image": ModelSpec(
        key="qwen.image",
        display_name="Qwen-Image",
        family="qwen",
        version="1.0",
        adapter_id="gen.image.comfyui",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "Qwen/Qwen-Image",
            "url": "https://huggingface.co/Qwen/Qwen-Image",
        },
        license={
            "name": "Apache 2.0",
            "url": "https://huggingface.co/Qwen/Qwen-Image/blob/main/LICENSE",
            "commercial_ok": True,
            "notes": "Permissive — commercial use, redistribution, and modification allowed with attribution.",
        },
        files=(),  # no verified artifact on a supported runtime yet
        requirements={
            "min_vram_bytes": 12 * GIB,  # quantized; bf16 needs ~24+
            "rec_vram_bytes": 24 * GIB,
            "min_ram_bytes": 32 * GIB,
            "min_disk_bytes": 45 * GIB,
            "backends": ["cuda", "cpu"],
        },
        capabilities={
            "tasks": ["text2image"],
            "negative_prompt": True,
            "seed": True,
            "sizes": {"min": 512, "max": 2048, "step": 8,
                      "presets": [{"w": 1328, "h": 1328, "label": "1:1 square"},
                                  {"w": 1664, "h": 928, "label": "16:9 landscape"},
                                  {"w": 928, "h": 1664, "label": "9:16 portrait"}]},
            "steps": {"min": 1, "max": 100, "default": 40},
            "cfg": {"min": 1.0, "max": 20.0, "default": 4.0},
            "samplers": ["flowmatch"],
            "batch": {"min": 1, "max": 1, "default": 1},
            "ref_images": False,  # base model is t2i; Qwen-Image-Edit is a separate entry when supported
            "lora": True,
            "character_consistency": False,
            "audio": False,
            "precision": ["bf16", "q8_0", "q4_0"],
            "strengths": ["best-in-class prompt adherence", "legible embedded text (EN+ZH)"],
            "cpu_fallback": True,
        },
        vram_baseline_bytes=int(18.0 * GIB),
        vram_weight_bytes=int(12.0 * GIB),
        support="experimental",
        runtime_key="comfyui",
        modality="image",
        modes=("text2image",),
        notes="~20B MMDiT with exceptional text rendering. NOT an sd.cpp architecture — needs the "
              "planned ComfyUI path; catalog-visible for the Phase-2 candidate discussion.",
    ),

    # ------------------------------------------------- video (PLANNED) —
    # Catalog-visible future candidates. files=() and support="planned"
    # mean they can never be installed or selected in M3 — they exist so
    # the schema, UI, and roadmap carry real per-model metadata now.
    "wan21.t2v.1_3b": ModelSpec(
        key="wan21.t2v.1_3b",
        display_name="Wan 2.1 T2V 1.3B (fp16)",
        family="wan",
        version="2.1-t2v-1.3b-fp16",
        adapter_id="gen.video.sdcpp",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "Comfy-Org/Wan_2.1_ComfyUI_repackaged + city96/umt5-xxl-encoder-gguf",
            "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged",
        },
        license={
            "name": "Apache 2.0",
            "url": "https://huggingface.co/Wan-AI/Wan2.1-T2V-1.3B/blob/main/LICENSE.txt",
            "commercial_ok": True,
            "notes": "Permissive Apache-2.0 across all three components (verified on Wan-AI source, "
                     "Comfy-Org repackaged, and city96 GGUF).",
        },
        # Verified via HF API 2026-11-09 — three real components, exact
        # sizes + sha256 recorded.
        files=(
            ModelFile(
                name="wan2.1_t2v_1.3B_fp16.safetensors",
                size_bytes=2838303560,
                url="https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors",
                sha256="be531024cd9018cb5b48c40cfbb6a6191645b1c792eb8bf4f8c1c6e10f924dc5",
                role="diffusion",
            ),
            ModelFile(
                name="wan_2.1_vae.safetensors",
                size_bytes=253815318,
                url="https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors",
                sha256="2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b",
                role="vae",
            ),
            ModelFile(
                name="umt5-xxl-encoder-Q4_K_M.gguf",
                size_bytes=3655145312,
                url="https://huggingface.co/city96/umt5-xxl-encoder-gguf/resolve/main/umt5-xxl-encoder-Q4_K_M.gguf",
                sha256="17cf97a5bbbc60a646d6105b832b6f657ce904a8a1ad970e4b59df0c67584a40",
                role="text_encoder",
            ),
        ),
        requirements={
            # ~6.3 GiB disk for all three components. VRAM: with
            # --offload-to-cpu + --vae-on-cpu (mandatory on Vulkan — sd.cpp
            # only supports WAN VAE on CUDA/CPU) an 8 GiB card is the floor.
            "min_vram_bytes": 8 * GIB,
            "rec_vram_bytes": 12 * GIB,
            "min_ram_bytes": 16 * GIB,   # CPU-offloaded components live in RAM
            "min_disk_bytes": 8 * GIB,
            "backends": ["vulkan", "cuda", "cpu"],
        },
        capabilities={
            "tasks": ["text2video"],
            "negative_prompt": True,
            "seed": True,
            "resolutions": ["832x480"],
            "aspect_ratios": ["16:9"],
            "frame_counts": [17, 33, 49, 81],  # 4k+1 temporal constraint
            "durations_s": {"min": 1, "max": 5},
            "fps": [16, 24],
            "audio": False,
            "ref_images": False,  # I2V needs the separate 14B model
            "lora": False,
            "precision": ["fp16"],
            "cpu_fallback": True,
            "vae_on_cpu": True,   # required on Vulkan; slow but works
            "flow_shift": 3.0,
        },
        support="experimental",  # real artifacts verified; not yet run end-to-end here
        runtime_key="sdcpp",
        modality="video",
        modes=("text2video",),
        vram_baseline_bytes=int(5.5 * GIB),   # estimate: diffusion + UMT5 + act (CPU offload)
        vram_weight_bytes=int(3.0 * GIB),
        notes="Only credible T2V fit for an 8 GiB card on sd.cpp vid_gen: ~2-second 832x480 clips, "
              "33 frames @ 16fps typical. I2V needs the 14B variant (not viable here).",
    ),
    "ltx.2.5": ModelSpec(
        key="ltx.2.5",
        display_name="LTX-2.5 (dev)",
        family="ltx",
        version="2.5",
        adapter_id="gen.video.comfyui",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "Lightricks/LTX-2.5",
            "url": "https://huggingface.co/Lightricks/LTX-2.5",
        },
        license={
            "name": "LTX-2.x Community License",
            "url": "https://huggingface.co/Lightricks/LTX-2.5/blob/main/LICENSE",
            "commercial_ok": True,
            "notes": "Free commercial/production use under $10M annual revenue; fine-tune transfer may "
                     "require a paid license. Verified official release (Aug 11, 2026).",
        },
        files=(),
        requirements={
            # 22B transformer + 12B Gemma text encoder + two VAEs — the Q4
            # pipeline alone is ~15+ GiB of weights; realistic floor is a
            # 16-24 GiB GPU or heavy CPU offload with 64 GiB RAM.
            "min_vram_bytes": 16 * GIB,
            "rec_vram_bytes": 24 * GIB,
            "min_ram_bytes": 64 * GIB,
            "min_disk_bytes": 45 * GIB,
            "backends": ["cuda", "cpu"],
        },
        capabilities={
            "tasks": ["text2video", "image2video", "audio2video"],
            "negative_prompt": True,
            "seed": True,
            "resolutions": ["720p", "1080p", "1440p", "2160p"],
            "aspect_ratios": ["16:9", "9:16", "1:1", "3:2", "2:3"],
            "durations_s": {"min": 2, "max": 20},
            "fps": [24, 25, 48, 50],
            "audio": True,            # synchronized audio+video in one pass
            "multi_shot": True,       # native multi-shot consistency across cuts
            "ref_images": True,       # image-to-video / last-frame conditioning
            "lora": False,
            "character_consistency": True,  # multi-shot holds character identity
            "precision": ["bf16", "fp8"],
            "cpu_fallback": True,
        },
        support="planned",
        runtime_key="comfyui",
        modality="video",
        modes=("text2video", "image2video", "audio2video"),
        notes="Officially verified open-weights release — the strongest candidate for Atomic's first "
              "video milestone: native multi-shot + synchronized audio map directly to the storyboard model.",
    ),
    "wan.2.2.ti2v": ModelSpec(
        key="wan.2.2.ti2v",
        display_name="Wan 2.2 TI2V-5B",
        family="wan",
        version="2.2-ti2v-5b",
        adapter_id="gen.video.comfyui",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "Wan-AI/Wan2.2-TI2V-5B",
            "url": "https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B",
        },
        license={
            "name": "Apache 2.0",
            "url": "https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B/blob/main/LICENSE.txt",
            "commercial_ok": True,
            "notes": "Permissive; the 5B unified variant is the consumer-GPU-sized member of the family.",
        },
        files=(),
        requirements={
            "min_vram_bytes": 8 * GIB,   # fp8/gguf-quantized path; fp16 needs ~16+
            "rec_vram_bytes": 16 * GIB,
            "min_ram_bytes": 24 * GIB,
            "min_disk_bytes": 20 * GIB,
            "backends": ["cuda", "cpu"],
        },
        capabilities={
            "tasks": ["text2video", "image2video"],
            "negative_prompt": True,
            "seed": True,
            "resolutions": ["720p"],
            "aspect_ratios": ["16:9"],
            "durations_s": {"min": 2, "max": 5},
            "fps": [24],
            "audio": False,
            "multi_shot": False,
            "ref_images": True,
            "lora": True,
            "character_consistency": False,
            "precision": ["fp16", "fp8", "q8_0"],
            "cpu_fallback": True,
        },
        support="planned",
        runtime_key="comfyui",
        modality="video",
        modes=("text2video", "image2video"),
        notes="Smallest credible open video model — the pragmatic first-video fallback if LTX memory "
              "requirements prove too heavy on consumer cards.",
    ),
    "hunyuan.video": ModelSpec(
        key="hunyuan.video",
        display_name="HunyuanVideo",
        family="hunyuan",
        version="1.0",
        adapter_id="gen.video.comfyui",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "tencent/HunyuanVideo",
            "url": "https://huggingface.co/tencent/HunyuanVideo",
        },
        license={
            "name": "Tencent Hunyuan Community License",
            "url": "https://huggingface.co/tencent/HunyuanVideo/blob/main/LICENSE.txt",
            "commercial_ok": True,
            "notes": "Commercial use allowed with restrictions (excludes EU/UK/Korea territories; "
                     "product-branding obligations above 100M MAU). Review before any production use.",
        },
        files=(),
        requirements={
            "min_vram_bytes": 24 * GIB,  # 13B — quantized; full-quality needs ~45+
            "rec_vram_bytes": 48 * GIB,
            "min_ram_bytes": 48 * GIB,
            "min_disk_bytes": 60 * GIB,
            "backends": ["cuda"],
        },
        capabilities={
            "tasks": ["text2video"],
            "negative_prompt": True,
            "seed": True,
            "resolutions": ["720p"],
            "aspect_ratios": ["16:9", "9:16"],
            "durations_s": {"min": 2, "max": 5},
            "fps": [24],
            "audio": False,
            "multi_shot": False,
            "ref_images": False,  # i2v ships as a separate checkpoint
            "lora": True,
            "character_consistency": False,
            "precision": ["bf16", "fp8"],
            "cpu_fallback": False,
        },
        support="planned",
        runtime_key="comfyui",
        modality="video",
        modes=("text2video",),
        notes="Highest-quality open t2v baseline, but 13B-class memory needs put it beyond consumer "
              "single-GPU cards — tracked for high-end/workstation tiers.",
    ),
}


def catalog() -> list[ModelSpec]:
    return list(MODELS.values())


def get(key: str) -> ModelSpec | None:
    return MODELS.get(key)


def get_runtime(key: str) -> RuntimeSpec | None:
    return RUNTIMES.get(key)


def spec_to_dict(spec: ModelSpec) -> dict:
    return {
        "key": spec.key,
        "display_name": spec.display_name,
        "family": spec.family,
        "version": spec.version,
        "adapter_id": spec.adapter_id,
        "format": spec.fmt,
        "source": spec.source,
        "license": spec.license,
        "files": [{"name": f.name, "size_bytes": f.size_bytes, "sha256": f.sha256} for f in spec.files],
        "download_bytes": spec.total_bytes,
        "requirements": spec.requirements,
        "capabilities": spec.capabilities,
        "runtime_key": spec.runtime_key,
        "support": spec.support,
        "modality": spec.modality,
        "modes": list(spec.modes),
        "notes": spec.notes,
    }


def runtime_to_dict(spec: RuntimeSpec) -> dict:
    return {
        "key": spec.key,
        "display_name": spec.display_name,
        "version": spec.version,
        "license": spec.license,
        "backends": list(spec.backends),
        "est_bytes": spec.est_bytes,
        "notes": spec.notes,
    }
