"""B-3c — shared, side-effect-free LoRA resolution.

The B-1 admission gate (`check_loras`), its shot-cast membership helper,
and the prompt-syntax guard were extracted from `commands.py` unchanged
so the read-only resolution preview enforces the SAME code path the
generation compose/request-builder/validate sites run — there is no
second eligibility policy.

Everything here is SELECT + filesystem stat only: no rows are written,
no job is queued, no inference subprocess starts, nothing is staged or
downloaded, and no credential or provider is touched. A preview result
is advisory — it documents what the resolver would accept NOW; a later
generation re-validates against the then-current state.
"""
from __future__ import annotations

import math
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ModelInstall, Project, Shot, ShotCharacter
from app.domain import cast_state, lora_tags, model_catalog

LORA_MAX_STRENGTH = 2.0
LORA_MAX_PER_JOB = 2
# Per-adapter admission overhead added to est_vram — resident weights +
# scratch for at_runtime application. ESTIMATE, refined by real-run data.
LORA_VRAM_OVERHEAD_BYTES = 768 * 1024 * 1024


def shot_cast_ids(session: Session, shot) -> set[str]:
    """C4-B — a shot's effective cast: its own shot_characters UNION
    the scene-level cast it inherits (mirrors the storyboard payload's
    cast + inherited lists)."""
    from app.db.models import SceneCharacter
    own = {r.character_id for r in session.execute(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
    ).scalars()}
    inherited = {r.character_id for r in session.execute(
        select(SceneCharacter).where(SceneCharacter.scene_id == shot.scene_id)
    ).scalars()}
    return own | inherited


def check_loras(session: Session, project: Project, loras_in,
                base_spec, *, shot=None) -> list[dict]:
    """G2-B/B-1 — validate + pin structured ``loras`` selections.

    Returns the pinned entries the durable request carries: every field
    needed to re-verify and stage at worker start (install row, sha256,
    deterministic staged name). Refuses — never silently strips — on any
    violation. ``shot=None`` means no cast context: identity adapters
    then refuse outright (there is nothing to bind them to)."""
    from app.domain.commands import CommandError

    if not loras_in:
        return []
    if not isinstance(loras_in, list) or len(loras_in) > LORA_MAX_PER_JOB:
        raise CommandError(
            422, "loras accepts at most 2 adapters — one identity plus "
                 "one style")
    if base_spec is None or base_spec.family != "sd1.5":
        raise CommandError(
            422, "LoRA adapters are enabled for SD 1.5 image models only")
    # Base-model provenance pin: the catalog sha when pinned, else the
    # installed row's recorded sha. Traceable, immutable request state —
    # the worker's existing resolve path enforces integrity at run time.
    base_row = session.execute(
        select(ModelInstall).where(
            ModelInstall.model_key == base_spec.key,
            ModelInstall.status == "installed")).scalars().first()
    base_sha = (base_spec.files[0].sha256 if base_spec.files else None) \
        or (base_row.sha256 if base_row else "") or ""

    seen_keys: set[str] = set()
    seen_sha: set[str] = set()
    n_identity = 0
    out: list[dict] = []
    cast_ids = shot_cast_ids(session, shot) if shot is not None else set()
    for i, raw in enumerate(loras_in):
        entry = {"key": raw} if isinstance(raw, str) \
            else dict(raw) if isinstance(raw, dict) else None
        if entry is None:
            raise CommandError(
                422, f"loras[{i}] must be a model key or a "
                     "{'key', 'strength'} object")
        key = (entry.get("key") or "")
        if not isinstance(key, str) or not key.strip():
            raise CommandError(422, f"loras[{i}].key is required")
        key = key.strip()
        if key in seen_keys:
            raise CommandError(422, f"loras[{i}]: duplicate adapter '{key}'")
        seen_keys.add(key)
        spec = model_catalog.get(key)
        if spec is None or spec.modality != "lora":
            raise CommandError(
                422, f"unknown LoRA '{key}' — pick an installed adapter")
        if spec.support not in ("production", "tested"):
            raise CommandError(
                422, f"{spec.display_name} is {spec.support} — "
                     "catalog-visible but not usable yet")
        if not spec.license.get("commercial_ok", True):
            raise CommandError(
                422, f"{spec.display_name} has a restricted license and "
                     "cannot be used")
        if spec.family != base_spec.family:
            raise CommandError(
                422, f"{spec.display_name} targets '{spec.family}' base "
                     f"models — incompatible with '{base_spec.family}'")
        if spec.fmt != "safetensors":
            raise CommandError(
                422, f"{spec.display_name} is {spec.fmt} — only "
                     "safetensors LoRAs are verified on this runtime")
        role = (spec.capabilities or {}).get("lora_role")
        if role not in ("identity", "style"):
            raise CommandError(
                422, f"{spec.display_name} declares no lora_role — "
                     "unclassified adapters refuse")
        row = session.execute(
            select(ModelInstall).where(
                ModelInstall.model_key == key,
                ModelInstall.status == "installed")).scalars().first()
        if row is None:
            raise CommandError(
                422, f"{spec.display_name} is not installed — install "
                     "it from the Model Manager first")
        src = (Path(row.install_root) / row.rel_dir / row.file_name
               if row.file_name else None)
        if src is None or not src.is_file():
            raise CommandError(
                422, f"{spec.display_name} install is incomplete — "
                     "reinstall it")
        sha = (spec.files[0].sha256 if spec.files else None) \
            or (row.sha256 or "")
        if not sha:
            raise CommandError(
                422, f"{spec.display_name} has no verifiable sha256 — "
                     "the adapter cannot be pinned")
        if sha in seen_sha:
            raise CommandError(
                422, f"loras[{i}]: same weights as another selection — "
                     "duplicates are refused")
        seen_sha.add(sha)
        s = entry.get("strength")
        if s is None:
            strength = 1.0
        else:
            if isinstance(s, bool) or not isinstance(s, (int, float)):
                raise CommandError(
                    422, f"loras[{i}].strength must be a number")
            strength = float(s)
            if not math.isfinite(strength) \
                    or strength <= 0 or strength > LORA_MAX_STRENGTH:
                raise CommandError(
                    422, f"loras[{i}].strength must be a finite number "
                         "in (0, 2.0]")
        bound_char = None
        if role == "identity":
            n_identity += 1
            if n_identity > 1:
                raise CommandError(
                    422, "at most one identity LoRA per generation — "
                         "multi-character blending is not supported")
            if shot is not None:
                for r in session.execute(
                        select(ShotCharacter).where(
                            ShotCharacter.shot_id == shot.id)).scalars():
                    if r.character_id in cast_ids and key in \
                            cast_state.lora_keys(r.lora_json):
                        bound_char = r.character_id
                        break
            if bound_char is None:
                raise CommandError(
                    422, f"identity LoRA '{key}' is not bound to a "
                         "character in this shot's cast — bind it via "
                         "shot.cast first")
        else:
            linked = any(
                key in cast_state.lora_keys(r.lora_json)
                for r in session.execute(
                    select(ShotCharacter).where(
                        ShotCharacter.project_id == project.id)).scalars())
            if not linked:
                raise CommandError(
                    422, f"style LoRA '{key}' is not linked to this "
                         "project — bind it via shot.cast first")
        staged_name = (f"{key}-{sha[:12]}"
                       f"{Path(row.file_name).suffix or '.safetensors'}")
        out.append({
            "key": key, "version": spec.version, "sha256": sha,
            "install_row_id": row.id, "staged_name": staged_name,
            "strength": strength, "role": role,
            "base_family": spec.family,
            "base_model_key": base_spec.key,
            "base_model_sha256": base_sha,
            "bound_character_id": bound_char,
        })
    return out


def lora_prompt_guard(prompt: str, negative: str) -> None:
    """G2-B/B-1 — last-mile injection scan over the exact text bound for
    the runtime: prompt (composed — user text + context), negative prompt.
    Runs on EVERY local image generation, lora-selected or not."""
    from app.domain.commands import CommandError
    try:
        lora_tags.reject_lora_syntax(prompt, field="prompt")
        lora_tags.reject_lora_syntax(negative, field="negative_prompt")
    except lora_tags.LoraSyntaxError as e:
        raise CommandError(422, str(e)) from e


def cast_bound_loras(session: Session, shot) -> list:
    """B-3c — the proposed adapter set a shot-scoped preview resolves
    when the caller supplies no explicit ``loras``: every stored entry on
    the shot's own cast rows, verbatim — order and duplicates preserved.
    Corrupt historical entries are passed through so the resolver
    surfaces honest refusals instead of silently pre-cleaning data."""
    out: list = []
    for r in session.execute(
            select(ShotCharacter).where(
                ShotCharacter.shot_id == shot.id)).scalars():
        out.extend(r.lora_json or [])
    return out


def _requested_echo(loras_in) -> list[dict]:
    """Honest per-entry echo of what was proposed — shape, key, strength.
    Order and duplicates preserved; malformed entries are labeled
    'invalid' rather than dropped or rewritten."""
    out: list[dict] = []
    for raw in loras_in or []:
        if isinstance(raw, str):
            out.append({"shape": "string", "key": raw, "strength": None})
        elif isinstance(raw, dict):
            out.append({
                "shape": "object",
                "key": raw.get("key"),
                "strength": raw.get("strength"),
            })
        else:
            out.append({"shape": "invalid", "key": None,
                        "strength": None})
    return out


def resolve_lora_preview(session: Session, project: Project, *,
                       model_key: str | None = None,
                       loras_in=None,
                       shot_id: str | None = None,
                       keyframe_id: str | None = None,
                       prompt: str | None = None,
                       negative_prompt: str | None = None) -> dict:
    """B-3c — the read-only resolution preview behind
    ``POST /api/projects/{id}/loras/preview``.

    Runs the shared B-1 gate (`check_loras`) and the prompt-syntax guard
    against the caller's context and returns the resolution as data:
    refusals are explicit ``errors`` entries, never raised, so a refused
    selection renders next to an accepted one in the UI.

    Context mirroring the generation paths: ``keyframe_id`` resolves its
    shot exactly like ``keyframe.generate``; a ``shot_id`` that does not
    belong to the project is a hard context error — silently degrading
    to "unbound" would hide the real problem. When ``loras_in`` is
    omitted the proposed set comes from the shot's stored cast bindings
    (`cast_bound_loras`); an explicit list is resolved as given. With no
    shot context the proposed set is empty.

    Hashes/provenance: adapter sha256 values are the same pins
    `check_loras` stamps into durable requests (catalog-pinned sha, else
    the installed row's recorded sha — byte-level re-verification runs
    at worker start, not here). ``prompt_sha256``/``runtime_prompt_sha256``
    are computed only when a prompt is supplied; anything not computable
    without staging or executing is named in ``hashes.unavailable``.

    Read-only: SELECT + file-stat only — no rows written, no job queued,
    no subprocess, no download. Advisory: a real generation re-validates
    against current state."""
    from app.domain.commands import CommandError
    from app.db.models import Keyframe

    import hashlib

    errors: list[str] = []
    prompt_errors: list[str] = []
    notes: list[str] = []
    unavailable: list[str] = []

    # --- context: mirror generation scoping exactly -------------------
    shot = None
    if keyframe_id:
        kf = session.get(Keyframe, keyframe_id)
        if kf is None or kf.project_id != project.id:
            errors.append("keyframe not found in this project")
        else:
            s = session.get(Shot, kf.shot_id)
            if s is None or s.project_id != project.id:
                errors.append("keyframe's shot is missing from this project")
            else:
                shot = s
    if shot_id:
        s = session.get(Shot, shot_id)
        if s is None or s.project_id != project.id:
            errors.append("shot not found in this project")
        elif shot is not None and s.id != shot.id:
            errors.append("shot_id and keyframe_id resolve to different "
                          "shots — pass one context")
        else:
            shot = s

    spec = model_catalog.get(model_key or "")
    if spec is None:
        notes.append(f"model_key '{model_key}' is not in the catalog — "
                     "generation would refuse before adapter selection")

    # --- proposed selection -------------------------------------------
    derived = loras_in is None
    proposed = (cast_bound_loras(session, shot)
                if derived and shot is not None
                else (loras_in if loras_in is not None else []))
    requested = _requested_echo(proposed)
    if derived and shot is not None:
        notes.append("proposed selection derived from the shot's stored "
                     "cast bindings")
    if shot is None:
        notes.append("no shot context — identity adapters always refuse "
                     "without a cast binding")

    # --- shared B-1 gate ----------------------------------------------
    resolved: list[dict] | None = None
    if not errors:
        try:
            resolved = check_loras(session, project, proposed, spec,
                                   shot=shot)
        except CommandError as e:
            errors.append(e.detail)

    # --- prompt guard (only exercised when text is supplied) ----------
    prompt_checked = prompt is not None or negative_prompt is not None
    if prompt_checked:
        try:
            lora_prompt_guard(prompt or "", negative_prompt or "")
        except CommandError as e:
            prompt_errors.append(e.detail)
    else:
        notes.append("no prompt supplied — the prompt-injection guard "
                     "was not exercised")

    accepted = resolved is not None and not prompt_errors

    # --- response ------------------------------------------------------
    adapters = [] if resolved is None else [{
        "key": e["key"],
        "version": e["version"],
        "role": e["role"],
        "strength": e["strength"],
        "sha256": e["sha256"],
        "staged_name": e["staged_name"],
        "base_family": e["base_family"],
        "base_model_key": e["base_model_key"],
        "base_model_sha256": e["base_model_sha256"],
        "bound_character_id": e["bound_character_id"],
    } for e in resolved]

    adapter_sha = {a["key"]: a["sha256"] for a in adapters}
    prompt_sha = (hashlib.sha256((prompt or "").encode("utf-8"))
                  .hexdigest() if prompt is not None else None)
    runtime_sha = None
    if accepted and prompt is not None:
        runtime_sha = lora_tags.runtime_prompt_sha256(prompt, resolved)
    if prompt is not None and not accepted:
        unavailable.append("runtime_prompt_sha256 — only computed for "
                           "an accepted selection")
    if prompt is None:
        unavailable.append("prompt_sha256 / runtime_prompt_sha256 — no "
                           "prompt supplied")
    unavailable.append(
        "staged-file content hash — adapter bytes are re-hashed by the "
        "worker at job start; the values above are the request pins")

    notes.append("advisory resolution — nothing was queued or "
                 "authorized; a later generation re-validates against "
                 "current state")

    return {
        "accepted": accepted,
        "errors": errors,
        "prompt_errors": prompt_errors,
        "requested": requested,
        "resolved": adapters,
        "hashes": {
            "adapter_sha256": adapter_sha,
            "base_model_sha256": (adapters[0]["base_model_sha256"] or None)
                                 if adapters else
                                 (spec.files[0].sha256
                                  if spec and spec.files else None),
            "prompt_sha256": prompt_sha,
            "runtime_prompt_sha256": runtime_sha,
            "unavailable": unavailable,
        },
        "est_vram_lora_overhead_bytes":
            None if resolved is None
            else LORA_VRAM_OVERHEAD_BYTES * len(resolved),
        "context": {
            "shot_id": shot.id if shot else None,
            "keyframe_id": keyframe_id,
            "model_key": model_key,
            "proposed_from_cast": derived and shot is not None,
        },
        "notes": notes,
    }
