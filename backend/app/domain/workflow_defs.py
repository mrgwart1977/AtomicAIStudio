"""U-5a — declarative workflow capability contracts.

A `WorkflowDef` describes what a creative workflow REQUIRES of the model
system — serving modes, model components, evidence, and policy notes —
so the runtime resolver can evaluate whether a registered artifact could
statically satisfy it. This registry is code-declared and versioned like
`model_families.py`; it defines requirements, never execution.

`status` is a static, audited declaration — never derived at runtime:

  implemented — a registered adapter on main serves the task today
  partial     — the path exists only through a gated/held/limited route
  missing     — no task or adapter exists on main
  held        — implementation exists but an explicit policy hold blocks it

A workflow being "implemented" says NOTHING about any model's
compatibility — recognition is not operation, and a candidate verdict
never grants execution authorization.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bump on ANY definition change — feeds future plan fingerprints.
WORKFLOW_DEFS_VERSION = 1


@dataclass(frozen=True)
class WorkflowDef:
    """Requirements of one creative workflow over the model system."""

    workflow_key: str
    display_name: str
    required_modes: tuple[str, ...]         # adapter task/mode strings
    required_components: tuple[str, ...] = ()
    optional_components: tuple[str, ...] = ()
    min_level: int = 3          # minimum verdict domain evidence expected
    policy: str = "none"        # none | lora_hold | cloud_gated | scheduled
    status: str = "missing"     # implemented | partial | missing | held
    notes: str = ""
    extra: dict = field(default_factory=dict)


WORKFLOW_DEFS: dict[str, WorkflowDef] = {
    d.workflow_key: d
    for d in [
        WorkflowDef(
            "text2image", "Text to image",
            required_modes=("text2image",),
            required_components=("diffusion",),
            status="implemented",
            notes="gen.image.sdcpp serves sd1.5/sdxl on main.",
        ),
        WorkflowDef(
            "image2image", "Image to image",
            required_modes=("image2image",),
            required_components=("diffusion",),
            policy="cloud_gated",
            status="partial",
            notes="Cloud img2img/edit only, gated by EndpointSpec "
                  "(C3-D2/D3); no local task exists.",
        ),
        WorkflowDef(
            "text2video", "Text to video",
            required_modes=("text2video",),
            required_components=("diffusion", "vae", "text_encoder"),
            status="implemented",
            notes="gen.video.sdcpp serves wan21.t2v @ the pinned tag.",
        ),
        WorkflowDef(
            "image2video", "Image to video",
            required_modes=("image2video",),
            required_components=("diffusion", "vae", "text_encoder"),
            status="implemented",
            notes="Same adapter task; documented for wan only.",
        ),
        WorkflowDef(
            "video2video", "Video to video",
            required_modes=("video2video",),
            required_components=("diffusion", "vae", "text_encoder"),
            status="missing",
            notes="No task or adapter on main.",
        ),
        WorkflowDef(
            "character_consistency", "Character consistency",
            required_modes=("text2image",),
            required_components=("diffusion",),
            optional_components=("lora", "image_encoder"),
            status="partial",
            notes="Cast/bible entities + lora_resolve bindings exist; "
                  "no generation path.",
        ),
        WorkflowDef(
            "multi_character_scene", "Multi-character scene",
            required_modes=("text2image",),
            required_components=("diffusion",),
            optional_components=("lora",),
            status="partial",
            notes="cast_state multi-entity scaffolding; no compose path.",
        ),
        WorkflowDef(
            "lora_character", "LoRA character workflow",
            required_modes=("text2image",),
            required_components=("diffusion", "base_model"),
            optional_components=("lora",),
            policy="lora_hold",
            status="held",
            notes="G1/B-2/B-3e-2 holds — design/resolution support only; "
                  "no unlock.",
        ),
        WorkflowDef(
            "upscale", "Enhancement / upscaling",
            required_modes=("upscale",),
            required_components=("diffusion",),
            status="missing",
            notes="No task or adapter on main.",
        ),
        WorkflowDef(
            "audio_generation", "Audio generation",
            required_modes=("text2audio",),
            required_components=("audio_decoder",),
            status="implemented",
            notes="text2audio (mock adapter) exists.",
        ),
        WorkflowDef(
            "voice_generation", "Voice generation",
            required_modes=("dialogue2audio",),
            required_components=("audio_decoder",),
            status="partial",
            notes="dialogue2audio covers screenplay dialogue; no "
                  "freeform TTS/clone.",
        ),
        WorkflowDef(
            "lipsync", "Lip synchronization",
            required_modes=("lipsync",),
            required_components=("image_encoder", "audio_encoder"),
            status="missing",
            notes="No task or adapter on main.",
        ),
        WorkflowDef(
            "timeline_render", "Timeline-driven rendering",
            required_modes=("export",),
            status="implemented",
            notes="timeline_export + N-series compositor — orchestration, "
                  "not a model workflow.",
        ),
        WorkflowDef(
            "keyframe_video", "Keyframe interpolation video",
            required_modes=("keyframe2video",),
            required_components=("diffusion",),
            status="implemented",
            notes="keyframe2video task exists (frame interpolation path).",
        ),
    ]
}


def get_workflow(key: str | None) -> WorkflowDef | None:
    if not key:
        return None
    return WORKFLOW_DEFS.get(key)


def workflow_catalog() -> list[WorkflowDef]:
    return list(WORKFLOW_DEFS.values())
