"""U-4a — runtime capability declarations.

Versioned, declarative claims about which model families a runtime can
serve — the U-4b verdict engine's static model↔runtime axis input. Every
entry is evidence-derived from the catalog and adapter contracts:

  grade="tested"     — a catalog model exists on this runtime_key with
                       support in (production, tested): verified in-app.
  grade="documented" — the runtime documents the path upstream (or the
                       catalog entry exists at support="experimental")
                       but the app has never validated it. Documented
                       support can never produce a bare "compatible"
                       verdict — it caps at limited with
                       runtime_path_unverified.
  absent             — not listed here at all = unverified, evaluated as
                       unknown, never as optimistic support.

Audited claims for the initial "sdcpp" declaration:

  sd1.5  tested — sd15.fp32 (safetensors, production),
                  sd15.fp16 + sd15.q8 (GGUF, production); text2image.
  sdxl   tested — sdxl.base (production) + sdxl.turbo (tested),
                  safetensors only; text2image. No verified GGUF path is
                  claimed.
  wan    tested — wan21.t2v.1_3b (tested, end-to-end validated on Vulkan
                  @ master-1073-991f825); text2video; multi-component
                  (diffusion + vae + text_encoder); safetensors.
  flux   documented — flux.schnell + flux.dev are catalog
                  support="experimental"; upstream sd.cpp supports FLUX
                  GGUF but the app has never validated it.
  adapter:sd1.5 — LoRA adapters for the sd1.5 base: documented +
                  POLICY-GATED (G1/B-2/B-3e-2 holds are active; the
                  adapter contract declares lora support but no LoRA
                  generation path is unlocked by this declaration).

"comfyui" is a PLANNED stub: its family list exists so verdicts can say
honestly "recognized, but the runtime is not installable yet" —
planned=True means runtime_axis=planned can never be operational and the
declaration implies zero verified compatibility.

Diffusers, dedicated LTX/Wan runtimes, llama.cpp-class runtimes, and all
cloud adapters are intentionally absent — no declarations, no claims.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.model_families import FAMILIES_VERSION

# Bump on ANY declaration OR evaluation-semantics change — feeds the
# U-4b verdict fingerprint so a capability/evaluation change correctly
# invalidates stored verdicts. caps2: CD-9-DUP — measured vram_free=0 is
# real evidence (was treated as absent → 0.85·total fallback).
CAPS_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class FamilyCap:
    """What a runtime claims for one model family. Unlisted combinations
    of fmt/mode/dtype are unverified — they must never resolve to
    supported."""

    family_id: str
    grade: str                               # tested | documented
    fmts_tested: tuple[str, ...] = ()
    fmts_documented: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    dtypes_tested: tuple[str, ...] = ()
    components_required: tuple[str, ...] = ()
    min_runtime_version: str | None = None   # documented floor
    policy_gated: bool = False
    notes: str = ""


@dataclass(frozen=True)
class RuntimeCapabilityDecl:
    """One runtime's static capability declaration."""

    runtime_key: str
    display_name: str
    caps_version: int
    adapter_ids: tuple[str, ...]
    planned: bool
    families: dict[str, FamilyCap]
    backends: tuple[str, ...]
    validated_versions: tuple[str, ...]      # exact release-tag pins —
    # non-semver tags can't be ordered; anything else is "unverified"
    notes: str = ""


def adapter_cap_key(kind: str, family_id: str) -> str:
    """The FamilyCap lookup key: adapters declare against their base
    family ("adapter:sd1.5"), models/auxiliaries against their family."""
    return f"adapter:{family_id}" if kind == "adapter" else family_id


RUNTIME_CAPS: dict[str, RuntimeCapabilityDecl] = {
    "sdcpp": RuntimeCapabilityDecl(
        runtime_key="sdcpp",
        display_name="stable-diffusion.cpp",
        caps_version=1,
        adapter_ids=("gen.image.sdcpp", "gen.video.sdcpp"),
        planned=False,
        backends=("vulkan", "cuda", "cpu"),
        validated_versions=("master-1073-991f825",),
        families={
            "sd1.5": FamilyCap(
                family_id="sd1.5", grade="tested",
                fmts_tested=("safetensors", "gguf"),
                modes=("text2image",),
                dtypes_tested=("F16", "F32", "Q8_0"),
            ),
            "sdxl": FamilyCap(
                family_id="sdxl", grade="tested",
                fmts_tested=("safetensors",),      # no verified GGUF path
                modes=("text2image",),
                dtypes_tested=("F16",),
            ),
            "wan": FamilyCap(
                family_id="wan", grade="tested",
                fmts_tested=("safetensors",),
                modes=("text2video",),
                dtypes_tested=("F16",),
                components_required=("diffusion", "vae", "text_encoder"),
                min_runtime_version="master-1073-991f825",
            ),
            "flux": FamilyCap(
                family_id="flux", grade="documented",
                fmts_documented=("gguf",),
                modes=("text2image",),
                notes="Catalog flux.schnell/flux.dev are experimental — "
                       "upstream sd.cpp claims FLUX GGUF; never "
                       "validated in-app.",
            ),
            "adapter:sd1.5": FamilyCap(
                family_id="adapter:sd1.5", grade="documented",
                fmts_documented=("safetensors",),
                modes=("text2image",),
                policy_gated=True,
                notes="LoRA attach is declared by the adapter contract "
                       "but G1/B-2/B-3e-2 holds keep it gated — policy "
                       "axis must emit hold_gated regardless of arch fit.",
            ),
        },
        notes="Only the pinned release tag is verified; other tags are "
              "release strings that cannot be ordered → "
              "runtime_version_unverified.",
    ),
    "comfyui": RuntimeCapabilityDecl(
        runtime_key="comfyui",
        display_name="ComfyUI (planned)",
        caps_version=1,
        adapter_ids=(),
        planned=True,
        backends=("cuda", "cpu", "directml"),
        validated_versions=(),
        families={
            "qwen": FamilyCap(
                family_id="qwen", grade="documented",
                fmts_documented=("safetensors",),
                modes=("text2image",),
            ),
            "ltx": FamilyCap(
                family_id="ltx", grade="documented",
                fmts_documented=("safetensors",),
                modes=("text2video", "image2video", "audio2video"),
            ),
            "wan": FamilyCap(
                family_id="wan", grade="documented",
                fmts_documented=("safetensors",),
                modes=("text2video", "image2video"),
                components_required=("diffusion", "vae", "text_encoder"),
            ),
            "hunyuan": FamilyCap(
                family_id="hunyuan", grade="documented",
                fmts_documented=("safetensors",),
                modes=("text2video",),
            ),
        },
        notes="Planned stub — families are listed so verdicts can say "
              "'recognized, but the runtime is not installable yet'. "
              "planned=True implies ZERO verified compatibility.",
    ),
}


def get_decl(runtime_key: str | None) -> RuntimeCapabilityDecl | None:
    if not runtime_key:
        return None
    return RUNTIME_CAPS.get(runtime_key)


def decl_catalog() -> list[RuntimeCapabilityDecl]:
    return list(RUNTIME_CAPS.values())


def claimed_families() -> set[str]:
    """Every family any declaration claims — 'no_declared_runtime' is the
    coverage state for recognized families outside this set."""
    out: set[str] = set()
    for decl in RUNTIME_CAPS.values():
        out.update(decl.families)
    return {k.split(":", 1)[-1] for k in out}


def ruleset_version() -> str:
    """Versioned identity of the classification + capability ruleset —
    stored on every verdict and included in its inputs fingerprint."""
    return f"u4:fam{FAMILIES_VERSION}.caps{CAPS_SCHEMA_VERSION}"
