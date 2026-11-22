"""U-4a — declarative model-family registry + pure artifact classification.

Classifies a *registered* model_artifacts row from STORED EVIDENCE ONLY:

  - catalog artifacts: the ModelSpec projection already on the row
    (family/fmt/modality/kind plus the meta_json files manifest and
    capability/requirements blocks) — confidence "declared", the catalog
    is the app's trusted declaration.
  - import artifacts: the bounded inspection report written by U-2
    (import_report_json: fmt, suggested_arch, suggested_role, the full
    safetensors __metadata__ string map, the GGUF architecture string,
    and the capped tensor_keys_sample / kv evidence).

This module NEVER opens, reads, hashes, or parses a model file, never
touches an unimported discovery candidate, and never synthesizes a disc:
projection — U-2 remains the only registration pathway. Filenames are
never used as architecture evidence.

Confidence scale (independent of the V-level axis in U-4b):
  declared   — catalog spec (trusted, app-authored)
  metadata   — untrusted in-file metadata (__metadata__ / GGUF kv claims)
  signature  — tensor-key / GGUF architecture signatures observed by U-2
  unverified — evidence exists but cannot support an identification
  unknown    — no usable evidence, or conflicting evidence unresolved

Conflicts between evidence sources are recorded and resolve
conservatively to family_id=None — never silently picked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FAMILIES_VERSION = 1


@dataclass(frozen=True)
class ModelFamilySpec:
    """One declarative model family. Presence means "the registry knows
    this architecture" — never that a runtime supports it (that claim
    lives in runtime_caps)."""

    family_id: str
    display_name: str
    modality: str                     # image|video|audio|text|multimodal
    kind: str                         # model|adapter|auxiliary
    fmts: tuple[str, ...]
    # Evidence signatures — all optional:
    arch_aliases: tuple[str, ...] = ()        # canonical arch names for this family
    gguf_arches: tuple[str, ...] = ()         # GGUF general.architecture values
    st_sig_all: tuple[str, ...] = ()          # every substring must appear in tensor keys
    st_sig_any: tuple[str, ...] = ()          # ≥1 must appear (iff non-empty)
    st_sig_absent: tuple[str, ...] = ()       # all must be absent (auxiliary carve-outs)
    metadata_arch_values: tuple[str, ...] = ()  # modelspec.architecture / ss_base_model_version
    base_family: str | None = None            # adapters: required base family
    component_roles: tuple[str, ...] = ()     # pipeline roles the family needs
    modes: tuple[str, ...] = ()
    requirements_profile: dict = field(default_factory=dict)
    notes: str = ""


FAMILIES: dict[str, ModelFamilySpec] = {
    f.family_id: f for f in (
        # -------------------------------------------------- image diffusion
        ModelFamilySpec(
            family_id="sd1.5",
            display_name="Stable Diffusion 1.5",
            modality="image", kind="model",
            fmts=("safetensors", "gguf"),
            arch_aliases=("sd1.5", "sd15", "stable-diffusion-1.5"),
            st_sig_all=("model.diffusion_model.", "first_stage_model.",
                        "cond_stage_model."),
            st_sig_absent=("conditioner.", "text_encoder_2."),
            metadata_arch_values=("stable-diffusion-v1", "sd_v1-5",
                                  "v1-5-pruned-emaonly", "sd_v1-4",
                                  "sd_v2-1"),
            modes=("text2image", "image2image"),
            requirements_profile={"min_vram_bytes": 4 * 1024**3},
        ),
        ModelFamilySpec(
            family_id="sdxl",
            display_name="Stable Diffusion XL",
            modality="image", kind="model",
            fmts=("safetensors",),
            arch_aliases=("sdxl", "stable-diffusion-xl"),
            st_sig_any=("conditioner.", "text_encoder_2."),
            metadata_arch_values=(
                "stable-diffusion-xl-v1-base", "sd_xl_base",
                "sd_xl_base_1.0", "sd_xl_refiner", "sd_xl_refiner_1.0"),
            modes=("text2image", "image2image"),
            requirements_profile={"min_vram_bytes": 8 * 1024**3},
        ),
        ModelFamilySpec(
            family_id="flux",
            display_name="FLUX",
            modality="image", kind="model",
            fmts=("gguf", "safetensors"),
            arch_aliases=("flux", "flux1-schnell", "flux1-dev"),
            st_sig_all=("double_blocks", "img_in", "txt_in"),
            metadata_arch_values=("flux1-dev", "flux1-schnell", "flux"),
            modes=("text2image",),
            requirements_profile={"min_vram_bytes": 12 * 1024**3},
        ),
        # -------------------------------------------------- video diffusion
        ModelFamilySpec(
            family_id="wan",
            display_name="Wan video (1.x/2.x)",
            modality="video", kind="model",
            fmts=("safetensors",),
            arch_aliases=("wan", "wan2.1", "wan2.2"),
            st_sig_all=("condition_embedder",),
            metadata_arch_values=("wan", "wan2.1", "wan2.2"),
            component_roles=("diffusion", "vae", "text_encoder"),
            modes=("text2video", "image2video"),
            requirements_profile={"min_vram_bytes": 8 * 1024**3},
            notes="Multi-component pipeline: diffusion + VAE + T5-class "
                  "text encoder ship as separate files.",
        ),
        ModelFamilySpec(
            family_id="ltx",
            display_name="LTX-Video",
            modality="video", kind="model",
            fmts=("safetensors",),
            arch_aliases=("ltx", "ltxv"),
            st_sig_all=("patchify_proj",),
            metadata_arch_values=("ltxv", "ltx-video"),
            modes=("text2video", "image2video"),
        ),
        ModelFamilySpec(
            family_id="hunyuan",
            display_name="HunyuanVideo",
            modality="video", kind="model",
            fmts=("safetensors",),
            arch_aliases=("hunyuan", "hunyuanvideo"),
            st_sig_all=("double_blocks", "img_attn"),
            metadata_arch_values=("hunyuan-video", "hunyuanvideo"),
            modes=("text2video",),
        ),
        ModelFamilySpec(
            family_id="qwen",
            display_name="Qwen-Image",
            modality="image", kind="model",
            fmts=("safetensors",),
            arch_aliases=("qwen", "qwen-image"),
            st_sig_all=("transformer_blocks", "img_mlp"),
            metadata_arch_values=("qwen-image",),
            modes=("text2image",),
        ),
        # ------------------------------------------------- language (GGUF)
        ModelFamilySpec(
            family_id="llm_llama",
            display_name="LLaMA-family LLM (GGUF)",
            modality="text", kind="model",
            fmts=("gguf",),
            arch_aliases=("llama",),
            gguf_arches=("llama",),
            modes=("text",),
        ),
        ModelFamilySpec(
            family_id="llm_qwen",
            display_name="Qwen LLM (GGUF)",
            modality="text", kind="model",
            fmts=("gguf",),
            arch_aliases=("qwen",),
            gguf_arches=("qwen2", "qwen3"),
            modes=("text",),
        ),
        ModelFamilySpec(
            family_id="llm_mistral",
            display_name="Mistral/Mixtral LLM (GGUF)",
            modality="text", kind="model",
            fmts=("gguf",),
            arch_aliases=("mistral", "mixtral"),
            gguf_arches=("mistral", "mixtral"),
            modes=("text",),
        ),
        ModelFamilySpec(
            family_id="llm_gemma",
            display_name="Gemma LLM (GGUF)",
            modality="text", kind="model",
            fmts=("gguf",),
            arch_aliases=("gemma",),
            gguf_arches=("gemma", "gemma2"),
            modes=("text",),
        ),
        ModelFamilySpec(
            family_id="llm_phi",
            display_name="Phi LLM (GGUF)",
            modality="text", kind="model",
            fmts=("gguf",),
            arch_aliases=("phi",),
            gguf_arches=("phi3",),
            modes=("text",),
        ),
        ModelFamilySpec(
            family_id="llm_gpt2",
            display_name="GPT-2 LLM (GGUF)",
            modality="text", kind="model",
            fmts=("gguf",),
            arch_aliases=("gpt2",),
            gguf_arches=("gpt2",),
            modes=("text",),
        ),
        # ---------------------------------------------------- auxiliaries
        ModelFamilySpec(
            family_id="aux_vae",
            display_name="Standalone VAE",
            modality="image", kind="auxiliary",
            fmts=("safetensors",),
            st_sig_any=("first_stage_model.", "encoder.", "decoder."),
            st_sig_absent=("model.diffusion_model.", "cond_stage_model.",
                           "conditioner."),
            modes=(),
        ),
        ModelFamilySpec(
            family_id="aux_text_encoder",
            display_name="Standalone text encoder",
            modality="image", kind="auxiliary",
            fmts=("safetensors",),
            st_sig_any=("cond_stage_model.transformer.", "text_model.",
                        "encoder.block."),
            st_sig_absent=("model.diffusion_model.", "first_stage_model."),
            modes=(),
        ),
    )
}


def get_family(family_id: str | None) -> ModelFamilySpec | None:
    if not family_id:
        return None
    return FAMILIES.get(family_id)


def family_catalog() -> list[ModelFamilySpec]:
    return list(FAMILIES.values())


# ------------------------------------------------------------ input view

@dataclass(frozen=True)
class ArtifactView:
    """The stored-evidence view of ONE registered artifact. Callers build
    this from a model_artifacts row; nothing here touches the filesystem."""

    artifact_id: str
    source_type: str                     # catalog | import
    managed_by: str                      # atomic | external
    kind: str = "model"
    arch: str = "unknown"
    family: str = ""
    modality: str = "image"
    fmt: str = "unknown"
    role: str = ""
    meta: dict = field(default_factory=dict)
    import_report: dict | None = None


def view_from_row(row) -> ArtifactView:
    """Duck-typed adapter from a ModelArtifact ORM row (or a test double
    with the same attribute names)."""
    return ArtifactView(
        artifact_id=row.artifact_id,
        source_type=row.source_type,
        managed_by=row.managed_by,
        kind=row.kind or "model",
        arch=row.arch or "unknown",
        family=row.family or "",
        modality=row.modality or "image",
        fmt=row.fmt or "unknown",
        role=row.role or "",
        meta=dict(row.meta_json or {}),
        import_report=dict(row.import_report_json)
        if getattr(row, "import_report_json", None) else None,
    )


# ------------------------------------------------------------- output

@dataclass(frozen=True)
class Classification:
    """Deterministic classification result. family_id=None means unknown —
    never guessed. `conflicts` records every disagreeing evidence pair."""

    family_id: str | None
    arch: str                            # resolved arch or "unknown"
    kind: str                            # model | adapter | auxiliary
    modality: str
    fmt: str
    base_family: str | None
    confidence: str                      # declared|metadata|signature|unverified|unknown
    precision: str | None                # evidenced precision or None
    evidence: tuple[dict, ...]
    conflicts: tuple[dict, ...]
    components_required: tuple[str, ...]
    components_present: tuple[str, ...]


# --------------------------------------------------------- claim helpers

def _claim(source: str, field_name: str, claim: str,
           confidence: str) -> dict:
    return {"source": source, "field": field_name, "claim": claim,
            "confidence": confidence}


def _resolve_family_token(token: str, *, via: str) -> str | None:
    """Map a raw architecture token to a registered family_id.

    `via` scopes resolution to the namespace the token came from —
    "qwen" means Qwen-Image on safetensors but the Qwen LLM family on a
    GGUF arch string. Ambiguity (a token matching >1 family within its
    namespace) resolves to None — never to an arbitrary pick.
    """
    t = (token or "").strip().lower()
    if not t:
        return None
    fams = list(FAMILIES.values())
    if via == "gguf":
        cands = [f for f in fams
                 if t in [g.lower() for g in f.gguf_arches]]
        if not cands:
            # The U-2 GGUF arch map already canonicalizes (qwen2→"qwen");
            # only GGUF-capable families may absorb that canonical name.
            cands = [f for f in fams if "gguf" in f.fmts
                     and (f.family_id == t
                          or t in [a.lower() for a in f.arch_aliases])]
    elif via == "signature":
        # safetensors key-signature output — only ST-capable families.
        cands = [f for f in fams if "safetensors" in f.fmts
                 and (f.family_id == t
                      or t in [a.lower() for a in f.arch_aliases])]
    else:  # metadata values (modelspec.architecture, ss_base_model_version)
        cands = [f for f in fams
                 if f.family_id == t
                 or t in [m.lower() for m in f.metadata_arch_values]
                 or t in [a.lower() for a in f.arch_aliases]]
    return cands[0].family_id if len(cands) == 1 else None


_LORA_META_KEYS = ("ss_network_module", "ss_network_dim", "ss_network_alpha")
_LORA_KEY_HINTS = ("lora_down", "lora_up", "lora_a", "lora_b", ".lora.",
                  "hada", "dora_scale")


def _sig_hits(keys: set[str], spec: ModelFamilySpec) -> bool:
    """Signature match for a family against an observed tensor-key set.
    `all` sigs must hit, every `absent` sig must miss, and ≥1 `any` sig
    must hit when the family declares them."""
    if spec.st_sig_all and not all(
            any(s in k for k in keys) for s in spec.st_sig_all):
        return False
    if spec.st_sig_absent and any(
            any(s in k for k in keys) for s in spec.st_sig_absent):
        return False
    if spec.st_sig_any and not any(
            any(s in k for k in keys) for s in spec.st_sig_any):
        return False
    return bool(spec.st_sig_all or spec.st_sig_any)


def _metadata_claims(report: dict, fmt: str) -> list[dict]:
    """Architecture claims from stored import-report metadata."""
    claims: list[dict] = []
    meta = report.get("metadata") or {}
    # safetensors __metadata__ carries the whole string map; GGUF stores
    # {"architecture": raw, "name": ...} — the field resolves in the
    # namespace the container dictates.
    arch_val = meta.get("modelspec.architecture") or meta.get("architecture")
    if arch_val:
        via = "gguf" if fmt == "gguf" and "architecture" in meta \
            else "metadata"
        fam = _resolve_family_token(str(arch_val), via=via)
        claims.append(_claim(
            "metadata", "modelspec.architecture" if via == "metadata"
            else "architecture",
            fam or str(arch_val)[:80],
            "metadata" if fam else "unverified"))
    base_ver = meta.get("ss_base_model_version")
    if base_ver:
        fam = _resolve_family_token(str(base_ver), via="metadata")
        claims.append(_claim(
            "metadata", "ss_base_model_version",
            fam or str(base_ver)[:80],
            "metadata" if fam else "unverified"))
    return claims


def _classify_import(view: ArtifactView, report: dict) -> Classification:
    evidence: list[dict] = []
    conflicts: list[dict] = []

    fmt = report.get("fmt") or view.fmt
    meta = report.get("metadata") or {}
    sample = set((report.get("evidence") or {}).get("tensor_keys_sample") or [])
    sample_lower = {k.lower() for k in sample}

    # LoRA detection runs FIRST — adapter key namespaces can false-hit
    # auxiliary/model signatures, so adapter files skip the sample
    # family scan entirely and take their family from base-model claims.
    is_lora = (
        report.get("suggested_role") == "lora"
        or any(k in meta for k in _LORA_META_KEYS)
        or any(any(h in k for h in _LORA_KEY_HINTS) for k in sample_lower)
    )

    evidence.extend(_metadata_claims(report, fmt))

    # U-2 signature suggestions — the inspector already ran key/GGUF-arch
    # signature matching over the FULL header (not just the stored sample).
    suggested = report.get("suggested_arch") or "unknown"
    if suggested != "unknown":
        fam = _resolve_family_token(
            suggested, via="gguf" if fmt == "gguf" else "signature")
        evidence.append(_claim("signature", "suggested_arch",
                               fam or suggested,
                               "signature" if fam else "unverified"))

    # The capped tensor_keys_sample can only prove presence — never
    # absence — so it only yields positive hits, never refusals. Adapter
    # files carry lora-prefixed keys, not base-model signatures.
    if not is_lora:
        sample_hits = [f.family_id for f in FAMILIES.values()
                       if _sig_hits(sample_lower, f)]
        for fid in sample_hits:
            evidence.append(_claim("signature", "tensor_keys_sample",
                                   fid, "signature"))

    # Resolve family claims: distinct families disagreeing = conflict.
    claimed = {(e["claim"], e["confidence"]) for e in evidence
               if e["claim"] in FAMILIES}
    families_claimed = {c for c, _ in claimed}
    if len(families_claimed) > 1:
        for a, b in _pairs(sorted(families_claimed)):
            conflicts.append({"type": "arch_conflict", "claims": [a, b],
                              "sources": [e["field"] for e in evidence
                                          if e["claim"] in (a, b)]})
        family_id, confidence = None, "unverified"
    elif families_claimed:
        family_id = families_claimed.pop()
        conf_rank = {"declared": 3, "signature": 2, "metadata": 1}
        confidence = max((c for c in claimed if c[0] == family_id),
                         key=lambda x: conf_rank.get(x[1], 0))[1]
    else:
        family_id, confidence = None, "unknown"

    # Kind: adapter when LoRA evidence exists.
    if is_lora:
        evidence.append(_claim("signature", "lora_markers", "lora",
                               "signature"))
        kind = "adapter"
    elif family_id and FAMILIES[family_id].kind == "auxiliary":
        kind = "auxiliary"
    else:
        kind = "model"

    base_family = family_id if kind == "adapter" else None
    fam_spec = get_family(family_id)

    # Precision: safetensors dtype census and GGUF file_type are NOT in
    # the stored U-2 report — honest None, never inferred.
    precision = None

    components_required = tuple(fam_spec.component_roles) if fam_spec else ()
    components_present = ("single_file",) if kind != "adapter" else ()

    return Classification(
        family_id=family_id,
        arch=family_id or "unknown",
        kind=kind,
        modality=fam_spec.modality if fam_spec else view.modality,
        fmt=fmt,
        base_family=base_family,
        confidence=confidence,
        precision=precision,
        evidence=tuple(evidence),
        conflicts=tuple(conflicts),
        components_required=components_required,
        components_present=components_present,
    )


def _pairs(seq: list[str]):
    for i in range(len(seq)):
        for j in range(i + 1, len(seq)):
            yield seq[i], seq[j]


def _classify_catalog(view: ArtifactView) -> Classification:
    """Catalog rows carry the trusted ModelSpec declaration — the family
    claim is 'declared' evidence; no metadata spelunking is needed."""
    evidence = [_claim("catalog", "family", view.family or "unknown",
                       "declared")]
    conflicts: list[dict] = []
    fam = get_family(view.family)
    family_id = fam.family_id if fam else None
    if view.family and fam is None:
        conflicts.append({"type": "catalog_family_unregistered",
                          "claims": [view.family], "sources": ["family"]})

    files = (view.meta or {}).get("files") or []
    roles_present = tuple(sorted({f.get("role", "diffusion")
                                  for f in files if isinstance(f, dict)}))
    components_required = tuple(fam.component_roles) if fam else ()

    caps = (view.meta or {}).get("capabilities") or {}
    precision = None
    for cand in caps.get("precision") or []:
        if isinstance(cand, str) and cand:
            precision = cand
            evidence.append(_claim("catalog", "capabilities.precision",
                                   cand, "declared"))
            break

    kind = view.kind or (fam.kind if fam else "model")
    return Classification(
        family_id=family_id,
        arch=view.family or "unknown",
        kind=kind,
        modality=view.modality or (fam.modality if fam else "image"),
        fmt=view.fmt,
        base_family=(fam.base_family or view.family)
        if kind == "adapter" else None,
        confidence="declared" if family_id else "unknown",
        precision=precision,
        evidence=tuple(evidence),
        conflicts=tuple(conflicts),
        components_required=components_required,
        components_present=roles_present,
    )


def classify_artifact(view: ArtifactView) -> Classification:
    """Classify one registered artifact from stored evidence.

    Pure and deterministic: same view ⇒ same Classification. Unknown is
    always an honest result — never inferred from a filename.
    """
    if view.source_type == "import" and view.import_report:
        return _classify_import(view, view.import_report)
    if view.source_type == "catalog":
        return _classify_catalog(view)
    # Import row missing its report, or a source_type outside the U-1
    # contract — insufficient evidence by definition.
    return Classification(
        family_id=None, arch="unknown",
        kind=view.kind or "model", modality=view.modality or "image",
        fmt=view.fmt or "unknown", base_family=None,
        confidence="unverified", precision=None,
        evidence=(), conflicts=(),
        components_required=(), components_present=(),
    )
