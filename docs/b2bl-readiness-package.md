# B2b-L — Controlled DeepSeek Live Validation: Readiness & Approval Package

**Status: PREPARATION ONLY.** No live request is authorized, made, or
queued. No real credential was accessed. No approval artifacts were
created. C4's 47 human checks remain NOT RUN; Issue #96 remains OPEN;
C5 untouched.

## 1. Baseline

| Item | Value |
|---|---|
| Current main | `bc9e539` (B2b-P #165 merge) + this prep branch |
| B2b-P reviewed head / merge | `a7cd1d1` / `bc9e539` |
| Post-merge CI `44619684648` | 6/6 success |
| Migration head | `0026_a7` (unchanged) |
| This branch adds | `request_fingerprint` binding (arm approval), rehearsal suite `test_b2bl_rehearsal.py` (10/10), fingerprint surfacing in `status_report` + readiness UI |

## 2. Verified DeepSeek API contract (re-checked this session)

Sources: `api-docs.deepseek.com/api/create-chat-completion`,
`/quick_start/pricing`, `/quick_start/rate_limit`,
`/quick_start/error_codes` — fetched <today>. **No contract changes
since B2b-P.**

| Item | Verified |
|---|---|
| Base URL | `https://api.deepseek.com` |
| Endpoint | `POST /chat/completions` |
| Model IDs | `deepseek-flash`, `deepseek-v4-pro` — **`deepseek-flash` is current**, served by `DeepSeek-V4.1-Flash-1017`. No substitution needed. (Legacy `deepseek-v4-flash*` names retired but still accepted — unused.) |
| Auth | `Authorization: Bearer <key>` |
| Thinking disable | `thinking: {"type": "disabled"}` — docs confirm omission → thinking **enabled**; explicit object takes precedence over `reasoning_effort` |
| Streaming | `stream: false` |
| Output cap | `max_tokens` 1…393216; default 8K non-thinking |
| Response schema | `choices[].message.content`; `reasoning_content` present only in thinking mode |
| Usage | `usage.prompt_tokens`, `usage.completion_tokens`, `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens` |
| 402 | "Insufficient Balance — you have run out of balance" — pre-processing refusal |
| Rate limits | concurrency-based: flash 2500, pro 500; exceeded → HTTP 429. Irrelevant at 1 request |
| Keep-alive | non-streaming may emit empty lines (httpx handles); server closes connections after 10 min pre-inference — our 60 s total timeout is far inside |
| Context | 1M tokens both models |

Newly-noted unused surface: `user_id` (isolation tag), vision/file
content parts, Responses/Anthropic/FIM APIs, server tools — the frozen
request uses none of them.

## 3. The frozen lighthouse request (canonical)

The prompt already fixed in `docs/b2b-live-validation-proposal.md` is
kept — "Write one sentence about a lighthouse." — per instruction to
prefer the existing approved prompt.

```json
{
  "provider_key": "deepseek",
  "model_id": "deepseek-flash",
  "endpoint": "https://api.deepseek.com/chat/completions",
  "method": "POST",
  "request_body": {
    "model": "deepseek-flash",
    "messages": [
      {"role": "user", "content": "Write one sentence about a lighthouse."}
    ],
    "max_tokens": 32,
    "stream": false,
    "thinking": {"type": "disabled"}
  },
  "retry_policy": "none — single request, no resubmission",
  "max_cost_usd": "0.05",
  "authorization": {
    "project_id": "<project-id>",
    "credential_ref": "deepseek",
    "scope": "single_request — one completion, non-streaming"
  }
}
```

**Request fingerprint** = SHA-256 over the canonical JSON above
(`live_permit.request_fingerprint`). It is **project-scoped** — the
`project_id` is part of it, so the value is computed per project and
is revealed to the owner by the arm-refusal message (`Expected
request_fingerprint: …`) and, post-arm, in `live-status`. Any change
to any field — prompt, model, endpoint, thinking, stream, token cap,
cost cap, project, credential ref — produces a different fingerprint
and invalidates approvals naming the old one. Secret material is never
part of the fingerprint.

Response assessment (per instruction): the validation judges the actual
response — HTTP status, `choices[0].message.content`,
`finish_reason`, `usage` — not a literal "READY" match; minor
formatting differences are not failure evidence.

## 4. Bounded worst-case cost

| Component | Bound | Peak rate | Worst cost |
|---|---|---|---|
| Input | prompt is FIXED ≤ 500 chars ≈ ≤ 500 tokens absolute worst (1 tok/char) | $0.30/1M (cache-miss) | $0.00015 |
| Output | `max_tokens` = 32 hard cap | $1.20/1M | $0.0000384 |
| **Total** | | | **≈ $0.0002** |

The $0.05 reservation is ~250× worst case — a **fail-safe bound, not
an estimate**. DeepSeek documents **no** input-token cap parameter
(context limit is implicit); input is therefore bounded by the fixed
prompt, not a parameter.

**DISCLOSURE — no provider-side cap:** DeepSeek offers no per-request
charge limit; $0.05 is an internal reservation only. If the provider
misbehaved or billed differently, real exposure is the account balance
itself — the owner's operational control is keeping that balance
small. The reservation never claims to be an externally enforced cap.

## 5. Credential entry — owner instructions

In the installed app: **Settings → Providers → DeepSeek** → enter key
→ Save. The key is written to the DPAPI vault
(`<data_dir>/credentials.vault`, CurrentUser scope) — never returned
to the frontend, never displayed after save, replace/delete supported,
excluded from backups and any diagnostics. Saving enables nothing;
no validation call is fired on save (none exists in code).

## 6. Owner approval (first gate) — `validate_arm`

Owner writes `%APPDATA%…\<data_dir>\owner\arm-approval.json`:

```json
{
  "approve": "validate_arm",
  "provider_key": "deepseek",
  "model_id": "deepseek-flash",
  "project_id": "<project-id>",
  "request_fingerprint": "<from refusal message or live-status>",
  "not_after": "<ISO-8601 expiry, ≤ ~1 h>"
}
```

Then the owner (or operator on their behalf) commits
`cloud.live.validate_arm` with payload `{credential_ref: "deepseek",
prompt: "Write one sentence about a lighthouse.", max_output_tokens:
32, max_cost_usd: "0.05"}`. `arm()` refuses unless every check passes:
permit free, prompt ≤500 chars, tokens ≤64, cost ≤$0.05, credential
configured (metadata), full admission chain (kill/enablement/model
grant/evidence freshness/caps/concurrency), approval file valid,
provider/model/project match, **and `request_fingerprint` match**.

Preview for the owner — everything visible before approval:
- `live-status` report + LiveGatePanel readiness block: credential
  configured, model, mock status, armed/none, ≤$0.05, BLOCKED.
- Audit behavior: command rows + txn `audit_json` record
  provider/model/ref/prompt_len/fingerprints — never prompt body or
  secrets.
- Security limits: no retries, 10 s connect/60 s total timeouts,
  2 MiB response cap, DNS-pinned, no redirects, no proxies.
- Expiry: `not_after` lapses → approval dead.
- Approval fingerprint: `request_fingerprint` (arm) →
  `permit_fingerprint` (exec).

## 7. Execution approval (second gate) — `validate_run`

After arming, owner reads `permit_fingerprint` from `live-status`
(readiness block shows it) and writes `owner/exec-approval.json`:

```json
{
  "approve": "validate_run",
  "permit_fingerprint": "<from live-status>",
  "not_after": "<ISO-8601 expiry>"
}
```

Then commits `cloud.live.validate_run` (empty payload). Verified
against the actual implementation — all 12 properties:

| # | Property | Implementation |
|---|---|---|
| 1 | Owner approval required | `arm-approval.json` scope+fields+fingerprint+expiry |
| 2 | Exec approval separately required | `exec-approval.json`, checked FIRST in `consume()` |
| 3 | Both bound to same request fingerprint | arm binds `request_fingerprint` directly; exec binds `permit_fingerprint` which covers the stored `request_fingerprint` — transitively the same immutable request |
| 4 | One operation only | single armed permit at a time; consumed on dispatch |
| 5 | Approvals expire | `not_after` enforced on both |
| 6 | Replays rejected | consumed → `no_permit`; command idempotency replays without re-dispatch |
| 7 | Model/prompt change invalidates | fingerprint mismatch at arm; post-arm tamper → `consume` field compare + `permit_fingerprint` mismatch |
| 8 | Credential replacement | permit binds `credential_ref` — ref change refuses; **rotation of the VALUE under the same ref does not invalidate** (owner approves the ref, not the bytes). For B2b-L the key is entered once — noted as accepted semantics, not a gap |
| 9 | Budget changes revalidated | admission re-runs at dispatch against CURRENT budgets |
| 10 | Non-transferable | `project_id` inside fingerprint + job project match |
| 11 | Atomic consumption | `consume()` inside the `spend.admit` transaction |
| 12 | No concurrent duplicate | single armed + `concurrency` refusal on open reservation |

## 8. Budget reservation & billing flow

Preflight → reserve → authorize → dispatch → reconcile → audit.
Reservation opens COMMITTED before transport exists (atomic with
permit consume). Global + per-provider caps enforced ($0.75 / $0.15 /
$0.10/request; permit ≤ $0.05). Exactly one request; no retry, no
follow-up, no discovery/balance call — none exist in code.

- **Success + usage** → settle at catalog peak rates; usage recorded
  (input/output/cache-split when present).
- **Confirmed refusal** (400/401/402/403/404/422/429 — documented
  pre-processing) → release. 402 is released *only* because the docs
  establish it as an empty-balance pre-processing refusal — the rule
  does NOT extend to ambiguous payment/transport outcomes (they hold).
- **Ambiguous** (5xx, timeout, reset, malformed 2xx, missing usage) →
  reservation stays `open`, billing status uncertain, no retry,
  `spend.reconcile` required after owner inspects the provider
  console.

## 9. Live text-transport security review — COMPLETE, NOT BLOCKED

`live_transport.py` implements a real, security-reviewed text
transport (C2 test battery covers every control):

| Control | Implementation |
|---|---|
| Destination allowlist | `egress_allowed(provider_key, url)` — spec-registered base+path only |
| DNS validation | `resolve_public` preflight; **socket pins to the validated IP literal** (`_ValidatedBackend`) — TOCTOU-proof, hostname never dialed |
| TLS + SNI | `create_ssl_context(verify=True, trust_env=False)`; cert check + SNI bound to the allowlisted hostname via httpcore `start_tls` |
| Redirects | 3xx → `LiveTransportRefused`, never followed, credential never forwarded |
| Proxy/env | `trust_env=False` everywhere; no proxy parameter exists |
| Timeouts | connect 10 s, total 60 s (deepseek `LIVE_TIMEOUTS`) via `asyncio.wait_for` |
| Request bound | permit prompt ≤ 500 chars — body inherently tiny |
| Response bound | `_BoundedStream` 2 MiB cap |
| Auth before network | admission + reservation COMMIT precede transport construction |
| Credential timing | `vault.get_fields` only inside the boundary, after committed admission |
| Secret redaction | provider error body never stored; audit carries metadata only |
| Cancellation | sync contract — `cancel()` is `UnsupportedRemoteOp` by design |
| Retries | `retries=0` in the pool; no retry code path exists |
| Dependency drift | `_assert_pool_internals` refuses construction if httpx/httpcore internals change |

**Verdict: a safe, reviewed real DeepSeek text transport EXISTS.**
B2b-L is NOT blocked on item 7 grounds. The image-artifact
`stream_pinned` path remains unused per Issue #96 — untouched.

## 10. Issue #96 boundary assessment

#96 is OPEN and unaffected: it tracks resolved-IP filtering for live
**image-artifact retrieval** (`stream_pinned`), which stays disabled.
The B2b-L request is a text completion response — no artifact URL,
no fetch path — so #96's boundary is never approached. No change to
SSRF validation, redirect, TLS, proxy, or destination rules.

## 11. Offline rehearsal results

`tests/test_b2bl_rehearsal.py` — **10/10 PASS**. All dispatches via
`httpx.MockTransport`; an autouse fixture replaces
`_SecureHTTPTransport` with a raiser — ANY real-transport construction
fails the suite outright. Sequence test asserts the full ordered flow
(preflight→fingerprint→arm→exec→reserve→admit→one request→parse→
usage→settle→audit→consume, credential read exactly once inside the
boundary, no prompt/secret in audit); refusal tests cover replay,
expiry, changed-request, missing fingerprint, cancel-absence,
timeout-hold, 402-release, and restart durability.

Companion evidence: `test_b2bp_failure_matrix.py` 35/35,
`test_c2b2a_validation_permit.py` 38 pass/2 skip — all re-run green
after the fingerprint change.

## 12. Stop-condition checklist (all enforced in code)

Any of these refuses before dispatch: missing/invalid owner or exec
approval, missing fingerprint, missing credential, unverified
model/endpoint (catalog is the allowlist), fingerprint change
(mismatch refusal), pricing unverifiable (missing catalog entry →
admission refuses), reservation failure, expired approval, consumed
permit, transport not approved (`EgressRefused` on unmarked
transports), #96 boundary, any background/retry need, any secret
exposure path, or build mismatch (fingerprints change with spec).
Post-dispatch ambiguity → hold + report, never a second request.

## 13. Sanitized evidence template (for the future run)

| Field | Capture |
|---|---|
| Build/SHA | installed exe SHA256 + source SHA |
| Timestamp | UTC ISO-8601 |
| Provider/model | `deepseek` / `deepseek-flash` |
| Request fingerprint | sha256 (canonical request) |
| Permit fingerprint | sha256 (armed record) |
| Approval IDs | arm/exec file names + scopes (no secrets) |
| Reservation | job_id + reserved micro-USD |
| Dispatch count | must be 1 |
| HTTP status | if available |
| Provider request id | response `id` field if present |
| Latency | ms |
| Completion | `finish_reason`, outcome class |
| Usage | prompt/completion/cache-split tokens |
| Calculated cost | micro-USD at catalog peak rates |
| Settlement | settled / released / held-open |
| Permit state | consumed_at, job binding |
| Logs | redacted — no keys, no Authorization values, no prompt body |
| Discrepancies | free text |

## 14. Remaining blockers

- Owner must enter the real credential (their action, unassistable).
- Owner must write both approval files (their action).
- A successful HTTP 200 alone is NOT acceptance — authorization,
  accounting, and audit invariants must all pass.
- Real-provider behavior (latency, balance edge cases) is unproven
  by design until the one authorized request.

## 15. Exact future execution-approval request

To execute, the owner must approve **B2b-L explicitly**, covering:
one (1) `POST /chat/completions` to `api.deepseek.com`, model
`deepseek-flash`, the frozen prompt above, `max_tokens` 32,
`stream` false, `thinking` disabled, ≤ $0.05 internal reservation,
owner-configured credential, owner-written arm + exec approval files.
Nothing else is requested — no follow-ups, no retries, no further
providers, no media, no tools.

## 16. Package contents

- `docs/b2b-deepseek-compatibility.md` (B2b-P, re-verified §2)
- `docs/b2b-preparation-report.md` (B2b-P gates/budget/boundary)
- `docs/b2b-live-validation-proposal.md` (prior procedure, superseded
  by this package's §3/§6/§7)
- `tests/test_b2bl_rehearsal.py` (the 20-check rehearsal)
- `tests/test_b2bp_failure_matrix.py` (the 30-item matrix)
