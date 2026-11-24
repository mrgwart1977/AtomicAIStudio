"""U-5c-2a — declarative pipeline contracts.

Versioned, code-declared, deeply immutable pipeline definitions. A
pipeline is a DAG of WHOLE-JOB nodes: every node maps to an existing
command in the job-command table, and every edge is an asset-mediated
dependency grounded in a verified command argument (e.g. the
`image_asset_id` input of `generate.video`).

This module NEVER:

- enqueues jobs, submits commands, or executes anything;
- probes hardware, touches the scheduler, or reserves resources;
- reads the database, the filesystem, or any project asset row;
- claims operational readiness — `operational_readiness` is always the
  literal "not_evaluated". A statically valid template is not a
  runnable one.

Definitions are validated against an injected static evidence context
(command table, adapter profiles, workflow declarations, runtime
declarations) supplied by the caller — the same injection pattern as
the U-5c-1 preview. pipeline_defs imports nothing from commands,
adapters or the scheduler, so no circular imports can form.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

# Bump on ANY contract/schema change — feeds definition fingerprints.
PIPELINE_SCHEMA_VERSION = 1
PIPELINE_DEFS_VERSION = 1
# Bump when the audited static command contract changes.
COMMAND_CONTRACTS_VERSION = 2

LIMITS = {
    "max_pipelines": 64,
    "max_nodes": 32,
    "max_edges": 64,
    "max_ports_per_node": 8,          # combined input + output ports
    "max_depth": 16,
    "max_external_params": 64,
    "max_param_bindings_per_node": 32,
    "max_identifier_length": 64,
}

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ASSET_KINDS = ("image", "video", "audio", "other")
ASSET_ROLES = ("init_image",)
PTYPES = ("string", "int", "float", "bool")
CONSTRAINT_KINDS = ("min", "max", "choices", "min_len", "max_len",
                    "pattern")
OWNERSHIP_KINDS = ("same_project",)
PROVENANCE_KINDS = ("generated",)
NODE_KINDS = ("job",)

_PY_SCALARS = (str, int, float, bool, type(None))


def _deep_immutable(obj: Any) -> bool:
    """True when obj contains no mutable container anywhere (S1)."""
    if isinstance(obj, _PY_SCALARS):
        return True
    if isinstance(obj, (tuple, frozenset)):
        return all(_deep_immutable(x) for x in obj)
    return False


def _check_deep_immutable(defn) -> list[str]:
    """Collect the dotted paths of mutable containers inside a def."""
    bad: list[str] = []

    def walk(v, path: str) -> None:
        if isinstance(v, _PY_SCALARS):
            return
        if isinstance(v, (list, dict, set, bytearray)):
            bad.append(path)
            return
        if isinstance(v, (tuple, frozenset)):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
            return
        for f_ in getattr(v, "__dataclass_fields__", {}):
            walk(getattr(v, f_), f"{path}.{f_}")

    walk(defn, "def")
    return bad


# ------------------------------------------------------------------
# Schema — frozen dataclasses; every collection is a tuple, every
# nested value a scalar or tuple, so definitions are immutable all
# the way down (safeguard S1).
# ------------------------------------------------------------------


@dataclass(frozen=True)
class Constraint:
    """A typed, immutable parameter constraint."""
    kind: str                     # CONSTRAINT_KINDS
    value: object                 # scalar or tuple — never a container


@dataclass(frozen=True)
class ParamDecl:
    """A pipeline-level external input declaration."""
    param_key: str
    ptype: str                    # PTYPES
    required: bool
    description: str = ""
    has_default: bool = False     # overridable default — optional only
    default: object = None
    constraints: tuple[Constraint, ...] = ()


@dataclass(frozen=True)
class ParamBinding:
    """How one request key of a node command is satisfied."""
    request_key: str              # actual command payload argument
    source: str                   # "external" | "edge" | "constant"
    external_key: str | None = None    # source="external"
    edge_id: str | None = None         # source="edge"
    constant: object = None            # source="constant" — fixed,
                                     # non-overridable value (batch=1)
    constraints: tuple[Constraint, ...] = ()


@dataclass(frozen=True)
class PortDecl:
    """A typed asset port on a node."""
    port_key: str
    direction: str                # "in" | "out"
    asset_kind: str               # ASSET_KINDS
    required: bool = True         # in-ports: pipeline-level requirement —
                                # independent of command-level optionality
    cardinality: int = 1          # 1 only in U-5c-2a — no fan-out
    command_arg: str | None = None     # in-ports: the real request key the
                                     # asset feeds ("image_asset_id")
    output_contract: str | None = None  # out-ports: "primary_asset" — the
                                        # job's registered primary output


@dataclass(frozen=True)
class PipelineEdge:
    """An asset-mediated dependency between two node ports."""
    edge_id: str
    src_node: str
    src_port: str
    dst_node: str
    dst_port: str
    dst_command_arg: str          # must equal the dst port's command_arg —
                                # the verified request key, not a guess
    asset_role: str               # semantic label ("init_image")
    ownership: str                # "same_project" — mirrors the command's
                                # project_id check on the asset row
    provenance: str               # "generated" — source asset is expected
                                # to be a generation output of src node


@dataclass(frozen=True)
class PipelineNode:
    """One executable whole-job node."""
    node_id: str
    node_kind: str                # "job" — the only kind in U-5c-2a
    command_key: str              # must exist in the injected command table
    workflow_key: str | None      # must exist in WORKFLOW_DEFS when set and
                                # must be a workflow the command serves
    runtime_keys: tuple[str, ...] # admissible declared runtimes — never ()
    required_components: tuple[str, ...]  # dep-graph roles; declared MINIMUM
                                        # — family-specific extras (e.g. an
                                        # i2v image encoder) are resolved by
                                        # U-5b at plan time, not asserted here
    input_ports: tuple[PortDecl, ...] = ()
    output_ports: tuple[PortDecl, ...] = ()
    param_bindings: tuple[ParamBinding, ...] = ()
    policy_notes: tuple[str, ...] = ()   # explanatory metadata only —
                                       # never an authorization signal


@dataclass(frozen=True)
class PipelineDefinition:
    pipeline_key: str
    schema_version: int
    definition_version: int
    display_name: str             # presentation-only — excluded from
    description: str              #   the definition fingerprint
    category: str                 # "generation_chain"
    status: str                   # "declared" (only value in U-5c-2a)
    required_context: tuple[str, ...]
    policy_constraints: tuple[str, ...]  # explanatory, e.g. "lora_holds_apply"
    limitations: tuple[str, ...]
    external_params: tuple[ParamDecl, ...]
    nodes: tuple[PipelineNode, ...]
    edges: tuple[PipelineEdge, ...]


# ------------------------------------------------------------------
# Audited static command contract (safeguard S3).
#
# Source of truth: app/domain/commands.py _job_request/_validate and
# the adapter request/output contracts. Audited values are STATIC
# declarations of the real argument names, optionality, conditional
# requirements, types, asset-argument semantics and output contracts —
# they can drift when the real builders change, so every assertion is
# re-verified by tests that read the live request builders' contracts
# (JOB_COMMANDS mapping, catalog modality checks, validation branches).
# ------------------------------------------------------------------


@dataclass(frozen=True)
class CommandArg:
    name: str
    ptype: str                    # PTYPES
    required: bool = False        # unconditionally required by the builder
    conditional_group: str | None = None  # members of a group: >=1 required
    is_asset_arg: bool = False    # the value is a project asset reference
    computed: bool = False        # engine-computed — callers may NOT bind it
    refused: bool = False         # explicitly refused by the command
    has_default: bool = False
    default: object = None
    constraints: tuple[Constraint, ...] = ()
    # How completely the static contract models this argument for
    # PIPELINE BINDING — distinct from what the real command accepts:
    #   full        — real contract fully represented; bindings OK
    #   partial     — real contract is richer (e.g. device also takes an
    #                 int GPU index); bindings allowed within the
    #                 modeled value space only
    #   unsupported — real arg exists but cannot be safely modeled
    #                 (paired dims, selection lists); bindings rejected
    binding_support: str = "full"
    note: str = ""                # audit note — documents the gap
                                 # between real support and modeled
                                 # binding support


@dataclass(frozen=True)
class CommandContract:
    command_key: str
    workflow_keys: tuple[str, ...]        # workflows this command can serve
    runtime_keys: tuple[str, ...]
    args: tuple[CommandArg, ...]
    conditional_groups: tuple[tuple[str, ...], ...]
    output_kind: str                      # primary output asset kind
    output_origin: str                    # literal persisted asset origin
                                          # (adapters emit "generation";
                                          # jobs.py defaults "generated")
    extra_outputs_possible: bool          # batch>1 -> extra_assets
    output_count_arg: str | None = None   # request arg multiplying the
                                          # primary output ("batch") —
                                          # pipelines pin it to 1
    notes: tuple[str, ...] = ()
    audited_from: str = ""                # source citation

    def arg(self, name: str) -> CommandArg | None:
        return next((a for a in self.args if a.name == name), None)


COMMAND_CONTRACTS: dict[str, CommandContract] = {
    # Audited: commands.py _job_request "generate.image" builder
    # (model_key+prompt required; batch<=4 -> primary + extra_assets).
    "generate.image": CommandContract(
        command_key="generate.image",
        workflow_keys=("text2image",),
        runtime_keys=("sdcpp",),
        args=(
            CommandArg("model_key", "string", required=True),
            CommandArg("prompt", "string", required=True),
            CommandArg("negative_prompt", "string",
                       has_default=True, default=""),
            CommandArg("width", "int", has_default=True, default=512,
                       constraints=(Constraint("min", 1),
                                    Constraint("max", 4096))),
            CommandArg("height", "int", has_default=True, default=512,
                       constraints=(Constraint("min", 1),
                                    Constraint("max", 4096))),
            CommandArg("steps", "int", has_default=True, default=25),
            CommandArg("cfg", "float", has_default=True, default=7.0),
            CommandArg("seed", "int", has_default=True, default=-1),
            CommandArg("sampler", "string"),
            CommandArg("batch", "int", has_default=True, default=1,
                       constraints=(Constraint("min", 1),
                                    Constraint("max", 4)),
                       note=("real command accepts 1–4 "
                             "(adapter limits.max_batch); the PIPELINE "
                             "single-output contract pins it to 1 via "
                             "output_count_arg")),
            # commands.py:3076-3080 — device also accepts an int GPU
            # index >=0; only the string modes are modeled for binding.
            CommandArg("device", "string",
                       has_default=True, default="auto",
                       binding_support="partial",
                       note="real contract: 'auto'|'cpu'|int>=0"),
            CommandArg("runtime_flavor", "string"),
            CommandArg("residency_mode", "string"),
            # commands.py:6015-6032 — real contract takes a selections
            # list resolved through check_loras + prompt guard; the
            # scalar-string model cannot represent it, and all LoRA
            # selections remain subject to existing authorization holds.
            CommandArg("loras", "string",
                       binding_support="unsupported",
                       note="real contract: resolved selections list"),
            CommandArg("shot_id", "string"),
            CommandArg("scene_id", "string"),
            CommandArg("set_thumbnail", "bool",
                       has_default=True, default=False),
            CommandArg("memory_risk_acknowledged", "bool",
                       has_default=True, default=False),
            CommandArg("execution_target", "string"),
            CommandArg("local_adapter_id", "string"),
            CommandArg("est_vram_bytes", "int", computed=True),
            CommandArg("allow_cpu", "bool", computed=True),
            # commands.py:6058-6060 — accepted as passthrough when no
            # loras are bound, engine-computed when they are; either
            # way it is not a pipeline-bindable argument.
            CommandArg("runtime_prompt_sha256", "string", computed=True,
                       note="conditional passthrough in the real "
                            "contract; unbindable"),
        ),
        conditional_groups=(),
        output_kind="image",
        output_origin="generation",
        extra_outputs_possible=True,        # batch>1 -> extra_assets
        output_count_arg="batch",
        notes=(("local sd.cpp subprocess adapter; batch N yields N "
                "images — primary output plus extra_assets"),),
        audited_from="commands.py:_job_request generate.image + "
                     "adapters/inference.py SdCppAdapter",
    ),
    # Audited: commands.py _job_request/_check "generate.video" —
    # model_key required; prompt OR image_asset_id (same-project,
    # kind=="image") required; resolved to image_path+source_sha256 at
    # commit; allow_cpu hard-coded False.
    "generate.video": CommandContract(
        command_key="generate.video",
        workflow_keys=("text2video", "image2video"),
        runtime_keys=("sdcpp",),
        args=(
            CommandArg("model_key", "string", required=True),
            CommandArg("prompt", "string",
                       conditional_group="video_source"),
            CommandArg("image_asset_id", "string",
                       conditional_group="video_source",
                       is_asset_arg=True),
            CommandArg("negative_prompt", "string",
                       has_default=True, default=""),
            # commands.py:3166-3184 — width/height are a PAIRED argument
            # (both or neither) bounded to 64–2048 or the model's
            # catalog preset table, with i2v aspect resolution when
            # absent. Not modeled for pipeline binding in U-5c-2a.
            CommandArg("width", "int",
                       binding_support="unsupported",
                       note="paired w/ height; model presets|64-2048"),
            CommandArg("height", "int",
                       binding_support="unsupported",
                       note="paired w/ width; model presets|64-2048"),
            CommandArg("frames", "int", has_default=True, default=33,
                       note="catalog capabilities bounds at commit"),
            CommandArg("fps", "int", has_default=True, default=24,
                       note="catalog capabilities bounds at commit"),
            CommandArg("steps", "int", has_default=True, default=30),
            CommandArg("cfg", "float", has_default=True, default=7.0),
            CommandArg("seed", "int", has_default=True, default=-1),
            CommandArg("sampler", "string"),
            CommandArg("device", "string",
                       has_default=True, default="auto",
                       binding_support="partial",
                       note="real contract: 'auto'|'cpu'|int>=0"),
            CommandArg("runtime_flavor", "string"),
            CommandArg("residency_mode", "string"),
            CommandArg("max_vram_gib", "int"),
            # commands.py:3113-3118 + 6412-6417 — refused twice
            # (validate + request builder); selections list anyway.
            CommandArg("loras", "string", refused=True,
                       note="refused by the real command"),
            CommandArg("shot_id", "string"),
            CommandArg("scene_id", "string"),
            CommandArg("memory_risk_acknowledged", "bool",
                       has_default=True, default=False),
            CommandArg("est_vram_bytes", "int", computed=True),
            # hard-coded False in the builder — never caller input.
            CommandArg("allow_cpu", "bool", computed=True),
            # resolved from image_asset_id at commit (commands.py:6455-6463)
            CommandArg("image_path", "string", computed=True),
            CommandArg("source_sha256", "string", computed=True),
        ),
        conditional_groups=(("prompt", "image_asset_id"),),
        output_kind="video",
        output_origin="generation",
        extra_outputs_possible=False,
        notes=(("prompt OR image_asset_id satisfies the video-source "
                "requirement — command-level optionality does NOT relax "
                "pipeline-level required ports"),),
        audited_from="commands.py:_job_request/_validate generate.video "
                     "+ adapters/inference.py SdCppVideoAdapter",
    ),
}


# ------------------------------------------------------------------
# Evidence context — injected static evidence (no imports back into
# commands/adapters; the caller supplies it).
# ------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowEvidence:
    workflow_key: str
    status: str                  # implemented|partial|missing|held
    policy: str                  # none|lora_hold|cloud_gated|scheduled
    required_components: tuple[str, ...]
    optional_components: tuple[str, ...]
    defs_version: int = 0


@dataclass(frozen=True)
class EvidenceContext:
    """Static evidence the validator/describe layer may consume."""
    # command_key -> adapter_id (the JOB_COMMANDS table, verbatim)
    command_table: tuple[tuple[str, str], ...]
    # adapter_id -> {"adapter_kind": str, "notes": str}
    adapter_profiles: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    # workflow_key -> WorkflowEvidence
    workflows: tuple[WorkflowEvidence, ...]
    # runtime_key -> planned bool (True = declared-planned, never
    # operational)
    runtime_planned: tuple[tuple[str, bool], ...]

    def adapter_id(self, command_key: str) -> str | None:
        return dict(self.command_table).get(command_key)

    def adapter_kind(self, adapter_id: str | None) -> str | None:
        if adapter_id is None:
            return None
        prof = dict(self.adapter_profiles).get(adapter_id)
        return dict(prof).get("adapter_kind") if prof else None

    def adapter_notes(self, adapter_id: str | None) -> str:
        if adapter_id is None:
            return ""
        prof = dict(self.adapter_profiles).get(adapter_id)
        return dict(prof).get("notes", "") if prof else ""

    def workflow(self, key: str | None) -> WorkflowEvidence | None:
        if not key:
            return None
        return {w.workflow_key: w for w in self.workflows}.get(key)

    def runtime_state(self, key: str) -> str:
        """declared | planned | unknown — never 'operational'."""
        planned = dict(self.runtime_planned).get(key)
        if planned is None:
            return "unknown"
        return "planned" if planned else "declared"


# ------------------------------------------------------------------
# Validation — cheap checks (versions, limits, identifiers, types)
# run BEFORE any traversal; an over-limit or malformed definition is
# invalid, never silently truncated.
# ------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    code: str
    subject: str                  # node_id / edge_id / param_key / "graph"
    detail: str
    severity: str = "error"


def _ptype_ok(ptype: str, value) -> bool:
    """Type check for declared values — int excludes bool, float
    excludes bool and ALL non-finite values (D2)."""
    if ptype == "string":
        return isinstance(value, str)
    if ptype == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if ptype == "float":
        return (isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value))
    if ptype == "bool":
        return isinstance(value, bool)
    return False


def _constraint_findings(subj: str, ptype: str,
                         constraints: tuple[Constraint, ...]) -> list[Finding]:
    """Validate the constraint DECLARATION itself (D8) — kinds, value
    types, immutability, finiteness, duplicates and internal
    contradictions — independent of any default or bound value."""
    out: list[Finding] = []
    seen: set[str] = set()
    bounds: dict[str, object] = {}
    for c in constraints:
        if c.kind not in CONSTRAINT_KINDS:
            out.append(Finding("invalid_constraint", subj,
                               f"unsupported constraint kind '{c.kind}'"))
            continue
        if c.kind in seen:
            out.append(Finding("invalid_constraint", subj,
                               f"duplicate constraint kind '{c.kind}'"))
        seen.add(c.kind)
        if not _deep_immutable(c.value):
            out.append(Finding("invalid_constraint", subj,
                               f"constraint '{c.kind}' value is mutable"))
            continue
        if c.kind in ("min", "max"):
            if (ptype not in ("int", "float")
                    or not isinstance(c.value, (int, float))
                    or isinstance(c.value, bool)):
                out.append(Finding("invalid_constraint", subj,
                                   f"'{c.kind}' requires a numeric value "
                                   f"and numeric ptype (got {ptype!r})"))
            elif not math.isfinite(c.value):
                out.append(Finding("invalid_constraint", subj,
                                   f"'{c.kind}' value is not finite"))
            else:
                bounds[c.kind] = c.value
        elif c.kind == "choices":
            if not isinstance(c.value, tuple) or not c.value:
                out.append(Finding("invalid_constraint", subj,
                                   "'choices' requires a non-empty tuple"))
            elif not all(_ptype_ok(ptype, v) for v in c.value):
                out.append(Finding("invalid_constraint", subj,
                                   "'choices' elements must match "
                                   f"ptype {ptype!r}"))
        elif c.kind in ("min_len", "max_len"):
            if (ptype != "string" or not isinstance(c.value, int)
                    or isinstance(c.value, bool)):
                out.append(Finding("invalid_constraint", subj,
                                   f"'{c.kind}' requires an int value and "
                                   "string ptype"))
            else:
                bounds[c.kind] = c.value
        elif c.kind == "pattern":
            if ptype != "string" or not isinstance(c.value, str):
                out.append(Finding("invalid_constraint", subj,
                                   "'pattern' requires a string value and "
                                   "string ptype"))
            else:
                try:
                    re.compile(c.value)
                except re.error:
                    out.append(Finding("invalid_constraint", subj,
                                       "'pattern' does not compile"))
    if ("min" in bounds and "max" in bounds
            and bounds["min"] > bounds["max"]):
        out.append(Finding("invalid_constraint", subj,
                           f"min {bounds['min']} > max {bounds['max']}"))
    if ("min_len" in bounds and "max_len" in bounds
            and bounds["min_len"] > bounds["max_len"]):
        out.append(Finding("invalid_constraint", subj,
                           f"min_len {bounds['min_len']} > "
                           f"max_len {bounds['max_len']}"))
    return out


def _effective_domain_empty(ptype: str,
                            layers: list[tuple[Constraint, ...]]
                            ) -> bool:
    """Effective allowed-value domain across ALL applicable constraint
    layers (F2) — external-param declarations, binding-level and
    contract-arg constraints COMBINED, not pairwise. Returns True when
    no value can satisfy the combination:

    - numeric: every 'min' raises the bound, every 'max' lowers it;
      int ptypes additionally require an integer inside the interval
    - string: combined min_len/max_len bounds
    - choices: the intersection of every declared choice set, with
      each survivor re-checked against EVERY layer's constraints
    Domains without choices are unbounded elsewhere — pattern/length
    intersections over infinite string domains cannot be proven empty
    (a documented restriction of the supported constraint language)."""
    lo = hi = None
    lo_len = hi_len = None
    choice_sets: list[set] = []
    for cs in layers:
        for c in cs:
            v = c.value
            if c.kind == "min" and _numeric(v):
                lo = v if lo is None else max(lo, v)
            elif c.kind == "max" and _numeric(v):
                hi = v if hi is None else min(hi, v)
            elif c.kind == "min_len" and isinstance(v, int) \
                    and not isinstance(v, bool):
                lo_len = v if lo_len is None else max(lo_len, v)
            elif c.kind == "max_len" and isinstance(v, int) \
                    and not isinstance(v, bool):
                hi_len = v if hi_len is None else min(hi_len, v)
            elif c.kind == "choices" and isinstance(v, tuple):
                choice_sets.append(set(v))
    if lo is not None and hi is not None:
        if ptype == "int":
            if math.ceil(lo) > math.floor(hi):
                return True
        elif lo > hi:
            return True
    if lo_len is not None and hi_len is not None and lo_len > hi_len:
        return True
    if choice_sets:
        survivors = set.intersection(*choice_sets)
        for cs in layers:
            survivors = {v for v in survivors
                         if _value_meets(ptype, v, cs)}
        if not survivors:
            return True
    return False


def _numeric(v) -> bool:
    return (isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v))


def _value_meets(ptype: str, value,
                 constraints: tuple[Constraint, ...]) -> bool:
    if not _ptype_ok(ptype, value):
        return False
    for c in constraints:
        if c.kind == "min" and value < c.value:
            return False
        if c.kind == "max" and value > c.value:
            return False
        if c.kind == "choices" and value not in c.value:
            return False
        if c.kind == "min_len" and len(value) < c.value:
            return False
        if c.kind == "max_len" and len(value) > c.value:
            return False
        if c.kind == "pattern" and not re.fullmatch(c.value, value):
            return False
    return True


def validate_pipeline(defn: PipelineDefinition,
                      ctx: EvidenceContext) -> tuple[str, list[Finding]]:
    """Deterministic structural validation. Returns ("valid"|"invalid",
    findings) — 'valid' is STRUCTURAL only, never operational."""
    f: list[Finding] = []

    # ---- Phase A: versions, limits, identifiers, field types ----
    if not _IDENT_RE.match(defn.pipeline_key):
        f.append(Finding("bad_identifier", "pipeline",
                         f"pipeline_key {defn.pipeline_key!r}"))
    if defn.schema_version != PIPELINE_SCHEMA_VERSION:
        f.append(Finding("unsupported_schema_version", "pipeline",
                         f"{defn.schema_version} != "
                         f"{PIPELINE_SCHEMA_VERSION}"))
    if not isinstance(defn.definition_version, int) \
            or defn.definition_version < 1:
        f.append(Finding("unsupported_definition_version", "pipeline",
                         repr(defn.definition_version)))
    for path in _check_deep_immutable(defn):
        f.append(Finding("mutable_definition_field", "pipeline",
                         f"mutable container at {path}"))
    if len(defn.nodes) > LIMITS["max_nodes"]:
        f.append(Finding("limit_exceeded", "pipeline",
                         f"nodes {len(defn.nodes)} > "
                         f"{LIMITS['max_nodes']}"))
    if len(defn.edges) > LIMITS["max_edges"]:
        f.append(Finding("limit_exceeded", "pipeline",
                         f"edges {len(defn.edges)} > "
                         f"{LIMITS['max_edges']}"))
    if len(defn.external_params) > LIMITS["max_external_params"]:
        f.append(Finding("limit_exceeded", "pipeline",
                         f"external_params {len(defn.external_params)} > "
                         f"{LIMITS['max_external_params']}"))

    node_ids = [n.node_id for n in defn.nodes]
    edge_ids = [e.edge_id for e in defn.edges]
    param_keys = [p.param_key for p in defn.external_params]
    for label, ids in (("node", node_ids), ("edge", edge_ids),
                       ("external", param_keys)):
        for i in ids:
            if not _IDENT_RE.match(i):
                f.append(Finding("bad_identifier", label, repr(i)))
        for d in {i for i in ids if ids.count(i) > 1}:
            f.append(Finding(f"duplicate_{label}"
                             + ("_param" if label == "external"
                                else "_id"),
                             label, d))

    # Duplicate pipeline keys are validated across the registry.
    node_by_id = {n.node_id: n for n in defn.nodes}
    edge_by_id = {e.edge_id: e for e in defn.edges}
    param_by_key = {p.param_key: p for p in defn.external_params}

    # External param declarations.
    for p in defn.external_params:
        if p.ptype not in PTYPES:
            f.append(Finding("invalid_ptype", "external_param",
                             f"{p.param_key}: {p.ptype!r}"))
            continue
        if p.required and p.has_default:
            f.append(Finding("default_on_required_external", p.param_key,
                             "a required external cannot carry a default"))
        if p.has_default and not _ptype_ok(p.ptype, p.default):
            f.append(Finding("default_type_mismatch", p.param_key,
                             f"default {p.default!r} is not "
                             f"{p.ptype}"))
        elif p.has_default and p.constraints and not \
                _value_meets(p.ptype, p.default, p.constraints):
            f.append(Finding("constraint_violation", p.param_key,
                             f"default {p.default!r} violates declared "
                             "constraints"))
        f.extend(_constraint_findings(p.param_key, p.ptype,
                                    p.constraints))

    # Node-level cheap checks.
    for n in defn.nodes:
        if n.node_kind not in NODE_KINDS:
            f.append(Finding("unsupported_node_kind", n.node_id,
                             repr(n.node_kind)))
        if len(n.input_ports) + len(n.output_ports) \
                > LIMITS["max_ports_per_node"]:
            f.append(Finding("limit_exceeded", n.node_id,
                             "ports exceed max_ports_per_node"))
        if len(n.param_bindings) > LIMITS["max_param_bindings_per_node"]:
            f.append(Finding("limit_exceeded", n.node_id,
                             "param_bindings exceed "
                             "max_param_bindings_per_node"))
        seen_ports: set[str] = set()
        for p in n.input_ports + n.output_ports:
            if not _IDENT_RE.match(p.port_key):
                f.append(Finding("bad_identifier", n.node_id,
                                 f"port {p.port_key!r}"))
            if p.port_key in seen_ports:
                f.append(Finding("duplicate_port_key", n.node_id,
                                 p.port_key))
            seen_ports.add(p.port_key)
            if p.direction not in ("in", "out"):
                f.append(Finding("bad_direction", n.node_id,
                                 f"port {p.port_key} "
                                 f"direction {p.direction!r}"))
            if p.asset_kind not in ASSET_KINDS:
                f.append(Finding("unsupported_asset_kind", n.node_id,
                                 f"port {p.port_key} kind "
                                 f"{p.asset_kind!r}"))
            if p.cardinality != 1:
                f.append(Finding("cardinality_violation", n.node_id,
                                 f"port {p.port_key} cardinality "
                                 f"{p.cardinality} != 1"))
            if p.direction == "in" and p.command_arg is None:
                f.append(Finding("bad_command_arg", n.node_id,
                                 f"in-port {p.port_key} declares no "
                                 "command_arg"))
            if p.direction == "out" and \
                    p.output_contract != "primary_asset":
                f.append(Finding("bad_command_arg", n.node_id,
                                 f"out-port {p.port_key} must declare "
                                 "output_contract='primary_asset'"))
        binding_keys = [b.request_key for b in n.param_bindings]
        for d in {k for k in binding_keys if binding_keys.count(k) > 1}:
            f.append(Finding("conflicting_bindings", n.node_id,
                             f"request_key {d} bound more than once"))

    # Early exit: with limits/identifiers broken, traversal evidence is
    # unreliable — report phase-A findings without descending.
    if f:
        return "invalid", sorted(f, key=lambda x: (x.code, x.subject))

    # ---- Phase B: reference resolution — commands, workflows,
    # runtimes, ports, bindings, edges ----
    port_by_key = {(n.node_id, p.port_key): p
                   for n in defn.nodes for p in
                   (n.input_ports + n.output_ports)}

    for n in defn.nodes:
        contract = COMMAND_CONTRACTS.get(n.command_key)
        if ctx.adapter_id(n.command_key) is None:
            f.append(Finding("unknown_command", n.node_id,
                             n.command_key))
        if contract is None:
            f.append(Finding("missing_command_contract", n.node_id,
                             f"no audited contract for "
                             f"{n.command_key}"))
        wf = ctx.workflow(n.workflow_key)
        if n.workflow_key is not None:
            if wf is None:
                f.append(Finding("unknown_workflow", n.node_id,
                                 n.workflow_key))
            elif contract is not None and \
                    n.workflow_key not in contract.workflow_keys:
                f.append(Finding("workflow_not_served_by_command",
                                 n.node_id,
                                 f"{n.workflow_key} not in "
                                 f"{contract.command_key}.workflow_keys"))
            if wf is not None:
                declared = (set(wf.required_components)
                            | set(wf.optional_components))
                for c in n.required_components:
                    if c not in declared:
                        f.append(Finding("component_not_declared",
                                         n.node_id,
                                         f"{c} not in workflow "
                                         f"{n.workflow_key} components"))
        for rk in n.runtime_keys:
            if contract is not None and rk not in contract.runtime_keys:
                f.append(Finding("unknown_runtime", n.node_id,
                                 f"runtime {rk} not admissible for "
                                 f"{n.command_key}"))
            elif ctx.runtime_state(rk) == "unknown":
                f.append(Finding("unknown_runtime", n.node_id, rk))
        # Ports vs contract.
        for p in n.input_ports:
            if contract is not None and p.command_arg is not None:
                carg = contract.arg(p.command_arg)
                if carg is None:
                    f.append(Finding("unsupported_command_arg", n.node_id,
                                     f"in-port {p.port_key} arg "
                                     f"{p.command_arg} not a "
                                     f"{n.command_key} argument"))
                else:
                    if not carg.is_asset_arg:
                        f.append(Finding("bad_command_arg", n.node_id,
                                         f"in-port {p.port_key} arg "
                                         f"{p.command_arg} is not an "
                                         "asset argument"))
                    if carg.computed:
                        f.append(Finding("binding_to_computed_arg",
                                         n.node_id,
                                         f"in-port {p.port_key} arg "
                                         f"{p.command_arg} is "
                                         "engine-computed"))
                    if carg.refused:
                        f.append(Finding("refused_arg", n.node_id,
                                         f"in-port {p.port_key} arg "
                                         f"{p.command_arg} is refused"))
                    if carg.binding_support == "unsupported":
                        f.append(Finding("unsupported_binding_target",
                                         n.node_id,
                                         f"in-port {p.port_key} arg "
                                         f"{p.command_arg} is not "
                                         "modeled for pipeline binding"))
        # Bindings vs contract + externals + edges.
        bound_args: dict[str, str] = {}
        for b in n.param_bindings:
            if b.source not in ("external", "edge", "constant"):
                f.append(Finding("conflicting_binding_sources",
                                 n.node_id,
                                 f"{b.request_key}: unknown source "
                                 f"{b.source!r}"))
            sources_set = sum(x is not None for x in
                              (b.external_key, b.edge_id))
            if b.source == "constant":
                sources_set += 1
            if sources_set != 1 or (
                    b.source == "external" and b.edge_id is not None) or (
                    b.source == "edge" and b.external_key is not None) or (
                    b.source == "constant" and
                    (b.external_key is not None or
                     b.edge_id is not None)):
                f.append(Finding("conflicting_binding_sources",
                                 n.node_id,
                                 f"{b.request_key}: source fields "
                                 "inconsistent with "
                                 f"source={b.source}"))
            carg = contract.arg(b.request_key) if contract else None
            if contract is not None and carg is None:
                f.append(Finding("unsupported_command_arg", n.node_id,
                                 f"{b.request_key} is not a "
                                 f"{n.command_key} argument"))
                continue
            if carg is not None:
                if carg.computed:
                    f.append(Finding("binding_to_computed_arg", n.node_id,
                                     f"{b.request_key} is "
                                     "engine-computed"))
                if carg.refused:
                    f.append(Finding("refused_arg", n.node_id,
                                     f"{b.request_key} is refused by "
                                     f"{n.command_key}"))
                if carg.binding_support == "unsupported":
                    f.append(Finding("unsupported_binding_target",
                                     n.node_id,
                                     f"{b.request_key} is not modeled "
                                     "for pipeline binding"))
                f.extend(_constraint_findings(
                    b.request_key, carg.ptype, b.constraints))
            bound_args[b.request_key] = b.source
            ep = param_by_key.get(b.external_key or "") \
                if b.source == "external" else None
            if b.source == "external":
                if ep is None:
                    f.append(Finding("missing_external_param", n.node_id,
                                     f"{b.request_key} -> "
                                     f"{b.external_key}"))
            elif b.source == "edge":
                e = edge_by_id.get(b.edge_id or "")
                if e is None:
                    f.append(Finding("conflicting_binding_sources",
                                     n.node_id,
                                     f"{b.request_key} references "
                                     f"missing edge {b.edge_id}"))
                else:
                    port = port_by_key.get((e.dst_node, e.dst_port))
                    if e.dst_node != n.node_id:
                        f.append(Finding("conflicting_binding_sources",
                                         n.node_id,
                                         f"{b.request_key}: edge "
                                         f"{b.edge_id} targets "
                                         f"{e.dst_node}"))
                    elif port is None or port.command_arg != b.request_key:
                        f.append(Finding("bad_command_arg", n.node_id,
                                         f"{b.request_key}: edge "
                                         f"{b.edge_id} does not feed "
                                         "this argument"))
            elif b.source == "constant":
                if carg is not None and \
                        not _ptype_ok(carg.ptype, b.constant):
                    f.append(Finding("constant_type_mismatch", n.node_id,
                                     f"{b.request_key} constant "
                                     f"{b.constant!r} is not "
                                     f"{carg.ptype}"))
                elif carg is not None and b.constraints and not \
                        _value_meets(carg.ptype, b.constant,
                                     b.constraints):
                    f.append(Finding("constraint_violation", n.node_id,
                                     f"{b.request_key} constant "
                                     f"{b.constant!r} violates declared "
                                     "constraints"))
                if not _deep_immutable(b.constant):
                    f.append(Finding("mutable_definition_field",
                                     n.node_id,
                                     f"{b.request_key} constant is "
                                     "mutable"))
            # Effective-domain intersection across ALL applicable
            # layers (F2): external-decl, binding-level and
            # contract-arg constraints combined — an empty domain
            # can never produce a legal bound value.
            domain_layers = [b.constraints]
            if carg is not None:
                domain_layers.append(carg.constraints)
            if ep is not None:
                domain_layers.append(ep.constraints)
            dom_ptype = carg.ptype if carg is not None else (
                ep.ptype if ep is not None else None)
            if dom_ptype is not None and \
                    _effective_domain_empty(dom_ptype, domain_layers):
                f.append(Finding("conflicting_constraints", n.node_id,
                                 f"{b.request_key}: no value satisfies "
                                 "the combined external, binding and "
                                 "command constraints"))
        # Pipeline single-output contract (D1): when the command
        # declares an output-count argument, the PIPELINE pins it to
        # exactly one integer constant 1. The real command's wider
        # range (generate.image batch 1–4) is preserved intact — the
        # restriction is scoped to pipeline declarations only.
        if contract is not None and contract.output_count_arg:
            oca = contract.output_count_arg
            pins = [b for b in n.param_bindings if b.request_key == oca]
            if len(pins) != 1 or pins[0].source != "constant" \
                    or not _ptype_ok("int", pins[0].constant) \
                    or pins[0].constant != 1:
                f.append(Finding("cardinality_pin_violation",
                                 n.node_id,
                                 f"{oca} must be bound exactly once, "
                                 "to integer constant 1 (single-output "
                                 "pipeline contract)"))
        # Mandatory + conditional argument coverage.
        if contract is not None:
            for a in contract.args:
                if a.required and a.name not in bound_args:
                    f.append(Finding("missing_mandatory_binding",
                                     n.node_id,
                                     f"required argument {a.name} "
                                     "has no binding"))
            for group in contract.conditional_groups:
                if not any(g in bound_args for g in group):
                    f.append(Finding("conditional_binding_missing",
                                     n.node_id,
                                     f"one of {group} must be bound"))

    # Edges.
    for e in defn.edges:
        if not _IDENT_RE.match(e.edge_id):
            f.append(Finding("bad_identifier", "edge", repr(e.edge_id)))
        if e.src_node == e.dst_node:
            f.append(Finding("self_edge", e.edge_id, e.src_node))
        src_n = node_by_id.get(e.src_node)
        dst_n = node_by_id.get(e.dst_node)
        if src_n is None:
            f.append(Finding("missing_src_node", e.edge_id, e.src_node))
        if dst_n is None:
            f.append(Finding("missing_dst_node", e.edge_id, e.dst_node))
        sp = port_by_key.get((e.src_node, e.src_port))
        dp = port_by_key.get((e.dst_node, e.dst_port))
        if src_n is not None and sp is None:
            f.append(Finding("missing_src_port", e.edge_id,
                             f"{e.src_node}.{e.src_port}"))
        if dst_n is not None and dp is None:
            f.append(Finding("missing_dst_port", e.edge_id,
                             f"{e.dst_node}.{e.dst_port}"))
        if sp is not None and sp.direction != "out":
            f.append(Finding("bad_direction", e.edge_id,
                             f"src port {e.src_port} is not an "
                             "output"))
        if dp is not None and dp.direction != "in":
            f.append(Finding("bad_direction", e.edge_id,
                             f"dst port {e.dst_port} is not an input"))
        if sp is not None and dp is not None and \
                sp.asset_kind != dp.asset_kind:
            f.append(Finding("kind_mismatch", e.edge_id,
                             f"{sp.asset_kind} -> "
                             f"{dp.asset_kind}"))
        if dp is not None and e.dst_command_arg != dp.command_arg:
            f.append(Finding("bad_command_arg", e.edge_id,
                             f"dst_command_arg {e.dst_command_arg} != "
                             f"port arg {dp.command_arg}"))
        if e.asset_role not in ASSET_ROLES:
            f.append(Finding("unsupported_asset_role", e.edge_id,
                             repr(e.asset_role)))
        if e.ownership not in OWNERSHIP_KINDS:
            f.append(Finding("unsupported_ownership", e.edge_id,
                             repr(e.ownership)))
        if e.provenance not in PROVENANCE_KINDS:
            f.append(Finding("unsupported_provenance", e.edge_id,
                             repr(e.provenance)))
        # Required pipeline input coverage (S2): a required in-port
        # must be fed by an edge — command-level optionality does not
        # relax the pipeline contract.
        if dp is not None and dp.direction == "in" and dp.required:
            pass  # satisfied below via per-port edge accounting
    fed_ports = {(e.dst_node, e.dst_port) for e in defn.edges
                 if (e.dst_node, e.dst_port) in port_by_key}
    for n in defn.nodes:
        for p in n.input_ports:
            if p.required and p.direction == "in" \
                    and (n.node_id, p.port_key) not in fed_ports:
                f.append(Finding("missing_pipeline_input", n.node_id,
                                 f"required in-port {p.port_key} has "
                                 "no edge"))

    # ---- Phase C: traversal — cycles, depth, connectivity ----
    adj: dict[str, list[str]] = {n.node_id: [] for n in defn.nodes}
    undirected: dict[str, set[str]] = {n.node_id: set()
                                       for n in defn.nodes}
    for e in defn.edges:
        if e.src_node in adj and e.dst_node in adj \
                and e.src_node != e.dst_node:
            adj[e.src_node].append(e.dst_node)
            undirected[e.src_node].add(e.dst_node)
            undirected[e.dst_node].add(e.src_node)

    in_deg = {k: 0 for k in adj}
    for v in adj.values():
        for d in v:
            in_deg[d] += 1
    queue = sorted(k for k, d in in_deg.items() if d == 0)
    topo: list[str] = []
    depth = {k: 1 for k in queue}
    while queue:
        cur = queue.pop(0)
        topo.append(cur)
        for d in adj[cur]:
            depth[d] = max(depth.get(d, 0), depth[cur] + 1)
            in_deg[d] -= 1
            if in_deg[d] == 0:
                queue.append(d)
                queue.sort()
    if len(topo) != len(adj):
        f.append(Finding("cycle", "graph",
                         "directed cycle among nodes "
                         + ",".join(sorted(set(adj) - set(topo)))))
    elif depth and max(depth.values()) > LIMITS["max_depth"]:
        f.append(Finding("limit_exceeded", "graph",
                         f"depth {max(depth.values())} > "
                         f"{LIMITS['max_depth']}"))

    for n in defn.nodes:
        if not undirected[n.node_id]:
            f.append(Finding("node_isolated", n.node_id,
                             "no incident edges"))
    if len(adj) > 1:
        seen: set[str] = set()
        stack = [next(iter(adj))]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(undirected[cur] - seen)
        if len(seen) != len(adj):
            f.append(Finding("disconnected_graph", "graph",
                             "weak connectivity required — "
                             "independent chains are invalid"))

    return ("valid" if not f else "invalid"), \
        sorted(f, key=lambda x: (x.code, x.subject))


def concurrency_pairs(defn: PipelineDefinition) -> list[list[str]]:
    """Potentially-concurrent node pairs — pairs with no directed
    dependency path in EITHER direction. Potential, never guaranteed:
    actual concurrency is the execution-time scheduler's authority."""
    adj: dict[str, set[str]] = {n.node_id: set() for n in defn.nodes}
    for e in defn.edges:
        if e.src_node in adj and e.dst_node in adj \
                and e.src_node != e.dst_node:
            adj[e.src_node].add(e.dst_node)

    def reaches(a: str, b: str) -> bool:
        stack, seen = [a], set()
        while stack:
            cur = stack.pop()
            if cur == b:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(adj.get(cur, ()))
        return False

    ids = sorted(adj)
    out = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if not reaches(a, b) and not reaches(b, a):
                out.append([a, b])
    return out


# ------------------------------------------------------------------
# Static classification (safeguard S5) — derived, never declared.
# ------------------------------------------------------------------


def _execution_class(node: PipelineNode, ctx: EvidenceContext,
                     wf: WorkflowEvidence | None) -> str:
    """missing > held > simulated > experimental > implemented."""
    aid = ctx.adapter_id(node.command_key)
    if aid is None or ctx.adapter_kind(aid) is None:
        return "missing"
    if wf is not None and wf.status == "held":
        return "held"
    if ctx.adapter_kind(aid) == "mock":
        return "simulated"
    if "EXPERIMENTAL" in ctx.adapter_notes(aid):
        return "experimental"
    return "implemented"


def _policy_state(node: PipelineNode,
                  wf: WorkflowEvidence | None) -> str:
    """Static policy evidence — explanatory only, never authorization.
    'none_declared' is the honest zero-state (never 'clear')."""
    if wf is not None:
        if wf.status == "held" or wf.policy == "lora_hold":
            return "held"
        if wf.policy not in ("", "none"):
            return "restricted"          # cloud_gated / scheduled etc.
    if node.policy_notes:
        return "notes_only"
    return "none_declared"


def _adapter_class(ctx: EvidenceContext, adapter_id: str | None) -> str:
    return ctx.adapter_kind(adapter_id) or "unknown"


def describe_node(node: PipelineNode,
                  ctx: EvidenceContext) -> dict[str, Any]:
    wf = ctx.workflow(node.workflow_key)
    aid = ctx.adapter_id(node.command_key)
    return {
        "node_id": node.node_id,
        "node_kind": node.node_kind,
        "command_key": node.command_key,
        "adapter_id": aid,
        "workflow_key": node.workflow_key,
        "runtime_keys": list(node.runtime_keys),
        "runtime_declared": {k: ctx.runtime_state(k)
                             for k in node.runtime_keys},
        "required_components": list(node.required_components),
        "input_ports": [_port_out(p) for p in node.input_ports],
        "output_ports": [_port_out(p) for p in node.output_ports],
        "param_bindings": [_binding_out(b) for b in node.param_bindings],
        "policy_notes": list(node.policy_notes),
        # Derived static axes — independent, never merged.
        "command_contract": ("exists" if node.command_key
                             in COMMAND_CONTRACTS else "unknown"),
        "adapter_class": _adapter_class(ctx, aid),
        "execution_class": _execution_class(node, ctx, wf),
        "policy_state": _policy_state(node, wf),
        "operational_readiness": "not_evaluated",
    }


def _port_out(p: PortDecl) -> dict[str, Any]:
    return {"port_key": p.port_key, "direction": p.direction,
            "asset_kind": p.asset_kind, "required": p.required,
            "cardinality": p.cardinality,
            "command_arg": p.command_arg,
            "output_contract": p.output_contract}


def _json_safe(v: Any) -> Any:
    """Non-finite floats -> None in describe output — invalid
    definitions can carry NaN/inf and must never leak them into API
    JSON (the validation finding still reports the repr)."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, tuple):
        return [_json_safe(x) for x in v]
    return v


def _constraint_out(c: Constraint) -> dict[str, Any]:
    return {"kind": c.kind, "value": _json_safe(c.value)}


def _binding_out(b: ParamBinding) -> dict[str, Any]:
    return {"request_key": b.request_key, "source": b.source,
            "external_key": b.external_key, "edge_id": b.edge_id,
            "constant": _json_safe(b.constant),
            "constraints": [_constraint_out(c)
                            for c in b.constraints]}


def _param_out(p: ParamDecl) -> dict[str, Any]:
    return {"param_key": p.param_key, "ptype": p.ptype,
            "required": p.required, "has_default": p.has_default,
            "default": _json_safe(p.default),
            "description": p.description,
            "constraints": [_constraint_out(c)
                            for c in p.constraints]}


def _edge_out(e: PipelineEdge) -> dict[str, Any]:
    return {"edge_id": e.edge_id, "src_node": e.src_node,
            "src_port": e.src_port, "dst_node": e.dst_node,
            "dst_port": e.dst_port,
            "dst_command_arg": e.dst_command_arg,
            "asset_role": e.asset_role, "ownership": e.ownership,
            "provenance": e.provenance}


def _static_assessment(defn: PipelineDefinition,
                       ctx: EvidenceContext) -> str:
    """Graph-level static capability label — descriptive only."""
    classes = []
    for n in defn.nodes:
        classes.append(_execution_class(n, ctx,
                                        ctx.workflow(n.workflow_key)))
    for worst in ("missing", "held", "simulated", "experimental"):
        if worst in classes:
            return worst
    if all(c == "implemented" for c in classes):
        return "implemented"
    return "mixed"


# ------------------------------------------------------------------
# Fingerprints (Gate 7)
# ------------------------------------------------------------------


def _canon(v) -> Any:
    """Deterministic canonicalization; declaration order is preserved
    where ordering is semantically significant (nodes, edges, ports,
    bindings) — only key-order inside dicts is normalized. Non-finite
    floats serialize as their repr string so the canonical JSON stays
    strict and deterministic even for invalid definitions (D2)."""
    if isinstance(v, float) and not math.isfinite(v):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, dict):
        return {k: _canon(v[k]) for k in sorted(v)}
    if v.__class__.__module__ == __name__ and hasattr(v, "__dataclass_fields__"):
        return {k: _canon(getattr(v, k))
                for k in v.__dataclass_fields__}
    return v


def _hash16(payload: Any) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def definition_fingerprint(defn: PipelineDefinition) -> str:
    """Stable identity over ALL material static fields. display_name and
    description are presentation-only and explicitly excluded; nothing
    else is excluded — no timestamps, hardware, project rows or
    operational claims can ever enter this fingerprint."""
    payload = {
        "ruleset": [PIPELINE_SCHEMA_VERSION, PIPELINE_DEFS_VERSION],
        "pipeline_key": defn.pipeline_key,
        "schema_version": defn.schema_version,
        "definition_version": defn.definition_version,
        "category": defn.category,
        "status": defn.status,
        "required_context": defn.required_context,
        "policy_constraints": defn.policy_constraints,
        "limitations": defn.limitations,
        "external_params": [_canon(p) for p in defn.external_params],
        "nodes": [_canon(n) for n in defn.nodes],
        "edges": [_canon(e) for e in defn.edges],
    }
    return "pipe:v1:" + _hash16(payload)


def _wf_evidence_out(w: WorkflowEvidence | None) -> dict | None:
    if w is None:
        return None
    return {"status": w.status, "policy": w.policy,
            "required_components": w.required_components,
            "optional_components": w.optional_components}


def contract_evidence_fingerprint(defn: PipelineDefinition,
                                  ctx: EvidenceContext) -> str:
    """Identity over the resolved STATIC evidence used for adapter/
    runtime/capability/policy classification — command-table mappings,
    adapter kinds/notes, workflow status/policy/components, runtime
    declaration state, and the audited contract version. Drift in any
    of those flips this fingerprint while the definition fingerprint
    stays put."""
    payload = {
        "ruleset": [COMMAND_CONTRACTS_VERSION, PIPELINE_DEFS_VERSION],
        "nodes": [
            {"node_id": n.node_id,
             "command_key": n.command_key,
             "adapter_id": ctx.adapter_id(n.command_key),
             "adapter_kind": ctx.adapter_kind(
                 ctx.adapter_id(n.command_key)),
             "adapter_notes": ctx.adapter_notes(
                 ctx.adapter_id(n.command_key)),
             "runtime_state": {k: ctx.runtime_state(k)
                               for k in n.runtime_keys},
             "workflow": _wf_evidence_out(ctx.workflow(n.workflow_key)),
             "contract": _canon(COMMAND_CONTRACTS.get(n.command_key)),
             }
            for n in defn.nodes
        ],
    }
    return "pipectx:v1:" + _hash16(payload)


# ------------------------------------------------------------------
# Describe surface (read-only API payload construction)
# ------------------------------------------------------------------


def describe_pipeline(defn: PipelineDefinition,
                      ctx: EvidenceContext) -> dict[str, Any]:
    structural, findings = validate_pipeline(defn, ctx)
    # Registry-level errors stay authoritative (F1): a duplicate or
    # over-limit registry must surface in describe output and flip the
    # structural verdict — never masked by first-wins deduplication.
    reg = _applicable_registry_findings(defn.pipeline_key)
    if reg:
        findings = reg + list(findings)
        structural = "invalid"
    return {
        "pipeline_key": defn.pipeline_key,
        "display_name": defn.display_name,
        "description": defn.description,
        "category": defn.category,
        "status": defn.status,
        "schema_version": defn.schema_version,
        "definition_version": defn.definition_version,
        "required_context": list(defn.required_context),
        "policy_constraints": list(defn.policy_constraints),
        "limitations": list(defn.limitations),
        "external_params": [_param_out(p)
                            for p in defn.external_params],
        "nodes": [describe_node(n, ctx) for n in defn.nodes],
        "edges": [_edge_out(e) for e in defn.edges],
        "validation": {
            "structural": structural,
            "findings": [{"code": fd.code, "subject": fd.subject,
                          "severity": fd.severity, "detail": fd.detail}
                         for fd in findings],
        },
        # Concurrency analysis runs ONLY on valid bounded graphs (D7):
        # invalid or over-limit definitions return findings and make
        # no concurrency claims.
        "concurrency_pairs": (concurrency_pairs(defn)
                              if structural == "valid" else []),
        "static_assessment": _static_assessment(defn, ctx),
        "operational_readiness": "not_evaluated",
        "definition_fingerprint": definition_fingerprint(defn),
        "contract_evidence_fingerprint":
            contract_evidence_fingerprint(defn, ctx),
    }


def pipeline_summary(defn: PipelineDefinition,
                     ctx: EvidenceContext) -> dict[str, Any]:
    structural, _ = validate_pipeline(defn, ctx)
    # Same F1 propagation on the list surface — a retained first-wins
    # duplicate is never advertised as structurally valid.
    if _applicable_registry_findings(defn.pipeline_key):
        structural = "invalid"
    return {
        "pipeline_key": defn.pipeline_key,
        "display_name": defn.display_name,
        "category": defn.category,
        "status": defn.status,
        "node_count": len(defn.nodes),
        "edge_count": len(defn.edges),
        "structural": structural,
        "definition_fingerprint": definition_fingerprint(defn),
    }


# ------------------------------------------------------------------
# Registry — the single initial template (T1), verified against the
# real command contracts in commands.py (see COMMAND_CONTRACTS notes).
# ------------------------------------------------------------------


IMG2VID_CHAIN = PipelineDefinition(
    pipeline_key="img2vid_chain",
    schema_version=PIPELINE_SCHEMA_VERSION,
    definition_version=1,
    display_name="Image to video chain",
    description=("Generate one image, then drive image-to-video from "
                 "that image. Two whole-job nodes linked by a typed "
                 "image asset edge."),
    category="generation_chain",
    status="declared",
    required_context=("project",),
    policy_constraints=("lora_holds_apply",
                        "experimental_models_gate_apply"),
    limitations=(
        "single_image_output",
        "no_fan_out",
        "advisory_only",
        ("component_requirements_are_workflow_minimums — "
         "model-family extras resolve at plan time")),
    external_params=(
        ParamDecl("img_model_key", "string", required=True,
                  description="catalog key of the image model"),
        ParamDecl("img_prompt", "string", required=True,
                  description="image-generation prompt"),
        ParamDecl("vid_model_key", "string", required=True,
                  description="catalog key of the video model"),
        ParamDecl("vid_prompt", "string", required=False,
                  description="optional motion prompt for the video "
                              "stage"),
        ParamDecl("width", "int", required=False,
                  has_default=True, default=512,
                  constraints=(Constraint("min", 1),
                               Constraint("max", 4096)),
                  description="image width"),
        ParamDecl("height", "int", required=False,
                  has_default=True, default=512,
                  constraints=(Constraint("min", 1),
                               Constraint("max", 4096)),
                  description="image height"),
        ParamDecl("frames", "int", required=False,
                  has_default=True, default=33,
                  description="video frame count — catalog bounds "
                              "apply at execution"),
        ParamDecl("fps", "int", required=False,
                  has_default=True, default=24,
                  description="video frame rate"),
    ),
    nodes=(
        PipelineNode(
            node_id="img",
            node_kind="job",
            command_key="generate.image",
            workflow_key="text2image",
            runtime_keys=("sdcpp",),
            required_components=("diffusion",),
            input_ports=(),
            output_ports=(
                PortDecl("result", "out", "image", cardinality=1,
                         output_contract="primary_asset"),
            ),
            param_bindings=(
                ParamBinding("model_key", "external",
                             external_key="img_model_key"),
                ParamBinding("prompt", "external",
                             external_key="img_prompt"),
                ParamBinding("width", "external",
                             external_key="width"),
                ParamBinding("height", "external",
                             external_key="height"),
                # Fixed, non-overridable — the single-output contract.
                ParamBinding("batch", "constant", constant=1),
            ),
            policy_notes=("lora_selections_subject_to_existing_holds",),
        ),
        PipelineNode(
            node_id="vid",
            node_kind="job",
            command_key="generate.video",
            workflow_key="image2video",
            runtime_keys=("sdcpp",),
            required_components=("diffusion", "vae", "text_encoder"),
            input_ports=(
                # Pipeline-REQUIRED even though the command accepts
                # prompt-only — removing the edge invalidates the
                # template rather than degrading it to text2video.
                PortDecl("init_image", "in", "image", required=True,
                         cardinality=1, command_arg="image_asset_id"),
            ),
            output_ports=(
                PortDecl("result", "out", "video", cardinality=1,
                         output_contract="primary_asset"),
            ),
            param_bindings=(
                ParamBinding("model_key", "external",
                             external_key="vid_model_key"),
                ParamBinding("prompt", "external",
                             external_key="vid_prompt"),
                ParamBinding("image_asset_id", "edge",
                             edge_id="img_to_vid_init"),
                ParamBinding("frames", "external",
                             external_key="frames"),
                ParamBinding("fps", "external",
                             external_key="fps"),
            ),
            policy_notes=(),
        ),
    ),
    edges=(
        PipelineEdge(
            edge_id="img_to_vid_init",
            src_node="img", src_port="result",
            dst_node="vid", dst_port="init_image",
            dst_command_arg="image_asset_id",
            asset_role="init_image",
            ownership="same_project",
            provenance="generated",
        ),
    ),
)

# Ordered declaration source (D6) — duplicates are PRESERVED here so
# pipeline_registry can flag them before any deduplication. The dict
# below is a lookup convenience built after validation, first-wins.
_PIPELINE_DECLS: tuple[PipelineDefinition, ...] = (IMG2VID_CHAIN,)


def _build_registry(
        decls: tuple[PipelineDefinition, ...]
) -> dict[str, PipelineDefinition]:
    out: dict[str, PipelineDefinition] = {}
    for d in decls:
        out.setdefault(d.pipeline_key, d)
    return out


PIPELINE_DEFS: dict[str, PipelineDefinition] = \
    _build_registry(_PIPELINE_DECLS)


def get_pipeline(key: str | None) -> PipelineDefinition | None:
    if not key:
        return None
    return PIPELINE_DEFS.get(key)


def _registry_level_findings(
        decls: tuple[PipelineDefinition, ...] | None = None
) -> list[Finding]:
    """Registry-scope findings over the ORDERED declaration source —
    count bound and duplicate keys flagged BEFORE any deduplication."""
    decls = _PIPELINE_DECLS if decls is None else tuple(decls)
    out: list[Finding] = []
    if len(decls) > LIMITS["max_pipelines"]:
        out.append(Finding("limit_exceeded", "registry",
                           f"pipeline declarations {len(decls)} > "
                           f"{LIMITS['max_pipelines']}"))
    keys = [d.pipeline_key for d in decls]
    for k in sorted({k for k in keys if keys.count(k) > 1}):
        # Subject is the affected KEY (not 'registry') so the finding
        # applies only to that definition's surfaces (F1).
        out.append(Finding("duplicate_pipeline_key", k,
                           "pipeline_key declared more than once"))
    return out


def _applicable_registry_findings(
        pipeline_key: str,
        decls: tuple[PipelineDefinition, ...] | None = None
) -> list[Finding]:
    """Registry findings that apply to ONE definition — global errors
    (subject 'registry') plus this key's duplicates (F1)."""
    return [fd for fd in _registry_level_findings(decls)
            if fd.subject in ("registry", pipeline_key)]


def pipeline_registry(
        ctx: EvidenceContext,
        declarations: tuple[PipelineDefinition, ...] | None = None
) -> tuple[str, list[Finding]]:
    """Registry-level checks over the ORDERED declaration source:
    count bound and duplicate keys flagged BEFORE any deduplication —
    a duplicate declaration is a finding, never a silent drop."""
    decls = _PIPELINE_DECLS if declarations is None \
        else tuple(declarations)
    f: list[Finding] = _registry_level_findings(decls)
    for d in decls:
        _, findings = validate_pipeline(d, ctx)
        f.extend(findings)
    return ("valid" if not f else "invalid"), \
        sorted(f, key=lambda x: (x.code, x.subject))
