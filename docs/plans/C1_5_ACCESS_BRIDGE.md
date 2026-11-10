# Program C · Milestone C1.5 — Universal AI Access Bridge

Status: planning-only (C0 amendment). Inserted between C1 and C2–C6 as an
optional shared integration layer. No M-series or C-series identifiers are
changed; C1.5 depends on C1 (registry + credential vault exist first).

Purpose: maximum *legitimate* provider coverage. A provider is never excluded
solely for lacking a public REST API — but no access method is ever claimed
that the provider's terms do not permit. Technical feasibility is never
treated as permission.

---

## 1. Access methods (taxonomy)

| Method | Kind | Automation ceiling |
|---|---|---|
| `official_api` | Provider's own REST/GraphQL | Unattended (within rate/cost gates) |
| `aggregator_api` | Authorized third-party gateway (fal, Replicate, OpenRouter, Higgsfield-as-gateway, Cloudflare Workers AI) | Unattended |
| `mcp_connector` | Official MCP server / tool connector (Suno, OpenArt, Descript, invideo) | Agent-mediated; still unattended-capable but session-shaped |
| `browser_authorized` | Documented provider-sanctioned browser actions ONLY (official extension, documented deeplink/prefill, explicit ToS grant) | User-initiated only; narrowest tier |
| `desktop_plugin` | Official desktop CLI/plugin/scripting/local endpoint (invideo CLI, Descript CLI), documented file-exchange | Per tool's documented scope |
| `assisted_workflow` | ADE prepares prompt/assets → opens site → **user** submits → user downloads → ADE imports | Never unattended; the user performs the generation step |
| `import_export` | Clipboard / file-picker / watched import folder only | Manual exchange, no site orchestration |
| `external_info` | Catalog row + docs/status/links only | None |

A provider may carry several strategies (e.g. Suno = `mcp_connector` primary
+ `assisted_workflow` fallback). Each is evaluated on its own terms.

## 2. Capability tiers — never blur the line

| Tier | Name | What the UI may claim |
|---|---|---|
| **T1** | Direct programmatic generation | "Connected — Official API" / "— Authorized Connector" |
| **T2** | Assisted website workflow | "Assisted Workflow — you generate on their site" |
| **T3** | Manual import/export | "Manual Import — paste or drop files" |
| **T4** | Informational external access | "External — opens their website" |

An assisted or manual path is **never** labeled a full integration. The
adapter-kind axis gains `cloud-assisted`/`cloud-external` alongside
`cloud` so `adapter_kind="local"|"mock"|"cloud"` stays honest at the job
layer — only T1 strategies produce `cloud` jobs on the durable-job engine;
T2/T3 produce *import* jobs (`ops.asset.import`-style, with provenance
marking external origin + method).

## 3. AccessStrategy record (extends ProviderSpec)

```jsonc
{
  "strategy_id": "suno.mcp.v1",
  "provider_id": "suno",
  "method": "mcp_connector",               // §1 enum
  "capabilities": ["music.generate"],      // what THIS strategy can do
  "auth_method": "oauth_pkce|api_key|account_session|none",
  "user_interaction": "none|consent|submit|download|all",
  "automation_permission": "prohibited|user_initiated|assisted|unattended",
  "tos_evidence": {"url": "...", "note": "...", "checked": "2026-11-10"},
  "connection": {"base_url": "...", "region_field": false,
                 "requires": ["account"]},
  "input_formats":  ["prompt","lyrics"],
  "output_formats": ["mp3","wav","provider-library"],
  "settings_schema": { /* only genuinely supported options */ },
  "cost_implications": "per_credit|subscription|free|unknown",
  "reliability": "official channel; MCP session-shaped, no job ids",
  "last_verified": "2026-11-10",
  "tier": "T1|T2|T3|T4"
}
```

`tos_evidence` is mandatory for every non-`official_api` strategy — the
matrix cites what permits (or forbids) each path and when it was checked.
`automation_permission=prohibited` strategies exist in the registry only to
record *why* a tempting path is refused (e.g. Midjourney Discord wrappers).

## 4. Browser bridge (authorized interactions only)

Components (all user-visible, all opt-in):

- **PromptPreparer** — fills the provider's prompt template, puts it on the
  clipboard and/or into a documented prefill URL **where the provider
  documents one**. No DOM injection, no form filling by default.
- **SiteLauncher** — Tauri `shell.open` to the provider's real site in the
  user's own browser, with the prepared payload staged.
- **ImportWatcher** — watches ONLY a user-designated folder (default off;
  explicit per-provider toggle). New files matching expected types surface
  as import candidates — never monitors arbitrary browsing.
- **ClipboardImport** — explicit "Import from clipboard" button (image,
  text, file ref). No background clipboard snooping.
- **ProvenanceTagger** — imported outputs carry `origin=external`,
  provider, strategy_id, method, timestamp, and a user-confirmed flag;
  *unverified provenance is marked unverified*, not guessed.

`browser_authorized` escalation requires **documented provider permission**
(official extension, published deeplink spec, or explicit ToS grant) and is
still user-initiated per action. Hard prohibitions — no exceptions:

- No CAPTCHA/bot-detection bypass or rate-limit evasion.
- No session-cookie or token extraction; no credential scraping.
- No hidden endpoints or reverse-engineered private APIs.
- No UI automation the provider prohibits (check `tos_evidence` first).
- No silent monitoring of browser activity or page content.

## 5. Desktop integration

Official desktop surfaces only, each recorded as its own strategy:

- **Official CLIs** — invideo CLI (`agents.invideo.io`, OAuth), Descript CLI
  — spawned like local workers under proc_guard; real, documented interfaces.
- **Official plugins/scripting** — none confirmed in the catalog yet;
  investigated per provider before listing.
- **Local service endpoints** — provider-documented localhost APIs (none
  confirmed; placeholder strategy type for e.g. future local servers).
- **Documented file exchange** — SRT/VTT, .docx/.md, project formats (e.g.
  Descript publish → import media manually until their export API lands).
- **OS-level** — download-folder import, clipboard, file associations.

Prohibited here too: UI automation the provider forbids, credential/session
scraping, accessibility-tree scraping used to defeat access controls.

## 6. User-assisted workflow (canonical T2 flow)

```
ADE: pick provider + task → PromptPreparer fills template
    → "Copy prompt & open site" (clipboard + shell.open)
USER: pastes, configures, generates on provider site, downloads
ADE: ImportWatcher/file-picker surfaces the file → validate (ffprobe/
     image decode) → CAS register → link to project/shot → provenance
     (provider, strategy, user-confirmed, cost note if user reports it)
```

Variants: clipboard round-trip for text (QuillBot/Hemingway/Sudowrite),
file exchange for documents/media, provider-library-mediated for
MCP-shaped services that save to the user's own account (Suno, OpenArt).

## 7. Recommended strategy per restricted provider

| Provider | Primary | Fallback | Automation boundary |
|---|---|---|---|
| Midjourney | `assisted_workflow` (prompt→web→manual download→import) | `import_export` | **ToS prohibits automation** — no wrappers, no browser actions beyond opening the site; official MCP test excludes SaaS use |
| Sudowrite | `assisted_workflow` | `import_export` (.docx/.md) | No API; user generates in their editor |
| QuillBot | `import_export` (clipboard round-trip) | `assisted_workflow` | No API; ToS non-commercial + automation limits |
| Hemingway Plus | `import_export` (clipboard/.docx/.md) | `assisted_workflow` | No API |
| Udio | `external_info` | — | No API **and downloads disabled** (Oct 2025) — no import path; monitor only |
| Suno | `mcp_connector` (official, OAuth) | `assisted_workflow` | MCP is the sanctioned channel; songs land in user's Suno library → assisted import |
| Jasper | `official_api` (Business plan) | `assisted_workflow` | API exists — plan-gated |
| Anyword | `official_api` (Enterprise) | `assisted_workflow` | Plan-gated |
| Canva | `official_api` (Connect preview, private-app OAuth) | `assisted_workflow` (design export→import) | Preview API can't ship public apps; user-consent OAuth |
| Notion AI | `official_api` (Agent sessions, PAT) | `assisted_workflow` | Session-shaped, workspace-coupled |
| Writesonic | `aggregator_api`/Botsonic REST (Business) | `assisted_workflow` | Narrow surface |
| Copy.ai | `official_api` (workflow-run scoped) | `assisted_workflow` | Workflows defined in their UI first |
| TaskAGI | `official_api` | `external_info` | Small vendor — verify stability before T1 claim |
| OpenArt | `mcp_connector` (official, OAuth) | `assisted_workflow` | Sanctioned agent channel |
| Happy Horse | `aggregator_api` (Higgsfield `AlibabaVideo` workflow) | — | Only authorized path found |
| Descript | `official_api` + `desktop_plugin` (official CLI) | `import_export` | API can't export media yet → assisted export |
| invideo | `official_api` (Business) + `desktop_plugin`/`mcp` | `assisted_workflow` | Strong official CLI/MCP story |
| Seedance | `aggregator_api` (fal/Replicate/Higgsfield) | `official_api` (Volcengine Ark — CN/enterprise accounts) | Aggregator is the practical desktop path |
| Wan cloud / Wan Prime | `official_api` (DashScope, region-pinned) | `aggregator_api` | Region field required |
| Qwen Image | `official_api` (DashScope) | `aggregator_api` | — |
| LTX Studio | `official_api` (api.ltx.video credits) | `assisted_workflow` | Studio UI is enterprise — API is the path |
| Synthesia | `official_api` (Creator+) | `assisted_workflow` | Avatar-niche, plan-gated |
| HeyGen | `official_api` | `assisted_workflow` | — |
| TTSMaker | `official_api` (Pro/Studio) | `assisted_workflow` | Plan-gated |
| WellSaid | `official_api` (trial→sales) | `assisted_workflow` | Plan-gated |
| Meta Muse | `official_api` (dev.meta.ai) | — | New API — verify onboarding |
| All A-class rows | `official_api` | `aggregator_api` where the model also ships there (fal/Replicate/OpenRouter/Cloudflare) | Aggregator = alternate credential, still T1 |

## 8. UI — integration-method statuses & settings

Selector/statuses (mutually honest):

`Connected — Official API` · `Connected — Authorized Connector`
(MCP/aggregator) · `Connected — Approved Browser Integration` ·
`Assisted Workflow` · `Manual Import` · `Enterprise Access Required` ·
`Unavailable`.

Assisted-workflow settings pages expose only what applies: preferred
website/region, prompt template, output import directory, default asset
type, import behavior (auto-validate / ask-first), project destination,
provenance preferences. Generation parameters appear only where the
strategy's `settings_schema` says they're real — never decorative controls.

## 9. Security & privacy (bridge-specific)

Same standing rules as C (§PROGRAM_C §8), plus:

- Assisted flows move **user-visible artifacts only** — prompts the user
  sees, files the user produced, imports the user confirmed.
- Download-folder watching is off by default, per-provider opt-in, folder-
  scoped, and surfaces candidates — never auto-claims files.
- Provenance distinguishes `verified_remote` (T1 job ids) from
  `user_reported_external` (T2/T3 imports) — analytics never confuse them.
- Paid actions still ride the C cost gate: T1 paid calls need the estimate
  + approval; T2/T3 have no API spend but the *provider-side* charge is the
  user's own account action, recorded as informational only.
- `tos_evidence` is refreshable — a stale verification date demotes the
  strategy to "needs re-check" before it can be enabled.

## 10. Revised dependencies

```
C0 → C1 → C1.5
C1  → C2, C3, C4, C5            (official-API integrations)
C1.5→ assisted/browser/desktop strategies delivered inside C2–C6
C6  consumes C1.5 heavily (coverage for non-API providers)
C7  orchestrates only where the underlying strategy permits automation;
    manual/assisted tiers always remain user-driven
```
