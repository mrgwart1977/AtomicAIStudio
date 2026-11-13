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

import datetime as _dt
from typing import Any

# Rough text heuristic — 4 chars/token, input-side; a nominal output
# allowance is included so the estimate is not misleadingly low.
_CHARS_PER_TOKEN = 4
_ASSUMED_OUTPUT_TOKENS = 512

# Catalog pricing evidence expires: a `checked` date older than this —
# malformed, missing, stale, or future-dated — is not authoritative and
# fails closed (est None → caller refuses).
_PRICE_MAX_AGE_DAYS = 90


def _price_age_days(checked: Any) -> int | None:
    """Age in days of the pricing `checked` evidence, or None when it
    cannot be trusted (missing, malformed, or future-dated)."""
    try:
        d = _dt.date.fromisoformat(str(checked))
    except (ValueError, TypeError):
        return None
    age = (_dt.date.today() - d).days
    return age if age >= 0 else None


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


# ---------- C3-A — image models ----------

def t1_image_strategy(provider: dict) -> dict | None:
    """First documented T1 official-API strategy covering image.* —
    the path a simulated cloud image job would exercise."""
    for s in provider.get("strategies") or []:
        if (s.get("method") == "official_api" and s.get("tier") == "T1"
                and any((c or "").startswith("image")
                        for c in s.get("capabilities") or [])):
            return s
    return None


def pick_image_model(provider: dict, model_id: str | None) -> dict | None:
    """Resolve an image-capable model from the catalog; default = first."""
    models = [m for m in provider.get("models") or []
              if (m.get("capability") or "").startswith("image")]
    if model_id:
        return next((m for m in models if m.get("id") == model_id), None)
    return models[0] if models else None


def image_tier_rate(table: dict, quality: str | None) -> Any:
    """Resolve the per-image rate for a quality tier.

    Documented ``rates`` map tier → price. An unspecified quality (or an
    unrecognized tier) resolves to the HIGHEST documented tier — the
    provider may bill any tier it chooses, so worst-case is the only
    honest bound. Flat ``rate`` tables return their single rate.
    None → the table cannot price this unit (caller must refuse)."""
    rates = table.get("rates") or {}
    if quality and rates.get(quality) is not None:
        return rates[quality]
    if rates:
        return max(float(r) for r in rates.values())
    return table.get("rate")


def estimate_image_cost(model: dict, *, count: int = 1,
                        size: str | None = None,
                        quality: str | None = None,
                        prompt: str = "") -> dict[str, Any]:
    """Labeled ESTIMATE for image generation — extensible cost units.

    ``cost_table`` on an image model declares ONE billing ``unit``:
      per_image  — flat per-image rate, optionally tiered by
                   ``rates``{quality or size} (e.g. low/medium/high)
      per_mpixel — ``rate`` × megapixels requested (``size`` "WxH")
      per_mtok   — token-billed image models: ``input_per_mtok``/
                   ``output_per_mtok`` × heuristic tokens
      per_credit — ``credits_per_image`` × ``usd_per_credit``

    Unknown unit, missing rate, or absent ``checked`` evidence →
    ``est_cost_usd`` stays None and the CALLER must refuse. Pricing
    that cannot be reliably estimated fails closed, never guesses.
    """
    table = (model or {}).get("cost_table") or {}
    unit = table.get("unit")
    checked = table.get("checked")
    age = _price_age_days(checked)
    count = max(1, int(count or 1))
    est_usd = None
    if age is not None and age <= _PRICE_MAX_AGE_DAYS:
        if unit == "per_image":
            rate = image_tier_rate(table, quality)
            if rate is not None:
                est_usd = round(float(rate) * count, 6)
        elif unit == "per_mpixel":
            rate = table.get("rate")
            dims = _parse_size(size)
            if rate is not None and dims is not None:
                est_usd = round(
                    (dims[0] * dims[1]) / 1e6 * float(rate) * count, 6)
        elif unit == "per_mtok":
            in_rate = table.get("input_per_mtok")
            out_rate = table.get("output_per_mtok")
            out_est = table.get("assumed_output_tokens")
            if (in_rate is not None and out_rate is not None
                    and out_est is not None):
                est_usd = round(
                    (max(1, len(prompt or "") // _CHARS_PER_TOKEN)
                     * float(in_rate) + float(out_est) * float(out_rate)
                     ) / 1e6 * count, 6)
        elif unit == "per_credit":
            credits = table.get("credits_per_image")
            usd_per_credit = table.get("usd_per_credit")
            if credits is not None and usd_per_credit is not None:
                est_usd = round(
                    float(credits) * float(usd_per_credit) * count, 6)
    return {
        "unit": unit,
        "count": count,
        "size": size, "quality": quality,
        "est_cost_usd": est_usd,
        "is_estimate": True,
        "rates_checked": checked,
        "price_age_days": age,
        "price_fresh": age is not None and age <= _PRICE_MAX_AGE_DAYS,
    }


def _parse_size(size: str | None) -> tuple[int, int] | None:
    """'1024x1024' | '16:9' → pixel dims; None when unparseable."""
    if not size:
        return None
    s = str(size).lower().strip()
    if "x" in s:
        try:
            w, h = s.split("x", 1)
            return int(w), int(h)
        except (ValueError, TypeError):
            return None
    aspect = {"1:1": (1024, 1024), "16:9": (1792, 1024),
              "9:16": (1024, 1792), "4:3": (1365, 1024),
              "3:2": (1536, 1024), "2:3": (1024, 1536)}
    return aspect.get(s)
