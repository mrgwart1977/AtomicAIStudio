"""C1.5 Access Bridge — dispatch + per-feature capability reporting (slice 2).

The bridge is the ONLY path by which a catalog strategy becomes an action.
Every dispatch is a command (preview → commit → audit) and every action is
user-driven by construction: ADE never submits a prompt, never automates a
browser, and never calls a provider API in this slice.

Honesty model — a capability report distinguishes three things that must
never be conflated:

- ``verification: "catalog_documented"`` — the strategy record exists and
  cites an evidence URL. This is NOT integration authorization, NOT proof
  the feature works, and NOT a successful integration test.
- ``dispatch`` — which bridge actions can run *now* for that capability:
  T2/T3 get user-driven prepare/launch/import; T1 reports
  ``t1_not_implemented`` until real API adapters land in C2–C6; T4 and
  prohibited paths get nothing.
- ``status`` — ``documented`` | ``needs_recheck`` | ``refused_prohibited``.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.domain.providers import catalog, registry

PROMPT_MAX_CHARS = 8000
IMPORT_CONTENT_MAX_CHARS = 1_000_000

ACTIONS = ("prepare", "launch", "import")

# Methods that document a user-driven (site/file) path rather than a
# programmatic channel — only these may carry prepare/launch/import.
_USER_DRIVEN_METHODS = (
    "assisted_workflow", "browser_authorized", "desktop_plugin",
    "import_export",
)

_TIER_ACTIONS = {
    "T1": (),
    "T2": ("prepare", "launch", "import"),
    "T3": ("import",),
    "T4": (),
}

_TIER_USER_ACTION = {
    "T2": "site_workflow",
    "T3": "file_import",
    "T4": "none_available",
}

_TIER_BLOCKED = {
    "T1": "t1_not_implemented",
    "T4": "informational_only",
}


record_stale = registry.record_stale


def get_provider(key: str | None) -> dict | None:
    if not key:
        return None
    return catalog.get_provider(key)


def resolve_strategy(provider: dict, action: str,
                     strategy_id: str | None = None) -> tuple[dict | None, str | None]:
    """Pick the strategy a bridge action would run under. Returns
    (strategy, error). Errors are refusal reasons, never silent fallbacks."""
    if action not in ACTIONS:
        return None, f"unknown bridge action '{action}'"
    strategies = provider.get("strategies") or []
    if strategy_id:
        rec = next((s for s in strategies if s.get("strategy_id") == strategy_id), None)
        if rec is None:
            return None, f"strategy_id '{strategy_id}' not found for provider '{provider['key']}'"
        if rec.get("automation_permission") == "prohibited":
            return None, ("strategy is prohibited — it exists only to document "
                          "why this path is not offered")
        strat = rec
    else:
        usable = registry.usable_strategies(provider)
        if not usable:
            return None, ("no usable access strategy — provider is "
                          "monitor-only or refused")
        if action == "import":
            strat = next((s for s in usable
                          if s.get("method") == "import_export"), None)
            if strat is None:
                strat = next((s for s in usable
                              if s.get("method") in _USER_DRIVEN_METHODS
                              and s.get("tier") in ("T2", "T3")), None)
            if strat is None:
                return None, ("no documented user-driven import path — only "
                              "programmatic strategies exist, which the bridge "
                              "does not dispatch in C1.5")
        else:  # prepare/launch — site-workflow tiers only
            strat = next((s for s in usable
                          if s.get("method") in _USER_DRIVEN_METHODS
                          and s.get("tier") == "T2"), None)
            if strat is None:
                tiers = "/".join(sorted({s["tier"] for s in usable}))
                return None, ("no documented T2 assisted/browser strategy — "
                              f"provider offers {tiers or 'nothing'}; "
                              "T1 dispatch is not implemented in C1.5")
    tier = strat.get("tier")
    if tier == "T1":
        return None, ("T1 API dispatch is not implemented in C1.5 — the bridge "
                      "never performs programmatic provider calls (C2–C6 scope)")
    if tier == "T4":
        return None, "informational record only — no executable path"
    if action in ("prepare", "launch") and tier != "T2":
        return None, (f"'{action}' requires a T2 site-workflow strategy; "
                      f"resolved strategy is {tier}")
    return strat, None


def resolve_launch_url(provider: dict, strat: dict) -> str | None:
    """The URL ``bridge.launch`` may open — resolved ONLY from validated
    catalog fields (strategy launch_url → provider docs → provider ToS),
    HTTPS-only, never from a command payload."""
    candidates = (
        (strat.get("connection") or {}).get("launch_url"),
        provider.get("docs_url"),
        (strat.get("tos_evidence") or {}).get("url"),
        provider.get("tos_url"),
    )
    for cand in candidates:
        if not cand:
            continue
        u = urlparse(cand)
        if u.scheme == "https" and u.netloc:
            return cand
    return None


# Launch split: the backend command validates + audits the destination and
# returns it to the caller; the desktop UI performs the actual shell.open
# after the explicit user action. The backend NEVER opens a browser —
# there is no webbrowser/subprocess call in this module, so no background
# job, replay, or API request can produce an unexpected browser window.
OPEN_VIA = "client_shell"

TMP_PREFIX = "ade-bridge-"


def is_bridge_tmp(path) -> bool:
    """True only for temp files this workflow materialized — the guard on
    every delete so a caller-supplied path can never be unlinked."""
    try:
        p = Path(path)
        return (p.name.startswith(TMP_PREFIX)
                and p.parent == Path(tempfile.gettempdir()))
    except OSError:
        return False


def prepare_prompt(provider: dict, strat: dict, payload: dict) -> tuple[str, dict]:
    """PromptPreparer — render the user's text through the provider's
    configured prompt_template (``{prompt}`` placeholder) when present,
    else pass it through verbatim. Returns (prepared_text, meta)."""
    prompt = (payload.get("prompt") or "").strip()
    negative = (payload.get("negative_prompt") or "").strip()
    settings = payload.get("provider_settings") or {}
    template = (settings.get("prompt_template") or "").strip()
    if template and "{prompt}" in template:
        text = template.replace("{prompt}", prompt)
        if negative and "{negative_prompt}" in template:
            text = text.replace("{negative_prompt}", negative)
            negative = ""
    else:
        text = prompt
    if negative:
        text = f"{text}\n\nNegative: {negative}"
    meta = {
        "provider_key": provider["key"],
        "strategy_id": strat.get("strategy_id"),
        "method": strat.get("method"),
        "template_applied": bool(template and "{prompt}" in template),
        "paste_hint": (f"Copy this text and paste it into {provider['label']}'s "
                       "prompt field on their site — ADE does not submit, "
                       "fill, or click anything."),
    }
    return text, meta


def capability_report(provider: dict, today: date | None = None) -> dict:
    """Per-feature report — one entry per declared capability, always
    labeled ``catalog_documented`` (a cited evidence URL is not proof of
    authorization, capability, or a working integration)."""
    order: list[str] = []
    per_cap: dict[str, list[dict]] = {}
    for s in provider.get("strategies") or []:
        for cap in s.get("capabilities") or []:
            if cap not in per_cap:
                per_cap[cap] = []
                order.append(cap)
            per_cap[cap].append(s)

    entries: list[dict[str, Any]] = []
    for cap in order:
        strats = per_cap[cap]
        usable = [s for s in strats
                  if s.get("automation_permission") != "prohibited"]
        primary = next((s for s in usable if s.get("tier") == "T1"),
                       usable[0] if usable else None)
        if primary is None:
            entries.append({
                "capability": cap, "strategy_id": None, "method": None,
                "tier": None, "status": "refused_prohibited", "stale": False,
                "verification": "catalog_documented",
                "dispatch": [], "dispatch_blocked": "prohibited",
                "required_user_action": "none_available",
                "alternate": None,
            })
            continue
        tier = primary["tier"]
        stale = record_stale(primary, today)
        if stale:
            dispatch, blocked, action, status = (), "stale", "reverify_strategy", "needs_recheck"
        else:
            dispatch = _TIER_ACTIONS.get(tier, ())
            blocked = _TIER_BLOCKED.get(tier)
            action = _TIER_USER_ACTION.get(tier, "none_available")
            status = "documented"
        alternate = next((s for s in usable
                          if s is not primary
                          and s.get("method") in _USER_DRIVEN_METHODS
                          and s.get("tier") in ("T2", "T3")), None)
        entries.append({
            "capability": cap,
            "strategy_id": primary.get("strategy_id"),
            "method": primary.get("method"),
            "tier": tier,
            "status": status,
            "stale": stale,
            "verification": "catalog_documented",
            "dispatch": list(dispatch),
            "dispatch_blocked": blocked,
            "required_user_action": action,
            "alternate": (
                {"strategy_id": alternate.get("strategy_id"),
                 "method": alternate.get("method"),
                 "tier": alternate.get("tier")}
                if alternate else None),
        })
    return {
        "provider_key": provider["key"],
        "evidence_stamp": catalog.CATALOG_VERIFIED,
        "capabilities": entries,
    }


def validate_dispatch(session, project, command_type: str, payload: dict,
                      ) -> tuple[list[str], dict, dict]:
    """Shared preview/commit gate for bridge.* commands. Returns
    (warnings, provider, strategy); raises commands.CommandError on refusal.
    Imported lazily by commands.py to avoid an import cycle."""
    from app.domain.commands import CommandError

    # watch.scan gates on the import path — candidates are only useful when
    # the provider has a documented user-driven import strategy.
    action = ("import" if command_type == "bridge.watch.scan"
              else command_type.rsplit(".", 1)[1])
    provider = get_provider(payload.get("provider_key"))
    if provider is None:
        raise CommandError(404, "provider not found in catalog")
    strat, err = resolve_strategy(provider, action,
                                  payload.get("strategy_id"))
    if strat is None:
        raise CommandError(422, err)
    if record_stale(strat):
        raise CommandError(422, "strategy verification is stale — needs "
                                "re-check before bridge dispatch")

    warnings: list[str] = [
        "External use is governed by the provider's own terms — a documented "
        "strategy is not proof an integration method is permitted.",
    ]
    if command_type == "bridge.prepare":
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise CommandError(422, "prompt is required")
        if len(prompt) > PROMPT_MAX_CHARS:
            raise CommandError(422, f"prompt exceeds {PROMPT_MAX_CHARS} characters")
        warnings.append("Prepared locally — you paste it into the provider's "
                        "site yourself; ADE never submits automatically")
    elif command_type == "bridge.launch":
        url = resolve_launch_url(provider, strat)
        if url is None:
            raise CommandError(422, "no documented HTTPS launch surface for "
                                    "this provider")
        warnings.append(f"Opens {url} in your browser — ADE does not "
                        "automate, log in, or submit anything")
    elif command_type == "bridge.import":
        src = (payload.get("source_path") or "").strip()
        content = payload.get("content")
        if bool(src) == bool(content):
            raise CommandError(422, "exactly one of source_path or content "
                                    "is required")
        if src:
            from pathlib import Path
            if not Path(src).is_file():
                raise CommandError(422, f"file not found: {src}")
            if payload.get("from_watch"):
                # Scan→import revalidation — the earlier candidate list is
                # NOT trusted. Between scan and import the file may have
                # been deleted, replaced, swapped for a link, or moved;
                # re-check everything against the CURRENT filesystem and
                # the CURRENT designated folder.
                cfg = registry.get_config(session).get(provider["key"], {})
                folder, ferr = validate_watch_folder(cfg)
                if ferr:
                    raise CommandError(422, ferr)
                p = Path(src)
                if p.is_symlink():
                    raise CommandError(422, "candidate is a symlink — refused")
                _ij = getattr(p, "is_junction", None)
                if callable(_ij) and _ij():
                    raise CommandError(422, "candidate is a junction — refused")
                try:
                    rp = p.resolve(strict=True)
                except (OSError, RuntimeError):
                    raise CommandError(422, "candidate no longer exists")
                if not rp.is_file():
                    raise CommandError(422, "candidate is not a regular file")
                if rp.parent != folder:
                    raise CommandError(
                        422, "candidate is outside the designated folder")
                if rp.suffix.lower() not in watch_eligible_exts(provider):
                    raise CommandError(
                        422, "candidate type is not eligible for this provider")
        else:
            if not isinstance(content, str) or not content.strip():
                raise CommandError(422, "content must be non-empty text")
            if len(content) > IMPORT_CONTENT_MAX_CHARS:
                raise CommandError(422, f"content exceeds "
                                        f"{IMPORT_CONTENT_MAX_CHARS} characters")
        note = payload.get("note")
        if note is not None and not isinstance(note, str):
            raise CommandError(422, "note must be a string")
        warnings.append("Provenance is user-reported — ADE does not verify "
                        "the file actually came from this provider")
    elif command_type == "bridge.watch.scan":
        # Stage A ImportWatcher — explicit "check folder" only, no polling.
        cfg = registry.get_config(session).get(provider["key"], {})
        folder, ferr = validate_watch_folder(cfg)
        if ferr:
            raise CommandError(422, ferr)
        warnings.append(
            "Read-only scan of the folder you designated — candidates are "
            "untrusted local files; ADE does not verify provider origin, "
            "never reads their contents, and never imports without your "
            "per-file confirmation")
    return warnings, provider, strat


# ---- ImportWatcher (slice 4, Stage A — manual candidate scan only) ---------
# Opt-in by construction: nothing scans until the user designates a folder in
# the provider's settings (``import_dir``) AND clicks "check folder". The
# scan is a bounded, non-recursive, stat-only enumeration — file contents are
# never read here. Background polling (Stage B) stays separately gated.
WATCH_SETTING = "import_dir"
WATCH_MAX_CANDIDATES = 200
WATCH_MAX_ENTRIES = 5000  # hard bound on raw directory enumeration

KIND_EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"},
    "video": {".mp4", ".mov", ".webm", ".mkv", ".avi"},
    "audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"},
    "text": {".txt", ".md", ".srt", ".vtt", ".json", ".docx", ".pdf", ".rtf"},
}
_PREFIX_KIND = {"image": "image", "video": "video", "media": "video",
                "audio": "audio", "text": "text"}


def watch_eligible_exts(provider: dict) -> dict[str, str]:
    """extension → asset kind, restricted to the provider's documented
    capability prefixes. A provider with no usable capabilities still
    scans for media — the folder designation is the gate, not caps."""
    prefixes = {c.split(".", 1)[0]
                for s in provider.get("strategies") or []
                for c in (s.get("capabilities") or [])}
    kinds = {_PREFIX_KIND.get(p) for p in prefixes}
    kinds.discard(None)
    if not kinds:
        kinds = set(KIND_EXTENSIONS)
    return {e: k for k in kinds for e in KIND_EXTENSIONS[k]}


def validate_watch_folder(cfg: dict) -> tuple[Path | None, str | None]:
    """Resolve + gate the designated import folder. Returns (path, error).
    Read-only — never creates, writes, or follows out-of-folder links."""
    raw = (cfg.get("settings") or {}).get(WATCH_SETTING)
    if not raw or not str(raw).strip():
        return None, ("no import folder designated — pick one in provider "
                      "settings before scanning")
    raw = str(raw).strip()
    p = Path(raw).expanduser()
    if ".." in p.parts:
        return None, ("folder path must not contain '..' — give the "
                      "absolute path to the folder itself")
    if p.is_symlink():
        return None, "designated folder is a symlink — refused"
    _is_junction = getattr(p, "is_junction", None)
    if callable(_is_junction) and _is_junction():
        return None, "designated folder is a junction — refused"
    try:
        rp = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"designated folder does not exist: {p}"
    if not rp.is_dir():
        return None, f"designated path is not a directory: {rp}"
    return rp, None


def scan_watch_folder(folder: Path, ext_map: dict[str, str],
                      imported_norm: set[str],
                      limit: int | None = None
                      ) -> tuple[list[dict], bool]:
    """Bounded, non-recursive, stat-only enumeration of `folder`. Skips
    symlinks/junctions, anything that resolves outside `folder`, and paths
    already imported into the project (`imported_norm`, normcased). Never
    opens a file — contents are untrusted and read only at import time by
    the ingest adapter."""
    import os
    if limit is None:
        limit = WATCH_MAX_CANDIDATES
    cands: list[dict] = []
    truncated = False
    count = 0
    try:
        it = folder.iterdir()
    except OSError:
        return [], False
    for f in it:
        count += 1
        if count > WATCH_MAX_ENTRIES:
            truncated = True
            break
        try:
            if not f.is_file() or f.is_symlink():
                continue
            _is_junction = getattr(f, "is_junction", None)
            if callable(_is_junction) and _is_junction():
                continue
            kind = ext_map.get(f.suffix.lower())
            if kind is None:
                continue
            rp = f.resolve(strict=True)
            if rp.parent != folder:
                continue
            if os.path.normcase(str(rp)) in imported_norm:
                continue
            st = f.stat()
            cands.append({"name": f.name, "path": str(rp),
                          "size": st.st_size, "mtime": st.st_mtime,
                          "kind": kind})
        except OSError:
            continue
    cands.sort(key=lambda c: -c["mtime"])
    if len(cands) > limit:
        cands, truncated = cands[:limit], True
    return cands, truncated
