"""C2.0a — cloud text pre-flight helpers: eligibility, estimate, data-out.

Everything here is advisory metadata for the preview/commit path — never
an authorization by itself:

- ``t1_text_strategy`` picks the documented T1 official-API text path a
  simulated cloud job would exercise. Eligibility is catalog-driven;
  no strategy is resolved, no tier promoted.
- ``estimate_text_cost`` produces a clearly-labeled ESTIMATE from the
  catalog ``cost_table`` — never an actual charge. Durable spend
  enforcement does not exist until the approved spend_ledger migration;
  nothing here claims otherwise.
- ``data_out_summary`` states what would leave the machine (consent
  surface per PROGRAM_C §7).
"""

from __future__ import annotations

from typing import Any

# Rough text heuristic — 4 chars/token, input-side; a nominal output
# allowance is included so the estimate is not misleadingly low.
_CHARS_PER_TOKEN = 4
_ASSUMED_OUTPUT_TOKENS = 512


def t1_text_strategy(provider: dict) -> dict | None:
    """First documented T1 official_api strategy covering text.* — the
    path a cloud text job simulates. None → provider is not eligible."""
    for s in provider.get("strategies") or []:
        if (s.get("method") == "official_api" and s.get("tier") == "T1"
                and any((c or "").startswith("text")
                        for c in s.get("capabilities") or [])):
            return s
    return None


def pick_text_model(provider: dict, model_id: str | None) -> dict | None:
    """Resolve a text-capable model from the catalog; default = first."""
    models = [m for m in provider.get("models") or []
              if (m.get("capability") or "").startswith("text")]
    if model_id:
        return next((m for m in models if m.get("id") == model_id), None)
    return models[0] if models else None


def estimate_text_cost(model: dict, prompt: str) -> dict[str, Any]:
    """Labeled ESTIMATE only — heuristic tokens × catalog rates.

    ``cost_table`` may be absent (est unknown) — the caller decides
    whether an unknown estimate is acceptable (it is NOT for a real
    billable call; the mock tolerates it)."""
    est_input = max(1, len(prompt or "") // _CHARS_PER_TOKEN)
    est_output = _ASSUMED_OUTPUT_TOKENS
    table = (model or {}).get("cost_table") or {}
    in_rate = table.get("input_per_mtok")
    out_rate = table.get("output_per_mtok")
    est_usd = None
    if in_rate is not None and out_rate is not None:
        est_usd = round(est_input * in_rate / 1e6
                        + est_output * out_rate / 1e6, 6)
    return {
        "est_input_tokens": est_input,
        "assumed_output_tokens": est_output,
        "est_cost_usd": est_usd,
        # Honesty invariant: every consumer must render this as an
        # estimate, never a charge. Durable spend limits are NOT
        # enforced in C2.0a (no spend_ledger yet).
        "is_estimate": True,
        "rates_checked": table.get("checked"),
    }


def data_out_summary(provider: dict, prompt: str) -> str:
    """What leaves the machine — consent surface (PROGRAM_C §7)."""
    label = provider.get("label") or provider.get("key") or "provider"
    return f"prompt text (~{len(prompt or '')} chars) → {label}"
