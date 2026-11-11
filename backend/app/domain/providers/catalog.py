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
       privacy="", notes="", aliases=()):
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
       models=[{"id": "gpt-5", "capability": "text"}, {"id": "gpt-5-mini", "capability": "text"}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream", "text.structured"])]),
    _p("anthropic", "Anthropic / Claude", "text", "A", "anthropic",
       auth=["api_key", "oauth"], creds=_key_credential("console.anthropic.com API key"),
       base_url="https://api.anthropic.com", job_model="sync", pricing="per_token",
       docs="https://docs.anthropic.com", tos="https://www.anthropic.com/legal/aup",
       privacy="Sends prompt + attached references to Anthropic (US).",
       notes="Claude subscription ≠ API access.",
       models=[{"id": "claude-opus-4-1", "capability": "text"},
               {"id": "claude-sonnet-4-5", "capability": "text"}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream", "text.structured", "text.reasoning"])]),
    _p("google-gemini", "Google / Gemini", "text", "A", "google",
       auth=["api_key"], creds=_key_credential("AI Studio or Vertex credential"),
       base_url="https://generativelanguage.googleapis.com", job_model="sync",
       pricing="per_token", region=True,
       docs="https://ai.google.dev/gemini-api/docs", tos="https://ai.google.dev/gemini-api/terms",
       privacy="Sends prompt + attached references to Google.",
       notes="Shared Google credential also covers Gemini Images and Veo.",
       models=[{"id": "gemini-2.5-pro", "capability": "text"},
               {"id": "gemini-2.5-flash", "capability": "text"}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream", "text.structured"])]),
    _p("deepseek", "DeepSeek", "text", "A", "deepseek",
       auth=["api_key"], creds=_key_credential("platform.deepseek.com API key"),
       base_url="https://api.deepseek.com", job_model="sync", pricing="per_token",
       docs="https://api-docs.deepseek.com",
       privacy="Sends prompt to DeepSeek — CN data residency.",
       notes="OpenAI-compatible; reasoning + chat models.",
       models=[{"id": "deepseek-chat", "capability": "text"},
               {"id": "deepseek-reasoner", "capability": "text"}],
       strategies=[_s(_API, "T1", ["text.generate", "text.stream", "text.reasoning"])]),
    _p("jasper", "Jasper", "text", "B", "jasper",
       auth=["api_key", "oauth"], creds=_key_credential("Jasper Business workspace key"),
       base_url="https://api.jasper.ai", job_model="sync", pricing="subscription",
       status_reason="Official API requires Jasper Business plan.",
       docs="https://developers.jasper.ai",
       privacy="Sends prompt to Jasper.",
       models=[{"id": "jasper-engine", "capability": "text"}],
       strategies=[_s(_API, "T1", ["text.generate"], note="Business plan only"),
                   _s(_ASSIST, "T2", ["text.generate"], **_T2)]),
    _p("copyai", "Copy.ai", "text", "B", "copyai",
       auth=["api_key"], creds=_key_credential("Copy.ai API key"),
       base_url="https://api.copy.ai", job_model="async_poll", pricing="subscription",
       status_reason="Workflows API — runs pre-built workflow IDs only.",
       docs="https://docs.copy.ai",
       notes="Cannot send arbitrary prompts; workflows defined in their UI.",
       strategies=[_s(_API, "T1", ["text.workflow"], note="workflow-scoped"),
                   _s(_ASSIST, "T2", ["text.generate"], **_T2)]),
    _p("writesonic", "Writesonic", "text", "C", "writesonic",
       auth=["api_key"], creds=_key_credential("Writesonic Business API key"),
       base_url="https://api.writesonic.com", pricing="subscription",
       tos="https://writesonic.com/legal/terms",
       status_reason="Botsonic/SEO APIs only — no creative-writing endpoint.",
       strategies=[_s(_API, "T1", ["text.chatbot"], note="Botsonic scope, not creative writing"),
                   _s(_ASSIST, "T2", ["text.generate"], **_T2)]),
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
                   _s(_ASSIST, "T2", ["text.generate"], **_T2)]),
    _p("anyword", "Anyword", "text", "B", "anyword",
       auth=["api_key"], creds=_key_credential("Anyword Enterprise API key"),
       job_model="sync", pricing="subscription",
       tos="https://www.anyword.com/terms",
       status_reason="Official API is Enterprise-plan only.",
       strategies=[_s(_API, "T1", ["text.generate"], note="Enterprise only"),
                   _s(_ASSIST, "T2", ["text.generate"], **_T2)]),
    _p("sudowrite", "Sudowrite", "text", "E", "sudowrite",
       tos="https://sudowrite.com/terms-and-conditions",
       status_reason="No public API — feature request open on their board.",
       strategies=[_s(_ASSIST, "T2", ["text.generate"], **_T2),
                   _s(_IMPEXP, "T3", ["text.import"], **_T3)]),
    _p("quillbot", "QuillBot", "text", "E", "quillbot",
       tos="https://quillbot.com/terms",
       status_reason="Officially no API (help center); ToS restricts automation.",
       strategies=[_s(_IMPEXP, "T3", ["text.roundtrip"], **_T3),
                   _s(_ASSIST, "T2", ["text.rewrite"], **_T2)]),
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
       models=[{"id": "grok-4", "capability": "text"}],
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
       models=[{"id": "gpt-image-1", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit"])]),
    _p("gemini-images", "Gemini Images / Nano Banana", "image", "A", "google",
       auth=["api_key"], creds=_key_credential("shared Google credential"),
       base_url="https://generativelanguage.googleapis.com", job_model="sync",
       pricing="per_image", region=True,
       notes="Uses the shared Google credential.",
       models=[{"id": "gemini-2.5-flash-image", "capability": "image"},
               {"id": "imagen-4", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit"])]),
    _p("bfl-flux", "FLUX / Black Forest Labs", "image", "A", "bfl",
       auth=["api_key"], creds=_key_credential("api.bfl.ai key"),
       base_url="https://api.bfl.ai", job_model="async_poll", pricing="per_image",
       docs="https://docs.bfl.ai",
       models=[{"id": "flux-pro-1.1", "capability": "image"},
               {"id": "flux-kontext", "capability": "image.edit"}],
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
       models=[{"id": "firefly-image-4", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate"], note="S2S OAuth scope"),
                   _s(_ASSIST, "T2", ["image.generate"], **_T2)]),
    _p("ideogram", "Ideogram", "image", "A", "ideogram",
       auth=["api_key"], creds=_key_credential("api.ideogram.ai key"),
       base_url="https://api.ideogram.ai", job_model="sync", pricing="per_image",
       docs="https://docs.ideogram.ai",
       models=[{"id": "ideogram-v3", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit", "image.remix"])]),
    _p("canva", "Canva AI", "image", "B", "canva",
       auth=["oauth"], creds=[{"name": "oauth_token", "secret": True, "required": True,
                               "hint": "Connect API OAuth token (private app)"}],
       base_url="https://api.canva.com", job_model="async_poll", pricing="per_credit",
       tos="https://www.canva.com/policies/canva-developer-terms/",
       status_reason="Connect API in preview — public apps can't pass review yet.",
       models=[{"id": "craft-1", "capability": "image"},
               {"id": "lucid-origin", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate"], note="preview API, private apps"),
                   _s(_ASSIST, "T2", ["image.export"], **_T2)]),
    _p("meta-ai", "Meta AI", "image", "B", "meta",
       auth=["api_key"], creds=_key_credential("dev.meta.ai key"),
       base_url="https://api.meta.ai/v1", job_model="sync", pricing="per_image",
       status_reason="Meta Model API (Muse) — new; onboarding unverified.",
       models=[{"id": "muse-image-1.0", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate"])]),
    _p("recraft", "Recraft", "image", "A", "recraft",
       auth=["api_key"], creds=_key_credential("api.recraft.ai key"),
       base_url="https://api.recraft.ai", job_model="sync", pricing="per_image",
       docs="https://www.recraft.ai/docs",
       models=[{"id": "recraft-v3", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate", "image.style"])]),
    _p("leonardo", "Leonardo AI", "image", "A", "leonardo",
       auth=["api_key"], creds=_key_credential("cloud.leonardo.ai key"),
       base_url="https://cloud.leonardo.ai/api/rest/v2", job_model="async_poll",
       pricing="per_credit", docs="https://docs.leonardo.ai",
       notes="Also serves Lucid Origin as model id 'lucid-origin'.",
       models=[{"id": "phoenix", "capability": "image"},
               {"id": "lucid-origin", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate", "image.canvas"])]),
    _p("openart", "OpenArt", "image", "C", "openart",
       auth=["oauth"], creds=[],
       base_url="https://mcp.openart.ai/mcp", pricing="per_credit",
       docs="https://openart.ai/mcp", tos="https://openart.ai/terms/",
       status_reason="Official MCP server + CLI — OAuth, no API key.",
       notes="MCP-marked models only; credits via your OpenArt account.",
       strategies=[_s(_MCP, "T1", ["image.generate", "video.generate"], note="official MCP"),
                   _s(_DESK, "T1", ["image.generate"], note="official CLI"),
                   _s(_ASSIST, "T2", ["image.generate"], **_T2)]),
    _p("freepik", "Freepik AI Suite", "image", "A", "freepik",
       auth=["api_key"], creds=_key_credential("x-freepik-api-key"),
       base_url="https://api.freepik.com", job_model="async_poll", pricing="per_credit",
       docs="https://docs.freepik.com",
       models=[{"id": "mystic", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit", "video.generate"])]),
    _p("grok-imagine", "Grok Imagine", "image", "A", "xai",
       auth=["api_key"], creds=_key_credential("shared xAI API key"),
       base_url="https://api.x.ai", job_model="sync", pricing="per_image",
       notes="Uses the shared xAI credential.",
       models=[{"id": "grok-imagine-image-2.0", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate", "video.generate"])]),
    _p("qwen-image", "Qwen Image", "image", "A", "dashscope",
       auth=["api_key"], creds=_key_credential("Alibaba Model Studio key"),
       base_url="https://dashscope-intl.aliyuncs.com", job_model="async_poll",
       pricing="per_image", region=True,
       notes="Region-pinned DashScope key (SG/BJ/TYO/HK).",
       models=[{"id": "qwen-image-3.0", "capability": "image"}],
       strategies=[_s(_API, "T1", ["image.generate", "image.edit"]),
                   _s(_AGGR, "T1", ["image.generate"], note="fal/Replicate",
                      tos_url="https://www.alibabacloud.com/help/en/model-studio/",
                      tos_note="Aggregator listing authorized by the model vendor; official channel is DashScope")]),
    _p("lucid-origin", "Lucid Origin", "image", "A", "leonardo",
       status_reason="A model on the Leonardo account — not a separate provider.",
       notes="Catalog alias of Leonardo; no separate credentials.",
       models=[{"id": "lucid-origin", "capability": "image"}],
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
                   _s(_ASSIST, "T2", ["video.generate"], **_T2)]),
    _p("ltx", "LTX Studio", "video", "B", "ltx",
       auth=["api_key"], creds=_key_credential("api.ltx.video key"),
       base_url="https://api.ltx.video", job_model="async_poll", pricing="per_second",
       tos="https://static.lightricks.com/legal/ltxstudio-terms-of-use.pdf",
       status_reason="LTX-2 API is credit-based; Studio UI itself is enterprise.",
       models=[{"id": "ltx-2", "capability": "video"}],
       strategies=[_s(_API, "T1", ["video.generate"], note="credits"),
                   _s(_ASSIST, "T2", ["video.generate"], **_T2)]),
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
                   _s(_ASSIST, "T2", ["video.generate"], **_T2)]),
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
                   _s(_ASSIST, "T2", ["video.generate"], **_T2)]),
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
                   _s(_ASSIST, "T2", ["audio.music"], **_T2)]),
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
                   _s(_ASSIST, "T2", ["audio.tts"], **_T2)]),
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
    return errs
