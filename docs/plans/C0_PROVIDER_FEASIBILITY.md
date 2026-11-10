# Program C · Phase C0 — Provider Feasibility Matrix

Status: C0 research, planning-only. Research date: 2026-11 · Classifications
must be re-verified at integration time — provider access terms change
frequently. This document covers catalog presence and access reality only;
no integration work is authorized by it.

## Classification legend

| Class | Meaning |
|---|---|
| **A** | Verified official API — public docs, self-serve or standard signup key |
| **B** | Restricted API — official API exists but gated (enterprise plan, sales call, preview, region limits, or narrow surface) |
| **C** | Authorized third-party/agent channel — official MCP server, sanctioned aggregator, or API-shaped access that is not the provider's own generation API |
| **D** | External workflow only — usable product, no legitimate programmatic path today |
| **E** | Unverified, unavailable, or prohibited (ToS bans automation / product ambiguous) |

---

## Text & writing (15)

| # | Catalog entry | Class | Access reality | Notes for C1+ |
|---|---|---|---|---|
| 1 | OpenAI / ChatGPT | **A** | `api.openai.com` — key auth, Responses/Chat APIs, streaming, tool calling | Primary C2 target. ChatGPT subscription ≠ API access; billing is per-token. |
| 2 | Anthropic / Claude | **A** | `api.anthropic.com` — key or OAuth, Messages API, streaming | Primary C2 target. Claude subscription ≠ API access. |
| 3 | Google / Gemini | **A** | Gemini API (AI Studio key) + Vertex AI; streaming, structured output | Normalizes Gemini text, Gemini Images/Nano Banana, and Veo into one Google provider account. |
| 4 | DeepSeek | **A** | `api.deepseek.com` — OpenAI-compatible, key auth | Low cost; reasoning + chat models; data-residency considerations for prompts. |
| 5 | Jasper | **B** | `api.jasper.ai/v1` — official REST, **Business plan only**, workspace key or OAuth PKCE | Marketing-oriented engine (model-agnostic inside); fine for copy tasks, not a general writer model. |
| 6 | Copy.ai | **B** | Workflows API — `api.copy.ai/api`, `x-copy-ai-api-key`; runs pre-built workflows by ID + webhooks | Workflow-scoped: cannot send an arbitrary prompt — the workflow is defined in their UI first. Fit: structured content pipelines, not free-form writing. |
| 7 | Writesonic | **C** | REST APIs exist for **Botsonic** (chatbot mgmt, business plan+) and SEO/analytics data; no first-class creative-writing endpoint | Catalog as available-but-narrow; creative writing would misuse the chatbot surface. Reassess if their API expands. |
| 8 | Writer | **B** | `api.writer.com/v1` — official API key, Palmyra chat/completions, structured outputs, knowledge graphs | Enterprise-grade; real API, pricing published ($0.60–$2.00/1M in). Good C2 secondary target. |
| 9 | Notion AI | **B** | Notion **Agent APIs** — session-based chat with Custom Agents or `notion_ai` personal agent via PAT; SSE streaming | Agent-shaped, not a completion endpoint. Could work for document-aware tasks but brings Notion workspace coupling. |
| 10 | Anyword | **B** | Official API, **Enterprise plan only** — performance prediction + generation + RAG | Marketing-performance niche; defer to C6. |
| 11 | Sudowrite | **E** | No public API (confirmed; feature request pending on their board) | Fiction-specialist tool — users want it, but no legitimate path. External-workflow guidance only. |
| 12 | QuillBot | **E** | Officially **no B2B/B2C API** (help center, June 2026); ToS restricts automation | Unofficial wrappers scrape the web app — prohibited approach, do not use. |
| 13 | Rytr | **A** | Self-serve API — `app.rytr.me/v1` (`/ryte` generate, `/languages`, `/tones`, `/use-cases`); 10k free chars, per-character billing | Real but template/use-case oriented; fits short-form copy tasks. |
| 14 | xAI / Grok | **A** | `api.x.ai/v1` — chat (OpenAI-compatible), plus Images + Imagine video | Single xAI account also serves Grok Imagine image/video entries. |
| 15 | Hemingway Editor Plus | **E** | No API (readability/rewrite product only) | External-workflow guidance only. |

## Images (15)

| # | Catalog entry | Class | Access reality | Notes for C3+ |
|---|---|---|---|---|
| 1 | Midjourney | **E** | **No official API**; ToS §1 prohibits automated access; closed MCP test explicitly excludes SaaS use | Do not integrate via wrappers — account-ban risk and ToS violation. Document as external workflow + monitor enterprise-API survey. |
| 2 | OpenAI Images | **A** | `gpt-image-*` via OpenAI Images API (`/v1/images/generations` + edits) | Same OpenAI provider/credential as text — normalize. |
| 3 | Gemini Images / Nano Banana | **A** | `gemini-2.5-flash-image` / Imagen via Gemini API + Vertex | Same Google provider/credential — normalize. |
| 4 | FLUX / Black Forest Labs | **A** | `api.bfl.ai` — official key, FLUX.1/2 pro, dev, kontext (edit), async task poll | Strong C3 priority: quality + transparent async job model. |
| 5 | Adobe Firefly | **B** | Firefly Services API — OAuth server-to-server, scoped per-enterprise credential; image + video + audio services | Real but enterprise-credentialed; verify self-serve tier availability at C3. |
| 6 | Ideogram | **A** | `api.ideogram.ai` — key auth, generate/edit/remix; strong text rendering | C3 priority. |
| 7 | Canva AI | **B** | Connect API image-generation job — `POST /v1/image-generations` (preview), OAuth `asset:write`, idempotency key built-in | Preview API: public apps cannot pass Canva review yet; CRAFT-1/Lucid Origin/Z-Image models. Viable private-app path today. |
| 8 | Meta AI | **B** | Meta Model API — `api.meta.ai/v1`, Muse Image (`muse-image-1.0`), OpenAI-Images-compatible, $0.01/image | New (Aug 2026); verify onboarding + rate limits at C3. |
| 9 | Recraft | **A** | `api.recraft.ai` — key auth, v3 model, style/vector/layout controls | C3 priority — design-oriented, unique style-id system. |
| 10 | Leonardo AI | **A** | `cloud.leonardo.ai/api/rest/v2` — key auth, generations + Phoenix/flux models + canvas APIs | Also serves **Lucid Origin** as `model: "lucid-origin"` — normalize. |
| 11 | OpenArt | **C** | Official **MCP server** (`mcp.openart.ai`, OAuth, no key) + internal suite API | Legit agent channel; aggregates other vendors' models (credit passthrough). Suitable as optional MCP-style provider. |
| 12 | Freepik AI Suite | **A** | `api.freepik.com` / `api.magnific.com` — `x-freepik-api-key`; Mystic, Flux, Seedream, Imagen, video, editing | Rich multi-model API; free €5 tier aids dev testing. |
| 13 | Grok Imagine | **A** | xAI `/v1/images/generations` + Imagine API — `grok-imagine-image-2.0`, $0.02/image, also video | Same xAI credential — normalize. |
| 14 | Qwen Image | **A** | Alibaba Model Studio/DashScope — `qwen-image-3.0(-pro)/2.1-*`, OpenAI-compatible + DashScope; **region-pinned keys** (SG/BJ/TYO/HK) | Official API; also available via fal/Replicate aggregators (C path). CN-region data considerations. |
| 15 | Lucid Origin | **A** (via Leonardo) | Leonardo's own model — `model: "lucid-origin"` in Leonardo REST v2; also on Canva preview + Cloudflare Workers AI | Not a separate account — a **model** on the Leonardo provider entry. |

## Video (15)

| # | Catalog entry | Class | Access reality | Notes for C4+ |
|---|---|---|---|---|
| 1 | Seedance | **B** | ByteDance via **Volcengine Ark** (official, async task API, CN/SG endpoints); also authorized aggregators (fal, Replicate, Higgsfield) | Official path requires Volcengine account; aggregator path is simpler for a desktop app — decide at C4. |
| 2 | Kling | **A** | Official dev platform `kling.ai/dev` — API key, `api-singapore.klingai.com` for non-CN; resource-package billing | Async tasks + callbacks + concurrency rules documented. C4 priority. |
| 3 | Runway | **A** | `api.dev.runwayml.com` — official key, Gen-4/Gen-3 tasks API, async | C4 priority; clean task/poll model. |
| 4 | Luma Dream Machine | **A** | `api.lumalabs.ai/dream-machine` — official key, async generation API | C4 priority. |
| 5 | Google Veo | **A** | Veo via Gemini API (`veo-*`) + Vertex AI — async LRO, per-second billing | Same Google credential — normalize. |
| 6 | Adobe Firefly Video | **B** | Firefly Services video endpoints — enterprise OAuth credential | Same Firefly Services account as images. |
| 7 | LTX Studio | **B** | `api.ltx.video` — LTX-2 API, credit-based per-second billing, async v2 jobs | Official; LTX *Studio* UI itself is enterprise — the API is the integration path. |
| 8 | Wan | **A** | Alibaba Model Studio — `wan3.0-video(-prime)` async API, `X-DashScope-Async: enable`, region-pinned keys | Cloud counterpart of our local Wan (M4.0) — distinct provider entry sharing a model family; also aggregators. |
| 9 | Juan Prime | **E → resolved** | No such product. Near-certain mishearing of **"Wan (3.0) Prime"** — `wan3.0-video-prime`, available on the same DashScope API | Catalog as an alias row pointing at Wan Prime; flag the ambiguous name in UI copy. |
| 10 | Miniax | **E → resolved** | No such product. Near-certain misspelling of **MiniMax (Hailuo)** — official `platform.minimax.io` API: MiniMax-H3 video family, async task API, pay-as-you-go | Classify the resolved provider **A**; catalog entry keeps "Miniax (MiniMax)" label honesty. |
| 11 | Happy Horse Video | **C** | Real model — Alibaba ATH "HappyHorse 1.0"; distributed via Fotor/Media.io and **Higgsfield API** (`AlibabaVideo · Happy Horse 1.0` workflow) | No direct official API found; legitimate path = authorized aggregator (Higgsfield). |
| 12 | Synthesia | **B** | `api.synthesia.io` — official videos API, API key; avatar/lip-sync focus, Creator+ plans | Avatar-video niche, not general T2V — label capability accurately. |
| 13 | HeyGen | **A** | `api.heygen.com` — official v2 API, key auth; avatar/TTS-video, photo-avatar, translate | Avatar-video niche; strong API. |
| 14 | Invideo AI | **B** | `api.invideo.io` — Business-plan API + official MCP server + CLI (`agents.invideo.io`) | Editor/assembly-oriented; generation is workflow-shaped. Verify scope fit at C4/C6. |
| 15 | Higgsfield | **A** | `api.higgsfield.ai` — official async API, `request_id`/`status_url`/`cancel_url`, failed jobs unbilled | Both a provider (Soul/Cinema Studio models) and authorized aggregator (Seedance, Kling, Wan, Happy Horse) — good C4 partner for restricted models. |

## Audio & voice (15)

| # | Catalog entry | Class | Access reality | Notes for C5+ |
|---|---|---|---|---|
| 1 | ElevenLabs | **A** | `api.elevenlabs.io` — key auth; TTS, STT, dubbing, voice clone (consent flow), music | C5 primary. Voice cloning has its own verification/consent requirements — mirror them in consent UX. |
| 2 | Suno | **D** | **No public API**; official **MCP connector** (`api.suno.com/mcp`, OAuth, uses account credits); partner API being explored (July 2026) | MCP path is legitimate but assistant-shaped. Wrappers violate ToS — do not use. Reassess at C5/C6. |
| 3 | Udio | **D** | **No public API** (help center explicit); post-UMG-settlement "walled garden" — downloads disabled Oct 2025 | Even external workflow is limited (no exports). Document honestly; monitor. |
| 4 | Hume AI / Octave | **A** | `api.hume.ai` — Octave TTS (emotion-directed), EVI, expression measurement | C5 target for emotive delivery. |
| 5 | Cartesia / Sonic | **A** | `api.cartesia.ai` — Sonic TTS, streaming WS, voice cloning | C5 target — low-latency streaming. |
| 6 | Murf.ai | **A** | `api.murf.ai` — key auth, Falcon/Gen2 TTS, STT beta, dubbing; free trial 100k chars + PAYG | C5 target — self-serve. |
| 7 | Speechify Studio | **A** | `api.speechify.ai` — `POST /v1/audio/speech` + WS streaming, simba-* models | C5 candidate. |
| 8 | WellSaid Labs | **B** | `api.wellsaidlabs.com` — `X-Api-Key`, streaming + async; trial → sales plan | Enterprise voice; plan-gated. |
| 9 | Descript | **B** | `descriptapi.com/v1` — token auth; import/edit/publish **jobs** (Underlord AI edits); media export not yet supported via API | Fit is post-production automation, not TTS generation — classify capability honestly (edit/dub workflow). |
| 10 | Fish Audio | **A** | `api.fish.audio` — key auth, `s2.1-pro` (+ free test model `s2.1-pro-free`), TTS + cloning + WS | C5 candidate — free tier helps testing. |
| 11 | DupDub | **A** | `openapi.dupdub.com` — TTS, voice cloning, dubbing, transcription, avatar; Bearer key | C5/C6 candidate. |
| 12 | TaskAGI | **C** | Platform API + HyperVoice TTS product (`taskagi.net`) — automation-platform-shaped, small vendor | Real API but niche; evaluate quality/stability before recommending. C6. |
| 13 | Lovo.ai | **A** | `api.genny.lovo.ai` — `X-API-KEY`, sync (90s) + async TTS, speakers/styles | C5 candidate. |
| 14 | Resemble AI | **A** | Official API — TTS, voice cloning (consent workflow), realtime WS, deepfake detect | C5 target — cloning consent flow aligns with our consent design. |
| 15 | TTSMaker | **B** | `pro.ttsmaker.com` API — token auth, voice list + create-order + quota APIs; **Pro/Studio tier required**; CN free API paused | Budget TTS; viable low-cost option. |

## Summary counts (audited 2026-11-10 — by catalog entry)

| Class | Count | Rows |
|---|---|---|
| A — verified official API | **32** | text 6 · images 10 · video 7 (Kling, Runway, Luma, Veo, Wan, HeyGen, Higgsfield) · audio 9 |
| B — restricted/gated API | **16** | text 5 · images 3 · video 5 · audio 3 |
| C — authorized third-party/agent channel | **4** | Writesonic, OpenArt, Happy Horse, TaskAGI |
| D — external workflow only | **2** | Suno, Udio |
| E — unavailable/prohibited | **6** | Sudowrite, QuillBot, Hemingway, Midjourney, Juan Prime*, Miniax* |
| **Total** | **60** | |

\*Juan Prime and Miniax are E-class **as named** (no such products) that resolve
to A-class providers (Wan 3.0 Prime on DashScope; MiniMax). Counting each row
at its *resolved provider's* class instead: **34 A / 16 B / 4 C / 2 D / 4 E** —
same 60 rows, two views. An earlier draft reported 30/19/4/3/6 (=62) which
double-counted the resolved pair; the figures above are the audited values.

## Provider normalization (shared accounts) — audited

| Catalog entries merged | Single provider account |
|---|---|
| Gemini text + Gemini Images/Nano Banana + Veo (3 rows) | **Google** (Gemini API / Vertex) |
| OpenAI text + OpenAI Images (2) | **OpenAI** |
| xAI Grok text + Grok Imagine (2) | **xAI** |
| Leonardo + Lucid Origin (2) | **Leonardo** (Lucid Origin is a model, not a provider) |
| Qwen Image + Wan + Juan Prime (3) | **Alibaba Model Studio / DashScope** (one key serves all) |
| Adobe Firefly + Firefly Video (2) | **Adobe Firefly Services** |
| Higgsfield + Happy Horse (2) | **Higgsfield** (Happy Horse rides its AlibabaVideo workflow) |

**60 catalog rows → 51 unique credential accounts**: 16 rows collapse into 7
shared accounts (Google, OpenAI, xAI, Leonardo, DashScope, Adobe, Higgsfield).
Earlier "53" under-counted the DashScope group and the Happy Horse→Higgsfield
routing; both are corrected here.

## Ambiguous-name resolutions (verified)

| Requested | Resolved to | Evidence |
|---|---|---|
| Juan Prime | Wan 3.0 Prime (`wan3.0-video-prime`, Alibaba) | No product named "Juan Prime" exists; Wan Prime matches phonetically and in modality |
| Miniax | MiniMax Hailuo (`platform.minimax.io`) | No product named "Miniax"; MiniMax is the major AI-video vendor matching the name |
| Happy Horse Video | HappyHorse 1.0 (Alibaba ATH) — via Higgsfield/aggregators | Real 2026 model; no first-party API — C class |
| TaskAGI | TaskAGI platform + HyperVoice TTS | Real but small vendor; automation-platform API |

## Key findings for C1 design

1. **Async-by-default is the norm**: Kling, Runway, Luma, Veo, Seedance, Wan, Higgsfield, MiniMax, BFL, Canva — nearly every video/image provider is submit→poll(or webhook)→download. The M4.1 remote-provider contract (durable submission intent, remote job id, unknown-outcome reconciliation) is the right shape already.
2. **Idempotency is inconsistent**: Canva has a real `idempotency_key`; xAI returns `request_id` for deferred results; Higgsfield gives `cancel_url`; many others have none — our durable-intent + never-silent-resubmit policy is mandatory.
3. **Billing models differ**: per-token (LLMs), per-image (xAI $0.02, Meta $0.01), per-second (Veo, LTX), per-generation/credits (Kling packs, Higgsfield, Canva AI credits, Leonardo tokens), subscription-with-allowance (Jasper, Anyword, Copy.ai workflows). Cost engine must normalize to estimated-USD with per-provider pricing tables.
4. **Region-pinned credentials**: DashScope (Alibaba) keys are region-bound (SG/BJ/TYO/HK); Volcengine Ark is CN-centric; Kling has separate CN/international endpoints — provider config needs a region field.
5. **MCP as a legitimate third channel**: Suno, OpenArt, Descript, invideo, Midjourney (closed test) all ship MCP servers. Our registry should treat "agent-channel provider" as a first-class access class, distinct from REST adapters.
6. **Hard E list**: Midjourney (ToS), Sudowrite, QuillBot, Hemingway (no API), Udio (walled garden, no exports). These get catalog rows with honest status — never simulated functionality.

## Access strategies (C1.5 amendment)

Every provider carries one or more **AccessStrategy** records — see
`C1_5_ACCESS_BRIDGE.md` for the schema, the four capability tiers
(T1 direct · T2 assisted · T3 manual import · T4 informational), and the
hard prohibition rules. Access class now maps to a *best strategy*, not a
verdict:

| Class | Best available strategy | Reachable via |
|---|---|---|
| A (32) | `official_api` (T1) | Unattended, within cost/rate gates; `aggregator_api` secondary where the model also ships on fal/Replicate/OpenRouter/Cloudflare |
| B (16) | `official_api` (T1) when plan/entitlement allows | Same provider, plan-gated; `assisted_workflow` (T2) fallback so the row is still *useful* on a lower plan |
| C (4) | `mcp_connector` / `aggregator_api` (T1) | Writesonic (narrow), OpenArt + Happy Horse + TaskAGI channels; Suno/Descript/invideo MCP/CLI upgrade D/B rows to T1 |
| D (2) | `assisted_workflow` (T2) | Suno (also T1 via official MCP), Udio (T4 only — no exports) |
| E (6) | `assisted_workflow` or `external_info` (T2/T3/T4) | Midjourney (T2 — ToS permits *user-driven* web use, never automation) · Sudowrite (T2) · QuillBot, Hemingway (T3 clipboard/file) · Juan Prime → Wan Prime, Miniax → MiniMax (resolve to A) |

Note: classes count *catalog entries*; strategies are per-provider-per-
capability, so one row can legitimately span tiers (Descript T1 edit-jobs +
T3 media-out; Suno T1-MCP generation + T2 library import).

**Coverage impact of the bridge**: without it, ~15 catalog rows (25%)
would be dead entries. With it, every row except Udio reaches at least a
useful T2/T3 workflow, and Udio is an honest T4 status pending any export
capability returning. Assisted/manual paths are import jobs with
`user_reported_external` provenance — never presented as API integration.
