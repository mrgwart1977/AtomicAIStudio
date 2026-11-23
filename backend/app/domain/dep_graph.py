"""U-5b — dependency requirement model and bounded graph.

A dependency plan is a per-resolution EPHEMERAL DAG of requirement
slots — never a persisted global graph, never an execution plan.
Nodes are requirement slots (a role that must be filled); fills are
edges from a slot to a real `model_artifacts` row, optionally narrowed
to one declared manifest member (`component_key`).

Component identity (C1): a manifest entry is a component FILE inside
its parent artifact — identified by `artifact_id::member_key`, where
member_key is the declared `file_name`, disambiguated by the declared
sha256 when a bundle repeats a name. Manifest order never affects
identity: entries are canonicalized (sorted, deduped) before any
evaluation. When declared metadata cannot distinguish two entries the
resolver reports `dep_evidence_conflict` — it never fabricates a key
and never silently collapses members.

No filesystem access, no hashing, no writes — pure stored evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

DEP_RULESET_VERSION = 1

# --- frozen component-role vocabulary (additive-only) -------------
DIFFUSION = "diffusion"
VAE = "vae"
TEXT_ENCODER = "text_encoder"
TOKENIZER = "tokenizer"
IMAGE_ENCODER = "image_encoder"
MOTION_MODULE = "motion_module"
AUDIO_ENCODER = "audio_encoder"
AUDIO_DECODER = "audio_decoder"
CONTROL = "control"
BASE_MODEL = "base_model"               # lora_character workflow role —
                                        # filled by the root model itself
LORA = "lora"                           # generic optional adapter slot
LORA_IDENTITY = "lora_identity"
LORA_STYLE = "lora_style"

COMPONENT_ROLES: tuple[str, ...] = (
    DIFFUSION, VAE, TEXT_ENCODER, TOKENIZER, IMAGE_ENCODER,
    MOTION_MODULE, AUDIO_ENCODER, AUDIO_DECODER, CONTROL,
    BASE_MODEL, LORA, LORA_IDENTITY, LORA_STYLE,
)

# --- requirement sources -------------------------------------------
SRC_WORKFLOW = "workflow_def"
SRC_FAMILY = "family_spec"
SRC_CAP = "family_cap"
SRC_CAST = "cast_binding"
SRC_REQUEST = "request"

# --- provenance (where a fill came from — NOT confidence) ----------
PROV_EXPLICIT = "explicit"
PROV_BUNDLE = "bundle"
PROV_EXTERNAL = "external"
PROV_SUGGESTED = "suggested"

_PROV_ORDER = {PROV_EXPLICIT: 0, PROV_BUNDLE: 1,
               PROV_EXTERNAL: 2, PROV_SUGGESTED: 3}

# --- slot outcomes ---------------------------------------------------
SLOT_RESOLVED = "resolved"
SLOT_AMBIGUOUS = "ambiguous"
SLOT_MISSING = "missing"
SLOT_NO_FILL_EVIDENCE = "no_fill_evidence"
SLOT_CONFLICTED = "conflicted"
SLOT_SELECTOR_REFUSED = "selector_refused"
SLOT_LIMIT_EXCEEDED = "dep_limit_exceeded"
SLOT_NEEDS_SHOT_CTX = "needs_shot_context"

# --- limit / conflict codes (distinct, never conflated) --------------
DEP_CYCLE = "dep_cycle"
DEP_MAX_DEPTH = "dep_max_depth"
DEP_MAX_NODES = "dep_max_nodes"
DEP_MAX_EDGES = "dep_max_edges"
DEP_MAX_CANDIDATES = "dep_max_candidates"
DEP_MAX_CHARACTER_SLOTS = "dep_max_character_slots"

DEP_FAMILY_CONFLICT = "dep_family_conflict"
DEP_VERSION_CONFLICT = "dep_version_conflict"
DEP_SELECTOR_CONFLICT = "dep_selector_conflict"
DEP_EXCLUSIVE_VIOLATION = "dep_exclusive_violation"
DEP_EVIDENCE_CONFLICT = "dep_evidence_conflict"

# --- declared bounds (echoed in responses; hit => explicit outcome) --
MAX_REQUIREMENTS = 32
MAX_EDGES = 128
MAX_DEPTH = 4
MAX_CANDIDATES_PER_SLOT = 64
MAX_CHARACTER_SLOTS = 8
MAX_SELECTORS = 32

LIMITS = {
    "max_requirements": MAX_REQUIREMENTS,
    "max_edges": MAX_EDGES,
    "max_depth": MAX_DEPTH,
    "max_candidates_per_slot": MAX_CANDIDATES_PER_SLOT,
    "max_character_slots": MAX_CHARACTER_SLOTS,
    "max_selectors": MAX_SELECTORS,
}


@dataclass(frozen=True)
class ManifestEntry:
    """One declared member of a bundle manifest — a component FILE,
    never a ModelArtifact row."""
    file_name: str
    role: str
    sha256: str = ""
    size_bytes: int = 0


@dataclass(frozen=True)
class MemberKeys:
    """member_key per manifest index + unresolvable collisions."""
    keys: tuple[str, ...]                       # parallel to manifest
    conflicts: tuple[int, ...]                  # indices that cannot be
    # distinguished by declared metadata (dep_evidence_conflict)


def canonical_manifest(files) -> tuple[ManifestEntry, ...]:
    """Deterministic manifest view: dedupe identical declared members
    (same role+name+sha IS the same member) and sort — stored order is
    irrelevant to identity."""
    seen: set[tuple[str, str, str]] = set()
    out: list[ManifestEntry] = []
    for f in files or ():
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "")
        role = str(f.get("role") or "")
        sha = str(f.get("sha256") or "")
        size = int(f.get("size_bytes") or 0)
        if not name:
            continue
        key = (role, name, sha)
        if key in seen:
            continue
        seen.add(key)
        out.append(ManifestEntry(name, role, sha, size))
    return tuple(sorted(out, key=lambda e: (e.role, e.file_name,
                                            e.sha256)))


def member_keys(manifest: tuple[ManifestEntry, ...]) -> MemberKeys:
    """Stable member_key per entry: bare file_name when unique; a
    `name#sha12` disambiguator when a name repeats. Entries whose
    declared metadata cannot be distinguished (same name AND same sha
    prefix — i.e. no discriminating evidence) are conflict indices."""
    names: dict[str, int] = {}
    for e in manifest:
        names[e.file_name] = names.get(e.file_name, 0) + 1
    keys: list[str] = []
    for e in manifest:
        if names[e.file_name] == 1:
            keys.append(e.file_name)
        else:
            keys.append(f"{e.file_name}#{e.sha256[:12] or 'nosha'}")
    conflicts: list[int] = []
    seen_keys: dict[str, int] = {}
    for i, k in enumerate(keys):
        if k in seen_keys:
            conflicts.append(i)
            conflicts.append(seen_keys[k])
        else:
            seen_keys[k] = i
    return MemberKeys(keys=tuple(keys),
                      conflicts=tuple(sorted(set(conflicts))))


def component_key(artifact_id: str, member_key: str | None = None) -> str:
    """C1 — stable component identity. Whole-artifact components use
    the artifact_id itself; manifest members address inside the parent."""
    if not member_key:
        return artifact_id
    return f"{artifact_id}::{member_key}"


@dataclass(frozen=True)
class Requirement:
    """One requirement slot in the ephemeral plan DAG."""
    requirement_id: str                 # deterministic slot identity
    role: str
    required: bool                      # required vs optional/advisory
    source: str                         # SRC_* — why the slot exists
    family_id: str = ""                 # required artifact family
    base_family: str = ""               # adapters: required base family
    fmts_accepted: tuple[str, ...] = ()
    binds_character_id: str = ""
    selected_artifact_id: str = ""      # explicit/cast selector pin
    exclusive: bool = True              # single-fill slot
    depth: int = 0                      # plan-graph depth


@dataclass(frozen=True)
class GraphView:
    """The bounded requirement graph."""
    nodes: tuple[Requirement, ...]
    edges: tuple[tuple[str, str], ...]  # (parent_req_id, child_req_id)
    limits: tuple[str, ...]             # dep_max_* codes hit
    cycle: tuple[str, ...]              # requirement_id cycle path, ()
    depth_exceeded: bool


def _req_id(parts: list[str]) -> str:
    return ":".join(p for p in parts if p)


def build_graph(requirements: list[Requirement],
                sub_reqs: dict[str, list[Requirement]] | None = None
                ) -> GraphView:
    """Assemble the bounded DAG: top-level requirements plus declared
    sub-requirements (a filled artifact's own family component_roles),
    edges parent→child. Detects real cycles (back-edge on the ancestor
    path), depth exceedance, and node/edge limits separately."""
    nodes = list(requirements)
    edges: list[tuple[str, str]] = []
    limits: list[str] = []
    sub_reqs = sub_reqs or {}
    for parent, children in sub_reqs.items():
        for c in children:
            nodes.append(c)
            edges.append((parent, c.requirement_id))

    if len(nodes) > MAX_REQUIREMENTS:
        limits.append(DEP_MAX_NODES)
    if len(edges) > MAX_EDGES:
        limits.append(DEP_MAX_EDGES)

    children_map: dict[str, list[str]] = {}
    for a, b in edges:
        children_map.setdefault(a, []).append(b)

    cycle: list[str] = []
    depth_exceeded = False

    def dfs(node: str, path: list[str]) -> None:
        nonlocal cycle, depth_exceeded
        if node in path:
            if not cycle:
                cycle = path[path.index(node):] + [node]
            return
        if len(path) >= MAX_DEPTH:
            depth_exceeded = True
            return
        for nxt in children_map.get(node, ()):
            dfs(nxt, [*path, node])

    for req in requirements:
        dfs(req.requirement_id, [])
        if cycle:
            break

    return GraphView(
        nodes=tuple(nodes), edges=tuple(edges),
        limits=tuple(limits),
        cycle=tuple(cycle), depth_exceeded=depth_exceeded)


__all__ = [
    "AUDIO_DECODER",
    "AUDIO_ENCODER",
    "BASE_MODEL",
    "COMPONENT_ROLES",
    "CONTROL",
    "DEP_CYCLE",
    "DEP_EVIDENCE_CONFLICT",
    "DEP_EXCLUSIVE_VIOLATION",
    "DEP_FAMILY_CONFLICT",
    "DEP_MAX_CANDIDATES",
    "DEP_MAX_CHARACTER_SLOTS",
    "DEP_MAX_DEPTH",
    "DEP_MAX_EDGES",
    "DEP_MAX_NODES",
    "DEP_RULESET_VERSION",
    "DEP_SELECTOR_CONFLICT",
    "DEP_VERSION_CONFLICT",
    "DIFFUSION",
    "IMAGE_ENCODER",
    "LIMITS",
    "LORA",
    "LORA_IDENTITY",
    "LORA_STYLE",
    "MAX_CANDIDATES_PER_SLOT",
    "MAX_CHARACTER_SLOTS",
    "MAX_DEPTH",
    "MAX_EDGES",
    "MAX_REQUIREMENTS",
    "MAX_SELECTORS",
    "MOTION_MODULE",
    "PROV_BUNDLE",
    "PROV_EXPLICIT",
    "PROV_EXTERNAL",
    "PROV_SUGGESTED",
    "SLOT_AMBIGUOUS",
    "SLOT_CONFLICTED",
    "SLOT_LIMIT_EXCEEDED",
    "SLOT_MISSING",
    "SLOT_NEEDS_SHOT_CTX",
    "SLOT_NO_FILL_EVIDENCE",
    "SLOT_RESOLVED",
    "SLOT_SELECTOR_REFUSED",
    "SRC_CAP",
    "SRC_CAST",
    "SRC_FAMILY",
    "SRC_REQUEST",
    "SRC_WORKFLOW",
    "TEXT_ENCODER",
    "TOKENIZER",
    "VAE",
    "GraphView",
    "ManifestEntry",
    "MemberKeys",
    "Requirement",
    "build_graph",
    "canonical_manifest",
    "component_key",
    "member_keys",
]
