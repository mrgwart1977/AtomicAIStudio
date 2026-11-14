"""C1 provider catalog — all 60 requested entries, declarative data.

Each row = one catalog entry exactly as requested. `account` is the
normalization key: rows sharing an account share credentials (Google
covers Gemini text + Gemini Images + Veo, etc.). Audited classes:
32 A / 16 B / 4 C / 2 D / 6 E by entry (resolved-provider view 34/16/4/2/4).

Strategies use the C1.5 taxonomy: official_api | aggregator_api |
mcp_connector | browser_authorized | desktop_plugin | assisted_workflow |
import_export | external_info. tier: T1 direct programmatic · T2 assisted
· T3 manual import · T4 informational.
"""

from __future__ import annotations

from pathlib import PurePosixPath

CATEGORIES = ("text", "image", "video", "audio")
ACCESS_CLASSES = ("A", "B", "C", "D", "E")
TIERS = ("T1", "T2", "T3", "T4")
METHODS = (
    "official_api", "aggregator_api", "mcp_connector", "browser_authorized",
    "desktop_plugin", "assisted_workflow", "import_export", "external_info",
)


# C1.5 §3 — catalog-wide verification stamp for strategy evidence.
# Date of the last completed pass over evidence URLs + record consistency —
# certify only that the cited URL resolves to the provider's real ToS/docs
# and that validate_catalog() passes; method-level permission review is the
# §9 re-check performed at enable time.
CATALOG_VERIFIED = "2026-11-11"

METHODS = {"official_api", "aggregator_api", "mcp_connector",
           "browser_authorized", "desktop_plugin", "assisted_workflow",
           "import_export", "external_info"}
# Access-method → tiers it may legitimately claim (§1/§2). T1 requires an
# authorized unattended-capable channel; assisted/manual/info are capped.
METHOD_TIERS = {
    "official_api": {"T1"}, "aggregator_api": {"T1"},
    "mcp_connector": {"T1", "T2"}, "desktop_plugin": {"T1", "T2"},
    "browser_authorized": {"T2"}, "assisted_workflow": {"T2"},
    "import_export": {"T3"}, "external_info": {"T4"},
}
AUTOMATION_PERMS = {"prohibited", "user_initiated", "assisted", "unattended"}
USER_INTERACTIONS = {"none", "consent", "submit", "download", "all"}
_AUTH_DEFAULT = {
    "official_api": "api_key", "aggregator_api": "api_key",
    "mcp_connector": "oauth_pkce", "browser_authorized": "account_session",
    "desktop_plugin": "account_session", "assisted_workflow": "account_session",
    "import_export": "none", "external_info": "none",
}
_COST_MAP = {"per_token": "per_credit", "per_image": "per_credit",
             "per_second": "per_credit", "per_character": "per_credit",
             "per_request": "per_credit", "per_video": "per_credit",
             "per_generation": "per_credit", "credits": "per_credit",
             "per_minute": "per_credit", "subscription": "subscription",
             "free": "free"}


def _p(key, label, category, cls, account, *, auth=(), creds=(),
       base_url=None, region=False, job_model=None, pricing=None,
       status_reason="", strategies=(), models=(), docs=None, tos=None,
       privacy="", notes="", aliases=(), cli=None):
    p = {
        "key": key, "label": label, "category": category,
        "access_class": cls, "account": account,
        "auth_methods": list(auth), "credential_fields": list(creds),
        "base_url": base_url, "region_selectable": region,
        "job_model": job_model, "pricing_model": pricing,
        "status_reason": status_reason,
        "strategies": [dict(s) for s in strategies],
        "models": list(models), "docs_url": docs, "tos_url": tos,
        "privacy_summary": privacy, "notes": notes,
        "aliases": list(aliases),
        # C1.5 §5 — optional documented local-CLI surface. Presence means
        # "a CLI is documented to exist", never that it is installed,
        # authentic, or executable by ADE. None = no documented CLI.
        "cli": dict(cli) if cli else None,
    }
    # Post-fill C1.5 §3 AccessStrategy fields that follow from context:
    # strategy_id/provider_id, evidence URL default (provider ToS > docs),
    # method-appropriate auth, and a cost guess from the pricing model.
    seen: dict[str, int] = {}
    for s in p["strategies"]:
        n = seen.get(s["method"], 0) + 1
        seen[s["method"]] = n
        s["strategy_id"] = f"{key}.{s['method']}.v{n}"
        s["provider_id"] = key
        ev = s["tos_evidence"]
        ev["url"] = ev.get("url") or tos or docs
        if s["auth_method"] is None:
            s["auth_method"] = _AUTH_DEFAULT.get(s["method"], "none")
        if s["cost_implications"] is None:
            s["cost_implications"] = _COST_MAP.get(pricing or "", "unknown")
    return p


def _key_credential(hint="API key"):
    return [{"name": "api_key", "secret": True, "required": True, "hint": hint}]


def _s(method, tier, caps, automation="unattended", interaction="none", note="",
       *, auth=None, tos_url=None, tos_note="", checked=None, verified=None,
       connection=None, inputs=(), outputs=(), settings=None, cost=None,
       reliability=""):
    """AccessStrategy record (C1.5 §3). _p() assigns strategy_id/provider_id
    and fills evidence/auth/cost defaults from the provider row."""
    return {"strategy_id": None, "provider_id": None,
            "method": method, "tier": tier, "capabilities": list(caps),
            "auth_method": auth,
            "automation_permission": automation,
            "user_interaction": interaction,
            "tos_evidence": {"url": tos_url, "note": tos_note,
                             "checked": checked or CATALOG_VERIFIED},
            "connection": dict(connection or {}),
            "input_formats": list(inputs), "output_formats": list(outputs),
            "settings_schema": dict(settings or {}),
            "cost_implications": cost, "reliability": reliability,
            "last_verified": verified or CATALOG_VERIFIED,
            "note": note}


_API = "official_api"
_AGGR = "aggregator_api"
_MCP = "mcp_connector"
_DESK = "desktop_plugin"
_ASSIST = "assisted_workflow"
_IMPEXP = "import_export"
_INFO = "external_info"
_T2 = dict(automation="user_initiated", interaction="submit")
_T3 = dict(automation="user_initiated", interaction="all")


def _ed(*, img2img=False, refs=0, mask=False, outpaint=False,
        identity=False, weights=False, note=""):
    """C3-D1 — explicit per-model image-editing capability declaration.
    Every image-capability model carries one; a missing block or a
    False/zero field means UNSUPPORTED and must fail closed downstream.
    ``identity_reference`` records only that a provider accepts
    identity-aimed reference input — it is NEVER a claim of guaranteed
    character identity consistency."""
    return {"img2img": img2img, "max_reference_images": refs,
            "mask_inpaint": mask, "outpaint": outpaint,
            "identity_reference": identity,
            "reference_weights": weights, "note": note}


# Fields a normalized editing record always carries (fail-closed defaults).
EDITING_DEFAULTS = _ed()


def editing_caps(model: dict | None) -> dict:
    """Normalized editing capabilities for a catalog model row.
    Absent/partial records resolve to the all-False defaults — an
    undeclared capability is an unsupported capability."""
    caps = dict(EDITING_DEFAULTS)
    if isinstance(model, dict) and isinstance(model.get("editing"), dict):
        for k in EDITING_DEFAULTS:
            if k in model["editing"]:
                caps[k] = model["editing"][k]
    return caps


def model_editing_caps(provider_key: str, model_id: str) -> dict:
    """Editing caps by provider+model id; unknown rows fail closed."""
    p = get_provider(provider_key) or {}
    for m in p.get("models") or []:
        if m.get("id") == model_id:
            return editing_caps(m)
    return dict(EDITING_DEFAULTS)

# =====================================================================
# TEXT & WRITING (15)
# =====================================================================
TEXT = [
    _p("openai", "OpenAI / ChatGPT", "text", "A", "openai",
       auth=["api_key"], creds=_key_credential("platform.openai.com API key"),
       base_url="https://api.openai.com", job_model="sync",
       pricing="per_token",
       docs="https://platform.openai.com/docs", tos="https://openai.com/policies/terms-of-use",
       privacy="Sends prompt + attached references to OpenAI (US).",
       notes="ChatGPT subscription ≠ API access; per-token billing.",
       models=[{"id": "gpt-5", "capability": "text",
                "cost_table": {"input_per_mtok": 1.25, "output_per_mtok": 10.0,
                               "checked": CATALOG_VERIFIED, "note": "estimate"}},
               {"id": "gpt-5-mini", "capability": "text",
                "cost_table": {"input_per_mtok": 0.25, "output_per_mtok": 2.0,
                               "checked": CATALOG_VERIFIED, "note": "estimate"}}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream", "text.structured"])],
       cli={"executables": ["openai"], "platforms": ["windows", "linux", "macos"],
            "install_hint_dirs": [],
            "docs_url": "https://github.com/openai/openai-python",
            "note": "Official openai-python package ships an `openai` CLI."}),
    _p("anthropic", "Anthropic / Claude", "text", "A", "anthropic",
       auth=["api_key", "oauth"], creds=_key_credential("console.anthropic.com API key"),
       base_url="https://api.anthropic.com", job_model="sync", pricing="per_token",
       docs="https://docs.anthropic.com", tos="https://www.anthropic.com/legal/aup",
       privacy="Sends prompt + attached references to Anthropic (US).",
       notes="Claude subscription ≠ API access.",
       models=[{"id": "claude-opus-4-1", "capability": "text",
                "cost_table": {"input_per_mtok": 15.0, "output_per_mtok": 75.0,
                               "checked": CATALOG_VERIFIED, "note": "estimate"}},
               {"id": "claude-sonnet-4-5", "capability": "text",
                "cost_table": {"input_per_mtok": 3.0, "output_per_mtok": 15.0,
                               "checked": CATALOG_VERIFIED, "note": "estimate"}}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream", "text.structured", "text.reasoning"])],
       cli={"executables": ["claude"], "platforms": ["windows", "linux", "macos"],
            "install_hint_dirs": [],
            "docs_url": "https://docs.anthropic.com/en/docs/claude-code/overview",
            "note": "Official Claude Code CLI (`claude`), npm @anthropic-ai/claude-code."}),
    _p("google-gemini", "Google / Gemini", "text", "A", "google",
       auth=["api_key"], creds=_key_credential("AI Studio or Vertex credential"),
       base_url="https://generativelanguage.googleapis.com", job_model="sync",
       pricing="per_token", region=True,
       docs="https://ai.google.dev/gemini-api/docs", tos="https://ai.google.dev/gemini-api/terms",
       privacy="Sends prompt + attached references to Google.",
       notes="Shared Google credential also covers Gemini Images and Veo.",
       models=[{"id": "gemini-2.5-pro", "capability": "text",
                "cost_table": {"input_per_mtok": 1.25, "output_per_mtok": 10.0,
                               "checked": CATALOG_VERIFIED, "note": "estimate"}},
               {"id": "gemini-2.5-flash", "capability": "text",
                "cost_table": {"input_per_mtok": 0.30, "output_per_mtok": 2.50,
                               "checked": CATALOG_VERIFIED, "note": "estimate"}}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream", "text.structured"])],
       cli={"executables": ["gemini"], "platforms": ["windows", "linux", "macos"],
            "install_hint_dirs": [],
            "docs_url": "https://github.com/google-gemini/gemini-cli",
            "note": "Official Gemini CLI (`gemini`), npm @google/gemini-cli."}),
    _p("deepseek", "DeepSeek", "text", "A", "deepseek",
       auth=["api_key"], creds=_key_credential("platform.deepseek.com API key"),
       base_url="https://api.deepseek.com", job_model="sync", pricing="per_token",
       docs="https://api-docs.deepseek.com",
       privacy="Sends prompt to DeepSeek — CN data residency.",
       notes="OpenAI-compatible; V4 flash + pro (legacy V3 ids retired).",
       models=[{"id": "deepseek-flash", "capability": "text",
                "cost_table": {"input_per_mtok": 0.30, "output_per_mtok": 1.20,
                               "checked": "2026-11-13",
                               "note": "peak rate (off-peak half); estimate"}},
               {"id": "deepseek-v4-pro", "capability": "text",
                "cost_table": {"input_per_mtok": 1.32, "output_per_mtok": 3.96,
                               "checked": "2026-11-13",
                               "note": "peak rate (off-peak half); estimate"}}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream", "text.reasoning"])]),
    _p("jasper", "Jasper", "text", "B", "jasper",
       auth=["api_key", "oauth"], creds=_key_credential("Jasper Business workspace key"),
       base_url="https://api.jasper.ai", job_model="sync", pricing="subscription",
       status_reason="Official API requires Jasper Business plan.",
       docs="https://developers.jasper.ai",
       privacy="Sends prompt to Jasper.",
       models=[{"id": "jasper-engine", "capability": "text"}],
       strategies=[_s(_API, "T1", ["text.generate"], note="Business plan only"),
                   _s(_ASSIST, "T2", ["text.generate"], connection={"launch_url": "https://www.jasper.ai"}, **_T2)]),
    _p("copyai", "Copy.ai", "text", "B", "copyai",
       auth=["api_key"], creds=_key_credential("Copy.ai API key"),
       base_url="https://api.copy.ai", job_model="async_poll", pricing="subscription",
       status_reason="Workflows API — runs pre-built workflow IDs only.",
       docs="https://docs.copy.ai",
       notes="Cannot send arbitrary prompts; workflows defined in their UI.",
       strategies=[_s(_API, "T1", ["text.workflow"], note="workflow-scoped"),
                   _s(_ASSIST, "T2", ["text.generate"], connection={"launch_url": "https://www.copy.ai"}, **_T2)]),
    _p("writesonic", "Writesonic", "text", "C", "writesonic",
       auth=["api_key"], creds=_key_credential("Writesonic Business API key"),
       base_url="https://api.writesonic.com", pricing="subscription",
       tos="https://writesonic.com/legal/terms",
       status_reason="Botsonic/SEO APIs only — no creative-writing endpoint.",
       strategies=[_s(_API, "T1", ["text.chatbot"], note="Botsonic scope, not creative writing"),
                   _s(_ASSIST, "T2", ["text.generate"], connection={"launch_url": "https://writesonic.com"}, **_T2)]),
    _p("writer", "Writer", "text", "B", "writer",
       auth=["api_key"], creds=_key_credential("Writer API key"),
       base_url="https://api.writer.com", job_model="sync", pricing="per_token",
       docs="https://dev.writer.com",
       models=[{"id": "palmyra-x5", "capability": "text"}],
       strategies=[_s(_API, "T1", ["text.generate", "text.structured"])]),
    _p("notion-ai", "Notion AI", "text", "B", "notion",
       auth=["api_key"], creds=[{"name": "token", "secret": True, "required": True,
                                 "hint": "Internal integration token (PAT)"}],
       base_url="https://api.notion.com", job_model="async_poll", pricing="subscription",
       tos="https://www.notion.so/terms",
       status_reason="Agent APIs — session-shaped, workspace-coupled.",
       strategies=[_s(_API, "T1", ["text.agent"], note="agent-session shaped"),
                   _s(_ASSIST, "T2", ["text.generate"], connection={"launch_url": "https://www.notion.so"}, **_T2)]),
    _p("anyword", "Anyword", "text", "B", "anyword",
       auth=["api_key"], creds=_key_credential("Anyword Enterprise API key"),
       job_model="sync", pricing="subscription",
       tos="https://www.anyword.com/terms",
       status_reason="Official API is Enterprise-plan only.",
       strategies=[_s(_API, "T1", ["text.generate"], note="Enterprise only"),
                   _s(_ASSIST, "T2", ["text.generate"], connection={"launch_url": "https://www.anyword.com"}, **_T2)]),
    _p("sudowrite", "Sudowrite", "text", "E", "sudowrite",
       tos="https://sudowrite.com/terms-and-conditions",
       status_reason="No public API — feature request open on their board.",
       strategies=[_s(_ASSIST, "T2", ["text.generate"],
                      connection={"launch_url": "https://sudowrite.com"}, **_T2),
                   _s(_IMPEXP, "T3", ["text.import"], **_T3)]),
    _p("quillbot", "QuillBot", "text", "E", "quillbot",
       tos="https://quillbot.com/terms",
       status_reason="Officially no API (help center); ToS restricts automation.",
       strategies=[_s(_IMPEXP, "T3", ["text.roundtrip"], **_T3),
                   _s(_ASSIST, "T2", ["text.rewrite"], connection={"launch_url": "https://quillbot.com"}, **_T2)]),
    _p("rytr", "Rytr", "text", "A", "rytr",
       auth=["api_key"], creds=_key_credential("app.rytr.me API key"),
       base_url="https://app.rytr.me/v1", job_model="sync", pricing="per_character",
       tos="https://rytr.me/trust/terms-of-use",
       docs="https://rytr.me/products/api",
       models=[{"id": "rytr-default", "capability": "text"}],
       strategies=[_s(_API, "T1", ["text.generate"])]),
    _p("xai", "xAI / Grok", "text", "A", "xai",
       auth=["api_key"], creds=_key_credential("console.x.ai API key"),
       base_url="https://api.x.ai", job_model="sync", pricing="per_token",
       docs="https://docs.x.ai",
       notes="Shared xAI credential also covers Grok Imagine.",
       models=[{"id": "grok-4", "capability": "text",
                "cost_table": {"input_per_mtok": 3.0, "output_per_mtok": 15.0,
                               "checked": CATALOG_VERIFIED, "note": "estimate"}}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream"])]),
    _p("hemingway", "Hemingway Editor Plus", "text", "E", "hemingway",
       status_reason="No API — readability/rewrite product only.",
       strategies=[_s(_IMPEXP, "T3", ["text.roundtrip"],
                      tos_url="https://www.hemingwayapp.com/",
                      tos_note="Editor product only — no API offered; manual .docx/.md exchange", **_T3),
                   _s(_ASSIST, "T2", ["text.rewrite"],
                      tos_url="https://www.hemingwayapp.com/", **_T2)]),
]

# =====================================================================
# IMAGES (15)
# =====================================================================
IMAGES = [
    _p("midjourney", "Midjourney", "image", "E", "midjourney",
       status_reason="No official API; ToS prohibits automation.",
       notes="Closed MCP test excludes product use. Assisted workflow only.",
       docs="https://docs.midjourney.com", tos="https://www.midjourney.com/terms",
       strategies=[_s(_ASSIST, "T2", ["image.generate"],
                      automation="user_initiated", interaction="submit",
                      connection={"launch_url": "https://www.midjourney.com"},
                      note="ToS permits user-driven web use — never automation"),
                   _s(_MCP, "T4", ["image.generate"],
                      automation="prohibited",
                      note="Closed MCP test explicitly excludes SaaS/product "
                           "use — recorded to document why this path is refused",
                      tos_note="Official MCP test excludes product integration; "
                               "ToS prohibits automation")]),
    _p("openai-images", "OpenAI Images", "image", "A", "openai",
       auth=["api_key"], creds=_key_credential("shared OpenAI API key"),
       base_url="https://api.openai.com", job_model="sync", pricing="per_image",
       docs="https://platform.openai.com/docs",
       privacy="Sends prompt + reference images to OpenAI (US).",
       notes="Uses the shared OpenAI credential.",
       models=[{"id": "gpt-image-1", "capability": "image",
                # Documented Images-API generation parameters.
                "sizes": ["auto", "1024x1024", "1536x1024",
                          "1024x1536"],
                "qualities": ["auto", "low", "medium", "high"],
                # Token-billed upstream; per-image equivalents from the
                # pricing doc — conservative, always the higher tier.
                "cost_table": {"unit": "per_image",
                               "rates": {"low": 0.02, "medium": 0.07,
                                         "high": 0.19},
                               "checked": "2026-11-13",
                               "note": "per-image equivalents of token "
                                       "billing; estimate"},
                # C3-D1 — Images API edits: array of input images
                # (documented up to 16) + mask for inpainting.
                "editing": _ed(img2img=True, refs=16, mask=True,
                               note="edits endpoint: input-image array "
                                    "≤16 + transparent-mask inpaint")}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit"])]),
    _p("gemini-images", "Gemini Images / Nano Banana", "image", "A", "google",
       auth=["api_key"], creds=_key_credential("shared Google credential"),
       base_url="https://generativelanguage.googleapis.com", job_model="sync",
       pricing="per_image", region=True,
       docs="https://ai.google.dev/gemini-api/docs/image-generation",
       notes="Uses the shared Google credential.",
       models=[{"id": "gemini-2.5-flash-image", "capability": "image",
                # 10 documented aspect ratios (Oct 2025 GA note).
                "sizes": ["1:1", "3:2", "2:3", "3:4", "4:3", "4:5",
                          "5:4", "9:16", "16:9", "21:9"],
                "qualities": ["auto"],
                "cost_table": {"unit": "per_image", "rate": 0.039,
                               "checked": "2026-11-13",
                               "source": "ai.google.dev/gemini-api/docs/pricing",
                               "note": "$0.039/image ($30/1M output "
                                       "tokens, 1290/img). Deprecated "
                                       "— shutdown Mar 15 2027; "
                                       "successor: Gemini 3.1 Flash "
                                       "Lite Image"},
                # C3-D1 — multi-image fusion documented to 3 inputs.
                "editing": _ed(img2img=True, refs=3,
                               note="multi-image fusion: up to 3 input "
                                    "images; no mask/outpaint surface")},
               {"id": "imagen-4.0-generate-001", "capability": "image",
                # Imagen :predict — 5 documented aspect ratios.
                "sizes": ["1:1", "3:4", "4:3", "9:16", "16:9"],
                "qualities": ["auto"],
                "cost_table": {"unit": "per_image", "rate": 0.04,
                               "checked": "2026-11-13",
                               "source": "developers.googleblog.com/en/"
                                         "imagen-4-now-available",
                               "note": "$0.04/image; Vertex docs list "
                                       "retirement Jun 30 2026"},
                "editing": _ed(note=":predict is generation-only — no "
                                    "image inputs declared")},
               {"id": "imagen-4.0-ultra-generate-001", "capability": "image",
                "sizes": ["1:1", "3:4", "4:3", "9:16", "16:9"],
                "qualities": ["auto"],
                "cost_table": {"unit": "per_image", "rate": 0.06,
                               "checked": "2026-11-13",
                               "source": "developers.googleblog.com/en/"
                                         "imagen-4-now-available",
                               "note": "$0.06/image Ultra tier"},
                "editing": _ed(note="generation-only — no image inputs "
                                    "declared")}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit"])]),
    _p("bfl-flux", "FLUX / Black Forest Labs", "image", "A", "bfl",
       auth=["api_key"], creds=_key_credential("api.bfl.ai key"),
       base_url="https://api.bfl.ai", job_model="async_poll", pricing="per_image",
       docs="https://docs.bfl.ai",
       models=[{"id": "flux-pro-1.1", "capability": "image",
                # Documented dimension range 256–1440, multiples of 32 —
                # declared presets are all compliant.
                "sizes": ["1024x1024", "1280x720", "720x1280",
                          "1440x1440", "1216x832", "832x1216"],
                "qualities": ["auto"], "seed": True,
                "cost_table": {"unit": "per_image", "rate": 0.04,
                               "checked": "2026-11-13",
                               "source": "docs.bfl.ai FLUX1.1 [pro] "
                                         "pricing",
                               "note": "$0.04/image flat per BFL "
                                       "published rate"},
                "editing": _ed(note="generation-only — no image inputs "
                                    "declared")},
               # Editing model (image-to-image) — outside the C3-C
               # generation contract; NO cost_table so commit fails
               # closed (pricing_unavailable) rather than guessing.
               {"id": "flux-kontext", "capability": "image.edit",
                # C3-D1 — Kontext: prompt-directed edit, single
                # input_image per BFL docs; no mask/outpaint params.
                "editing": _ed(img2img=True, refs=1,
                               note="Kontext edit: one input_image; "
                                    "prompt-directed, no mask")}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit"]),
                   _s(_AGGR, "T1", ["image.generate"], note="fal/Replicate")]),
    _p("adobe-firefly", "Adobe Firefly", "image", "B", "adobe",
       auth=["oauth_server"], creds=[{"name": "client_id", "secret": False, "required": True,
                                      "hint": "Firefly Services client ID"},
                                     {"name": "client_secret", "secret": True, "required": True,
                                      "hint": "Firefly Services client secret"}],
       base_url="https://firefly-api.adobe.io", job_model="sync", pricing="per_credit",
       tos="https://www.adobe.com/legal/terms.html",
       status_reason="Enterprise server-to-server OAuth credential.",
       notes="Shared Adobe credential also covers Firefly Video.",
       models=[{"id": "firefly-image-4", "capability": "image",
                # C3-C verification finding (2026-11): the Firefly API
                # is asynchronous + IMS OAuth and — critically — Adobe
                # publishes no per-image API USD price (web-app
                # generative credits are not API billing). With no
                # authoritative rate, cost admission must fail closed,
                # so NO cost_table is declared and this provider has no
                # implemented contract in this build.
                "notes": "API pricing not published per-image; "
                         "contract intentionally unimplemented",
                # C3-D1 — no editing contract is declared either;
                # everything fails closed alongside generation.
                "editing": _ed(note="no contract declared — all editing "
                                    "capabilities unsupported")}],
       strategies=[_s(_API, "T1", ["image.generate"], note="S2S OAuth scope"),
                   _s(_ASSIST, "T2", ["image.generate"], connection={"launch_url": "https://firefly.adobe.com"}, **_T2)]),
    _p("ideogram", "Ideogram", "image", "A", "ideogram",
       auth=["api_key"], creds=_key_credential("api.ideogram.ai key"),
       base_url="https://api.ideogram.ai", job_model="sync", pricing="per_image",
       docs="https://developer.ideogram.ai/api-reference/images/generate/ideogram-3",
       models=[{"id": "ideogram-3", "capability": "image",
                # v2 generate documents prompt + negative_prompt + seed.
                "negative_prompt": True, "seed": True,
                # Documented aspect_ratio enum (v2 generate).
                "sizes": ["1x1", "3x2", "2x3", "3x4", "4x3", "4x5",
                          "5x4", "9x16", "16x9", "10x16", "16x10",
                          "1x2", "2x1", "1x3", "3x1"],
                # rendering_speed doubles as the billing tier.
                "qualities": ["turbo", "default", "quality"],
                "cost_table": {"unit": "per_image",
                               "rates": {"turbo": 0.03,
                                         "default": 0.06,
                                         "quality": 0.09},
                               "checked": "2026-11-13",
                               "source": "ideogram.ai/pricing "
                                         "(API pricing tab)",
                               "note": "3.0 per-image rates: "
                                       "$0.03/$0.06/$0.09 by speed"},
                # C3-D1 — Ideogram 3.0 edit (image+mask) and extend
                # (outpaint) endpoints are documented.
                "editing": _ed(img2img=True, refs=1, mask=True,
                               outpaint=True,
                               note="edit (image+mask) + extend "
                                    "(outpaint) endpoints")}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit", "image.remix"])]),
    _p("canva", "Canva AI", "image", "B", "canva",
       auth=["oauth"], creds=[{"name": "oauth_token", "secret": True, "required": True,
                               "hint": "Connect API OAuth token (private app)"}],
       base_url="https://api.canva.com", job_model="async_poll", pricing="per_credit",
       tos="https://www.canva.com/policies/canva-developer-terms/",
       status_reason="Connect API in preview — public apps can't pass review yet.",
       models=[{"id": "craft-1", "capability": "image",
                "editing": _ed(note="no editing surface declared in "
                                    "the preview API")},
               {"id": "lucid-origin", "capability": "image",
                "editing": _ed(note="no editing surface declared in "
                                    "the preview API")}],
       strategies=[_s(_API, "T1", ["image.generate"], note="preview API, private apps"),
                   _s(_ASSIST, "T2", ["image.export"], connection={"launch_url": "https://www.canva.com"}, **_T2)]),
    _p("meta-ai", "Meta AI", "image", "B", "meta",
       auth=["api_key"], creds=_key_credential("dev.meta.ai key"),
       base_url="https://api.meta.ai/v1", job_model="sync", pricing="per_image",
       status_reason="Meta Model API (Muse) — new; onboarding unverified.",
       models=[{"id": "muse-image-1.0", "capability": "image",
                "editing": _ed(note="generation-only — onboarding "
                                    "unverified; no edit surface")}],
       strategies=[_s(_API, "T1", ["image.generate"])]),
    _p("recraft", "Recraft", "image", "A", "recraft",
       auth=["api_key"], creds=_key_credential("api.recraft.ai key"),
       base_url="https://external.api.recraft.ai", job_model="sync",
       pricing="per_credit",
       docs="https://www.recraft.ai/docs/api-reference",
       models=[{"id": "recraftv4_1", "capability": "image",
                # Documented raster size set.
                "sizes": ["1024x1024", "1024x1365", "1365x1024",
                          "1024x1536", "1536x1024", "1024x1707",
                          "1707x1024", "1024x1820", "1820x1024"],
                "qualities": ["auto"],
                "cost_table": {"unit": "per_credit",
                               "credits_per_image": 35,
                               "usd_per_credit": 0.001,
                               "checked": "2026-11-13",
                               "source": "recraft.ai/docs/api-reference/"
                                         "pricing",
                               "note": "V4.1 raster $0.035/img "
                                       "(35 API units; $1 = 1000)"},
                # C3-D1 — imageToImage (documented strength 0–1 acts
                # as the reference weight) + inpaint (image+mask).
                "editing": _ed(img2img=True, refs=1, mask=True,
                               weights=True,
                               note="imageToImage strength param + "
                                    "inpaint (image+mask) endpoints")},
               {"id": "recraftv3", "capability": "image",
                # V3 documents negative_prompt; V4.x does not.
                "negative_prompt": True,
                "sizes": ["1024x1024", "1024x1365", "1365x1024",
                          "1024x1536", "1536x1024", "1024x1707",
                          "1707x1024", "1024x1820", "1820x1024"],
                "qualities": ["auto"],
                "cost_table": {"unit": "per_credit",
                               "credits_per_image": 40,
                               "usd_per_credit": 0.001,
                               "checked": "2026-11-13",
                               "source": "recraft.ai/docs/api-reference/"
                                         "pricing",
                               "note": "V3 raster $0.04/img (40 units); "
                                       "only line with documented "
                                       "negative_prompt"},
                "editing": _ed(img2img=True, refs=1, mask=True,
                               weights=True,
                               note="imageToImage strength param + "
                                    "inpaint (image+mask) endpoints")}],
       strategies=[_s(_API, "T1", ["image.generate", "image.style"])]),
    _p("leonardo", "Leonardo AI", "image", "A", "leonardo",
       auth=["api_key"], creds=_key_credential("cloud.leonardo.ai key"),
       base_url="https://cloud.leonardo.ai/api/rest/v2", job_model="async_poll",
       pricing="per_credit", docs="https://docs.leonardo.ai",
       notes="Also serves Lucid Origin as model id 'lucid-origin'.",
       models=[{"id": "phoenix", "capability": "image",
                "editing": _ed(note="canvas strategy exists but no "
                                    "documented per-model edit surface")},
               {"id": "lucid-origin", "capability": "image",
                "editing": _ed(note="canvas strategy exists but no "
                                    "documented per-model edit surface")}],
       strategies=[_s(_API, "T1", ["image.generate", "image.canvas"])]),
    _p("openart", "OpenArt", "image", "C", "openart",
       auth=["oauth"], creds=[],
       base_url="https://mcp.openart.ai/mcp", pricing="per_credit",
       docs="https://openart.ai/mcp", tos="https://openart.ai/terms/",
       status_reason="Official MCP server + CLI — OAuth, no API key.",
       notes="MCP-marked models only; credits via your OpenArt account.",
       strategies=[_s(_MCP, "T1", ["image.generate", "video.generate"], note="official MCP"),
                   _s(_DESK, "T1", ["image.generate"], note="official CLI"),
                   _s(_ASSIST, "T2", ["image.generate"], connection={"launch_url": "https://openart.ai"}, **_T2)]),
    _p("freepik", "Freepik AI Suite", "image", "A", "freepik",
       auth=["api_key"], creds=_key_credential("x-freepik-api-key"),
       base_url="https://api.freepik.com", job_model="async_poll", pricing="per_credit",
       docs="https://docs.freepik.com",
       models=[{"id": "mystic", "capability": "image",
                "editing": _ed(img2img=True, refs=1,
                               note="provider declares image.edit "
                                    "strategy; single reference image")}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit", "video.generate"])]),
    _p("grok-imagine", "Grok Imagine", "image", "A", "xai",
       auth=["api_key"], creds=_key_credential("shared xAI API key"),
       base_url="https://api.x.ai", job_model="sync", pricing="per_image",
       notes="Uses the shared xAI credential.",
       models=[{"id": "grok-imagine-image-2.0", "capability": "image",
                "editing": _ed(note="generation-only documented")}],
       strategies=[_s(_API, "T1", ["image.generate", "video.generate"])]),
    _p("qwen-image", "Qwen Image", "image", "A", "dashscope",
       auth=["api_key"], creds=_key_credential("Alibaba Model Studio key"),
       base_url="https://dashscope-intl.aliyuncs.com", job_model="async_poll",
       pricing="per_image", region=True,
       notes="Region-pinned DashScope key (SG/BJ/TYO/HK).",
       models=[{"id": "qwen-image-3.0", "capability": "image",
                "editing": _ed(img2img=True, refs=1,
                               note="provider declares image.edit "
                                    "(qwen-image-edit surface); single "
                                    "reference image")}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit"]),
                   _s(_AGGR, "T1", ["image.generate"], note="fal/Replicate",
                      tos_url="https://www.alibabacloud.com/help/en/model-studio/",
                      tos_note="Aggregator listing authorized by the model vendor; official channel is DashScope")]),
    _p("lucid-origin", "Lucid Origin", "image", "A", "leonardo",
       status_reason="A model on the Leonardo account — not a separate provider.",
       notes="Catalog alias of Leonardo; no separate credentials.",
       models=[{"id": "lucid-origin", "capability": "image",
                "editing": _ed(note="alias of Leonardo lucid-origin — "
                                    "no documented edit surface")}],
       strategies=[_s(_API, "T1", ["image.generate"], note="via Leonardo REST v2")]),
]

# =====================================================================
# VIDEO (15)
# =====================================================================
VIDEO = [
    _p("seedance", "Seedance", "video", "B", "volcengine",
       auth=["api_key"], creds=_key_credential("Volcengine Ark key, or fal/Replicate/Higgsfield key"),
       base_url="https://ark.cn-beijing.volces.com/api/v3", job_model="async_poll",
       pricing="per_second", region=True,
       status_reason="Official path is Volcengine Ark (CN/SG-centric).",
       docs="https://docs.volcengine.com/docs/ark?lang=zh",
       models=[{"id": "doubao-seedance-1-5-pro", "capability": "video"}],
       strategies=[_s(_AGGR, "T1", ["video.generate"], note="fal/Replicate/Higgsfield — desktop default"),
                   _s(_API, "T1", ["video.generate"], note="Volcengine Ark — CN/enterprise")]),
    _p("kling", "Kling", "video", "A", "kling",
       auth=["api_key"], creds=_key_credential("kling.ai dev platform key"),
       base_url="https://api-singapore.klingai.com", job_model="async_poll",
       pricing="per_generation", region=True,
       docs="https://app.klingai.com/global/dev",
       models=[{"id": "kling-2.5", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate", "video.i2v"])]),
    _p("runway", "Runway", "video", "A", "runway",
       auth=["api_key"], creds=_key_credential("api.dev.runwayml.com key"),
       base_url="https://api.dev.runwayml.com", job_model="async_poll",
       pricing="per_second", docs="https://docs.dev.runwayml.com",
       models=[{"id": "gen4-turbo", "capability": "video"},
               {"id": "gen3a-turbo", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate", "video.i2v"])]),
    _p("luma", "Luma Dream Machine", "video", "A", "luma",
       auth=["api_key"], creds=_key_credential("api.lumalabs.ai key"),
       base_url="https://api.lumalabs.ai/dream-machine", job_model="async_poll",
       pricing="per_generation", docs="https://docs.lumalabs.ai",
       models=[{"id": "ray-2", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate", "video.i2v"])]),
    _p("veo", "Google Veo", "video", "A", "google",
       auth=["api_key"], creds=_key_credential("shared Google credential"),
       job_model="async_poll", pricing="per_second", region=True,
       notes="Via Gemini API / Vertex — shared Google credential.",
       models=[{"id": "veo-3.1", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate", "video.i2v"])]),
    _p("firefly-video", "Adobe Firefly Video", "video", "B", "adobe",
       auth=["oauth_server"], creds=[{"name": "client_id", "secret": False, "required": True,
                                      "hint": "Firefly Services client ID"},
                                     {"name": "client_secret", "secret": True, "required": True,
                                      "hint": "Firefly Services client secret"}],
       job_model="async_poll", pricing="per_credit",
       tos="https://www.adobe.com/legal/terms.html",
       status_reason="Enterprise S2S OAuth credential (shared with Firefly image).",
       models=[{"id": "firefly-video-1", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate"], note="S2S OAuth"),
                   _s(_ASSIST, "T2", ["video.generate"], connection={"launch_url": "https://firefly.adobe.com"}, **_T2)]),
    _p("ltx", "LTX Studio", "video", "B", "ltx",
       auth=["api_key"], creds=_key_credential("api.ltx.video key"),
       base_url="https://api.ltx.video", job_model="async_poll", pricing="per_second",
       tos="https://static.lightricks.com/legal/ltxstudio-terms-of-use.pdf",
       status_reason="LTX-2 API is credit-based; Studio UI itself is enterprise.",
       models=[{"id": "ltx-2", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate"], note="credits"),
                   _s(_ASSIST, "T2", ["video.generate"], connection={"launch_url": "https://ltx.studio"}, **_T2)]),
    _p("wan", "Wan", "video", "A", "dashscope",
       auth=["api_key"], creds=_key_credential("Alibaba Model Studio key"),
       job_model="async_poll", pricing="per_generation", region=True,
       notes="Regional-pinned DashScope key; also via aggregators.",
       models=[{"id": "wan3.0-video", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate", "video.i2v"]),
                   _s(_AGGR, "T1", ["video.generate"], note="fal/Replicate",
                      tos_url="https://www.alibabacloud.com/help/en/model-studio/",
                      tos_note="Aggregator listing authorized by the model vendor; official channel is DashScope")]),
    _p("juan-prime", "Juan Prime", "video", "E", "dashscope",
       status_reason="No such product — resolves to Wan 3.0 Prime (DashScope).",
       notes="Ambiguous name; aliases to wan3.0-video-prime.",
       models=[{"id": "wan3.0-video-prime", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate"], note="resolved → Wan Prime")],
       aliases=["wan-prime", "wan3.0-video-prime"]),
    _p("miniax", "Miniax", "video", "E", "minimax",
       auth=["api_key"], creds=_key_credential("platform.minimax.io key"),
       base_url="https://api.minimax.io", job_model="async_poll", pricing="per_second",
       status_reason="No product named Miniax — resolves to MiniMax (Hailuo).",
       models=[{"id": "minimax-hailuo-02", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate"], note="resolved → MiniMax")],
       aliases=["minimax", "hailuo"]),
    _p("happy-horse", "Happy Horse Video", "video", "C", "higgsfield",
       status_reason="Alibaba ATH model; only authorized path is the Higgsfield workflow.",
       notes="Uses the Higgsfield credential.",
       models=[{"id": "happyhorse-1.0", "capability": "video"}],
       strategies=[_s(_AGGR, "T1", ["video.generate"], note="Higgsfield AlibabaVideo workflow",
                      tos_url="https://higgsfield.ai/terms-of-use-agreement",
                      tos_note="Only authorized path found — Higgsfield partner workflow, not a first-party API")]),
    _p("synthesia", "Synthesia", "video", "B", "synthesia",
       auth=["api_key"], creds=_key_credential("api.synthesia.io key"),
       base_url="https://api.synthesia.io", job_model="async_poll", pricing="subscription",
       tos="https://www.synthesia.io/legal/user-terms-of-service",
       status_reason="Official videos API; Creator+ plans. Avatar-video niche.",
       models=[{"id": "synthesia-avatar", "capability": "video.avatar"}],
       strategies=[_s(_API, "T1", ["video.avatar"], note="plan-gated"),
                   _s(_ASSIST, "T2", ["video.generate"], connection={"launch_url": "https://www.synthesia.io"}, **_T2)]),
    _p("heygen", "HeyGen", "video", "A", "heygen",
       auth=["api_key"], creds=_key_credential("api.heygen.com key"),
       base_url="https://api.heygen.com", job_model="async_poll", pricing="per_credit",
       docs="https://docs.heygen.com",
       models=[{"id": "heygen-avatar4", "capability": "video.avatar"}],
       strategies=[_s(_API, "T1", ["video.avatar", "video.translate"])]),
    _p("invideo", "Invideo AI", "video", "B", "invideo",
       auth=["api_key", "oauth"], creds=_key_credential("api.invideo.io key (Business)"),
       base_url="https://api.invideo.io", job_model="async_poll", pricing="subscription",
       status_reason="Business-plan API; official MCP + CLI also available.",
       docs="https://agents.invideo.io",
       strategies=[_s(_API, "T1", ["video.generate"], note="Business plan"),
                   _s(_MCP, "T1", ["video.generate", "video.edit"], note="official MCP mcp.invideo.io"),
                   _s(_DESK, "T1", ["video.generate"], note="official CLI"),
                   _s(_ASSIST, "T2", ["video.generate"], connection={"launch_url": "https://invideo.io"}, **_T2)]),
    _p("higgsfield", "Higgsfield", "video", "A", "higgsfield",
       auth=["api_key"], creds=_key_credential("api.higgsfield.ai key"),
       base_url="https://api.higgsfield.ai", job_model="async_poll",
       pricing="per_credit", docs="https://docs.higgsfield.ai",
       notes="Provider AND authorized aggregator (Seedance, Kling, Wan, Happy Horse).",
       models=[{"id": "soul-2", "capability": "video"},
               {"id": "cinema-studio", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate", "video.i2v"], note="request_id/status_url/cancel_url")]),
]

# =====================================================================
# AUDIO & VOICE (15)
# =====================================================================
AUDIO = [
    _p("elevenlabs", "ElevenLabs", "audio", "A", "elevenlabs",
       auth=["api_key"], creds=_key_credential("xi-api-key"),
       base_url="https://api.elevenlabs.io", job_model="sync", pricing="per_character",
       docs="https://elevenlabs.io/docs", privacy="Sends text + voice refs to ElevenLabs.",
       notes="Voice cloning carries its own consent flow — mirrored in our consent design.",
       models=[{"id": "eleven-v3", "capability": "audio.tts"},
               {"id": "eleven-multilingual-v2", "capability": "audio.tts"}],
       strategies=[_s(_API, "T1", ["audio.tts", "audio.stt", "audio.dub",
                                    "audio.clone", "audio.music"])]),
    _p("suno", "Suno", "audio", "D", "suno",
       auth=["oauth"], creds=[],
       base_url="https://api.suno.com/mcp", pricing="per_credit",
       docs="https://suno.com/mcp",
       status_reason="No public API — official MCP connector only.",
       notes="MCP is the sanctioned channel; outputs land in your Suno library → assisted import.",
       strategies=[_s(_MCP, "T1", ["audio.music"], note="official MCP, OAuth, account credits"),
                   _s(_ASSIST, "T2", ["audio.music"], connection={"launch_url": "https://suno.com"}, **_T2)]),
    _p("udio", "Udio", "audio", "D", "udio",
       status_reason="No public API; downloads disabled since Oct 2025 — no import path.",
       notes="Walled garden post-UMG deal; output personal/non-commercial. Monitor only.",
       strategies=[_s(_INFO, "T4", [], automation="prohibited",
                      interaction="all", note="informational only",
                      tos_url="https://help.udio.com/en/articles/10756277-udio-public-api",
                      tos_note="No public API; downloads disabled 2025-10-29 under the UMG deal — no import path")]),
    _p("hume", "Hume AI / Octave", "audio", "A", "hume",
       auth=["api_key"], creds=_key_credential("api.hume.ai key"),
       base_url="https://api.hume.ai", job_model="sync", pricing="per_character",
       docs="https://dev.hume.ai",
       models=[{"id": "octave-tts", "capability": "audio.tts"},
               {"id": "evi", "capability": "audio.voice"}],
       strategies=[_s(_API, "T1", ["audio.tts", "audio.emotion"])]),
    _p("cartesia", "Cartesia / Sonic", "audio", "A", "cartesia",
       auth=["api_key"], creds=_key_credential("api.cartesia.ai key"),
       base_url="https://api.cartesia.ai", job_model="sync", pricing="per_credit",
       docs="https://docs.cartesia.ai",
       models=[{"id": "sonic-3", "capability": "audio.tts"}],
       strategies=[_s(_API, "T1", ["audio.tts", "audio.clone", "audio.stream"])]),
    _p("murf", "Murf.ai", "audio", "A", "murf",
       auth=["api_key"], creds=_key_credential("api.murf.ai key"),
       base_url="https://api.murf.ai", job_model="sync", pricing="per_character",
       models=[{"id": "falcon", "capability": "audio.tts"},
               {"id": "gen2", "capability": "audio.tts"}],
       strategies=[_s(_API, "T1", ["audio.tts", "audio.stt", "audio.dub"])]),
    _p("speechify", "Speechify Studio", "audio", "A", "speechify",
       auth=["api_key"], creds=_key_credential("api.speechify.ai key"),
       base_url="https://api.speechify.ai", job_model="sync", pricing="per_character",
       models=[{"id": "simba-english", "capability": "audio.tts"}],
       strategies=[_s(_API, "T1", ["audio.tts", "audio.stream"])]),
    _p("wellsaid", "WellSaid Labs", "audio", "B", "wellsaid",
       auth=["api_key"], creds=_key_credential("X-Api-Key (sales plan)"),
       base_url="https://api.wellsaidlabs.com", job_model="async_poll", pricing="subscription",
       status_reason="Enterprise voice — trial then sales plan.",
       strategies=[_s(_API, "T1", ["audio.tts"], note="plan-gated"),
                   _s(_ASSIST, "T2", ["audio.tts"],
                      tos_url="https://www.wellsaidlabs.com/",
                      tos_note="Enterprise product — terms at account signup; no public self-serve API", **_T2)]),
    _p("descript", "Descript", "audio", "B", "descript",
       auth=["api_key"], creds=_key_credential("descriptapi.com token"),
       base_url="https://descriptapi.com/v1", job_model="async_poll", pricing="subscription",
       status_reason="Import/edit/publish jobs API — media export not yet supported.",
       notes="Post-production automation, not TTS generation.",
       docs="https://docs.descriptapi.com",
       strategies=[_s(_API, "T1", ["media.import", "media.edit", "media.publish"],
                      note="jobs API"),
                   _s(_MCP, "T1", ["media.edit"], note="official Descript MCP"),
                   _s(_DESK, "T1", ["media.edit"], note="official CLI"),
                   _s(_IMPEXP, "T3", ["media.export"], **_T3)]),
    _p("fish", "Fish Audio", "audio", "A", "fish",
       auth=["api_key"], creds=_key_credential("api.fish.audio key"),
       base_url="https://api.fish.audio", job_model="sync", pricing="per_character",
       models=[{"id": "s2.1-pro", "capability": "audio.tts"}],
       strategies=[_s(_API, "T1", ["audio.tts", "audio.clone", "audio.stream"])]),
    _p("dupdub", "DupDub", "audio", "A", "dupdub",
       auth=["api_key"], creds=_key_credential("openapi.dupdub.com Bearer key"),
       base_url="https://openapi.dupdub.com", job_model="sync", pricing="per_credit",
       models=[{"id": "dupdub-tts", "capability": "audio.tts"}],
       strategies=[_s(_API, "T1", ["audio.tts", "audio.clone", "audio.dub",
                                    "audio.transcribe"])]),
    _p("taskagi", "TaskAGI", "audio", "C", "taskagi",
       auth=["api_key"], creds=_key_credential("TaskAGI platform key"),
       base_url="https://taskagi.net", job_model="sync", pricing="subscription",
       status_reason="Real API but small vendor — verify stability before use.",
       strategies=[_s(_API, "T1", ["audio.tts"], note="unverified stability")]),
    _p("lovo", "Lovo.ai", "audio", "A", "lovo",
       auth=["api_key"], creds=_key_credential("api.genny.lovo.ai X-API-KEY"),
       base_url="https://api.genny.lovo.ai", job_model="async_poll", pricing="per_character",
       models=[{"id": "genny", "capability": "audio.tts"}],
       strategies=[_s(_API, "T1", ["audio.tts"])]),
    _p("resemble", "Resemble AI", "audio", "A", "resemble",
       auth=["api_key"], creds=_key_credential("Resemble API token"),
       base_url="https://app.resemble.ai/api/v2", job_model="sync", pricing="per_character",
       notes="Cloning consent workflow aligns with our consent design.",
       models=[{"id": "resemble-v2", "capability": "audio.tts"}],
       strategies=[_s(_API, "T1", ["audio.tts", "audio.clone", "audio.stream",
                                    "audio.detect"])]),
    _p("ttsmaker", "TTSMaker", "audio", "B", "ttsmaker",
       auth=["api_key"], creds=_key_credential("pro.ttsmaker.com token"),
       base_url="https://pro.ttsmaker.com", job_model="async_poll", pricing="per_character",
       tos="https://ttsmaker.com/terms-of-service",
       status_reason="API requires Pro/Studio tier; CN free API paused.",
       strategies=[_s(_API, "T1", ["audio.tts"], note="plan-gated"),
                   _s(_ASSIST, "T2", ["audio.tts"], connection={"launch_url": "https://ttsmaker.com"}, **_T2)]),
]

PROVIDERS: list[dict] = TEXT + IMAGES + VIDEO + AUDIO

# ------------------------------------------------------------------
# lookups
# ------------------------------------------------------------------
_BY_KEY = {p["key"]: p for p in PROVIDERS}
_ACCOUNT_OF = {p["key"]: p["account"] for p in PROVIDERS}


def all_providers() -> list[dict]:
    return PROVIDERS


def get_provider(key: str) -> dict | None:
    return _BY_KEY.get(key)


def account_of(key: str) -> str | None:
    return _ACCOUNT_OF.get(key)


def accounts() -> dict[str, list[str]]:
    """account -> provider keys sharing it (normalization map)."""
    out: dict[str, list[str]] = {}
    for p in PROVIDERS:
        out.setdefault(p["account"], []).append(p["key"])
    return out


def catalog_count() -> int:
    return len(PROVIDERS)


def validate_catalog() -> list[str]:
    """Structural integrity check (C1.5 §2/§3): unique strategy ids, valid
    method/tier/automation enums, method-consistent tiers, mandatory
    tos_evidence on non-official paths, ≥1 usable strategy per provider."""
    errs: list[str] = []
    seen_ids: set[str] = set()
    for p in PROVIDERS:
        # Providers exist for informational completeness too — Udio
        # legitimately has only prohibited/monitor paths (§7).
        if not p["strategies"]:
            errs.append(f"{p['key']}: no strategies recorded")
        for s in p["strategies"]:
            sid = s.get("strategy_id") or "?"
            if sid in seen_ids:
                errs.append(f"{p['key']}: duplicate strategy_id {sid}")
            seen_ids.add(sid)
            if s["method"] not in METHODS:
                errs.append(f"{sid}: unknown method {s['method']}")
            prohibited = s["automation_permission"] == "prohibited"
            if prohibited and s["tier"] != "T4":
                errs.append(f"{sid}: prohibited records must be T4 "
                            "(informational refusal only)")
            elif not prohibited and s["tier"] not in METHOD_TIERS.get(s["method"], ()):
                errs.append(f"{sid}: method {s['method']} cannot claim "
                            f"tier {s['tier']}")
            if s["automation_permission"] not in AUTOMATION_PERMS:
                errs.append(f"{sid}: bad automation_permission "
                            f"{s['automation_permission']}")
            if s["user_interaction"] not in USER_INTERACTIONS:
                errs.append(f"{sid}: bad user_interaction "
                            f"{s['user_interaction']}")
            if s["method"] != "official_api":
                ev = s.get("tos_evidence") or {}
                if not ev.get("url") or not ev.get("checked"):
                    errs.append(f"{sid}: tos_evidence needs url+checked "
                                "on non-official_api strategies")
        cli = p.get("cli")
        if cli is not None:
            exes = cli.get("executables") or []
            plats = cli.get("platforms") or []
            if not exes:
                errs.append(f"{p['key']}: cli record needs executables")
            for name in exes:
                if (not isinstance(name, str) or not name or
                        "/" in name or "\\" in name or
                        name in (".", "..")):
                    errs.append(f"{p['key']}: unsafe cli executable name "
                                f"{name!r}")
            if not plats or any(pl not in ("windows", "linux", "macos")
                                for pl in plats):
                errs.append(f"{p['key']}: cli record needs valid platforms")
            if not (cli.get("docs_url") or "").startswith("https://"):
                errs.append(f"{p['key']}: cli record needs https docs_url "
                            "evidence")
            for d in cli.get("install_hint_dirs") or []:
                if (not isinstance(d, str) or not d or "\x00" in d or
                        ".." in PurePosixPath(d.replace("\\", "/")).parts):
                    errs.append(f"{p['key']}: unsafe cli install_hint_dir "
                                f"{d!r}")
        # C3-D1 — every image-capability model must carry an explicit
        # editing declaration; absent = structurally incomplete.
        for m in p.get("models") or []:
            mid = m.get("id") or "?"
            cap = m.get("capability") or ""
            if not cap.startswith("image"):
                continue
            ed = m.get("editing")
            if not isinstance(ed, dict):
                errs.append(f"{p['key']}/{mid}: image model missing "
                            "'editing' capability declaration")
                continue
            for f in ("img2img", "mask_inpaint", "outpaint",
                      "identity_reference", "reference_weights"):
                if not isinstance(ed.get(f), bool):
                    errs.append(f"{p['key']}/{mid}: editing.{f} must be "
                                "a boolean")
            refs = ed.get("max_reference_images")
            if not isinstance(refs, int) or isinstance(refs, bool) or refs < 0:
                errs.append(f"{p['key']}/{mid}: editing."
                            "max_reference_images must be an int ≥ 0")
                refs = 0
            if refs and not any(ed.get(f) for f in (
                    "img2img", "mask_inpaint", "outpaint",
                    "identity_reference")):
                errs.append(f"{p['key']}/{mid}: editing declares "
                            "reference images but no capability that "
                            "accepts them")
            if ed.get("identity_reference") and not refs:
                errs.append(f"{p['key']}/{mid}: identity_reference "
                            "requires max_reference_images ≥ 1")
            if ed.get("reference_weights") and not refs:
                errs.append(f"{p['key']}/{mid}: reference_weights "
                            "requires max_reference_images ≥ 1")
            if not isinstance(ed.get("note", ""), str):
                errs.append(f"{p['key']}/{mid}: editing.note must be "
                            "a string")
    return errs
