"""G2-B/B-1 — LoRA-control syntax boundary.

sd.cpp activates adapters from ``<lora:name:scale>`` tags parsed out of the
prompt string itself. Anything that reaches the runtime prompt — user text,
negative prompt, character descriptions, appended context — could therefore
silently activate an adapter. The structured ``loras`` request selections
are the ONLY authority: this module rejects anything that looks like a
control tag in untrusted text, and is the single constructor of the tags
the application itself appends.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

# Fail closed on any lora/lyco tag OPENER — complete or malformed. Trailing
# '>' is not required: a truncated tag is still an injection attempt.
LORA_SYNTAX_RE = re.compile(r"<\s*/?\s*l(?:ora|yco)\b", re.IGNORECASE)


class LoraSyntaxError(ValueError):
    """Untrusted text carried LoRA-control syntax."""


def reject_lora_syntax(text: str | None, *, field: str = "prompt") -> None:
    """Raise LoraSyntaxError if text contains LoRA-control syntax."""
    if not text:
        return
    m = LORA_SYNTAX_RE.search(text)
    if m:
        raise LoraSyntaxError(
            f"{field}: '{m.group(0)}…' looks like a LoRA control tag — "
            "adapter selection only happens through the loras field, "
            "never inside prompt text")


# Canonical staged filename contract — "<key>-<sha12>.safetensors".
_STAGED_NAME_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*-[0-9a-f]{12}\.safetensors")
# Windows device names are illegal as file stems on any drive.
_WIN_STEM_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10))})


def validate_staged_name(name: str) -> str:
    """Verify a queued ``staged_name`` against the app-generated contract
    at the staging boundary — independent of however the value arrived.
    Absolute paths, separators, traversal, unexpected extensions,
    Windows-illegal forms, and anything off-contract are refused."""
    if not isinstance(name, str) or not name:
        raise ValueError("staged LoRA name is required")
    if Path(name).name != name or ".." in name or name != name.strip():
        raise ValueError(f"unsafe staged LoRA name '{name}'")
    stem = name.rsplit(".", 1)[0]
    if stem.upper() in _WIN_STEM_RESERVED \
            or stem.endswith((" ", ".")):
        raise ValueError(f"unsafe staged LoRA name '{name}'")
    if not _STAGED_NAME_RE.fullmatch(name):
        raise ValueError(
            f"staged LoRA name '{name}' does not match the "
            "<key>-<sha12>.safetensors contract")
    return name


def lora_tag(entry: dict) -> str:
    """The one canonical application-emitted tag form. ``staged_name`` is
    adapter-derived ({key}-{sha12}.safetensors) — never user-controlled."""
    stem = Path(entry["staged_name"]).stem
    return f"<lora:{stem}:{float(entry['strength']):g}>"


def apply_lora_tags(prompt: str, loras: list[dict] | None) -> str:
    """Runtime prompt = composed prompt + approved tags, appended by the
    adapter/compose layer after the untrusted text passed the scan."""
    if not loras:
        return prompt
    return prompt + "".join(lora_tag(e) for e in loras)


def runtime_prompt_sha256(prompt: str, loras: list[dict] | None) -> str:
    """Hash of the exact prompt bytes the runtime receives — the honest
    pin alongside prompt_sha256 (which covers the composed text only)."""
    return hashlib.sha256(
        apply_lora_tags(prompt, loras).encode("utf-8")).hexdigest()
