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
    capabilities: dict[str, Any]  # tasks, sizes, seed, steps, cfg, samplers, batch, ref_images, lora, precision
    runtime_key: str = "sdcpp"
    status_hint: str = "stable"  # stable | preview | restricted
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
    # asset chosen by name pattern per backend flavor.
    release_api: str
    asset_patterns: dict[str, str]  # flavor -> filename substring, e.g. "vulkan" -> "vulkan-x64"
    exe_name: str
    backends: tuple[str, ...]
    est_bytes: int
    notes: str = ""


RUNTIMES: dict[str, RuntimeSpec] = {
    "sdcpp": RuntimeSpec(
        key="sdcpp",
        display_name="stable-diffusion.cpp",
        version="latest",
        license={
            "name": "MIT",
            "url": "https://github.com/leejet/stable-diffusion.cpp/blob/master/LICENSE.txt",
            "commercial_ok": True,
            "notes": "Open-source inference engine (ggml). Vendor-agnostic via Vulkan; CUDA and CPU builds exist.",
        },
        release_api="https://api.github.com/repos/leejet/stable-diffusion.cpp/releases/latest",
        asset_patterns={
            "vulkan": "vulkan",  # e.g. sd-bin-win-vulkan-x64.zip
            "cuda": "cuda",
            "cpu": "cpu",
        },
        exe_name="sd.exe",
        backends=("vulkan", "cuda", "cpu"),
        est_bytes=80 * MIB,
        notes="Subprocess worker — full isolation per generation job; kill = clean cancel.",
    ),
}


_HF_SD15 = "https://huggingface.co/sd-legacy/stable-diffusion-v1-5/resolve/main"

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
    "sd15.fp16": ModelSpec(
        key="sd15.fp16",
        display_name="Stable Diffusion 1.5 (fp16)",
        family="sd1.5",
        version="1.5",
        adapter_id="gen.image.sdcpp",
        fmt="safetensors",
        source={
            "provider": "huggingface",
            "repo": "sd-legacy/stable-diffusion-v1-5",
            "url": f"{_HF_SD15}/v1-5-pruned-emaonly.safetensors",
        },
        license={
            "name": "CreativeML OpenRAIL-M",
            "url": "https://huggingface.co/sd-legacy/stable-diffusion-v1-5/blob/main/LICENSE.md",
            "commercial_ok": True,
            "notes": "Commercial use allowed with use restrictions (no illegal/harmful content).",
        },
        files=(ModelFile(
            name="v1-5-pruned-emaonly.safetensors",
            size_bytes=2_138_000_000,  # ~2.0 GiB; size checked at download
            url=f"{_HF_SD15}/v1-5-pruned-emaonly.safetensors",
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
            "url": "https://huggingface.co/second-state/stable-diffusion-v1-5-GGUF/resolve/main/stable-diffusion-v1-5-Q8_0.gguf",
        },
        license={
            "name": "CreativeML OpenRAIL-M",
            "url": "https://huggingface.co/sd-legacy/stable-diffusion-v1-5/blob/main/LICENSE.md",
            "commercial_ok": True,
            "notes": "Community quantization of the same OpenRAIL-M weights.",
        },
        files=(ModelFile(
            name="stable-diffusion-v1-5-Q8_0.gguf",
            size_bytes=2_120_000_000,
            url="https://huggingface.co/second-state/stable-diffusion-v1-5-GGUF/resolve/main/stable-diffusion-v1-5-Q8_0.gguf",
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
        vram_weight_bytes=int(2.3 * GIB),
        notes="Smaller VRAM footprint, slightly reduced fidelity. Good first install on <8 GB GPUs.",
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
        status_hint="restricted",
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
        status_hint="preview",
        notes="High-end tier — listed so the manager shows an honest 'unsupported on this GPU' story.",
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
        "status_hint": spec.status_hint,
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
