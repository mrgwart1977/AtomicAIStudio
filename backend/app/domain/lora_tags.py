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
