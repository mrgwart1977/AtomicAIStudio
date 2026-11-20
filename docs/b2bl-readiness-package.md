# B2b-L — Controlled DeepSeek Live Validation: Readiness & Approval Package

**Status: PREPARATION ONLY.** No live request is authorized, made, or
queued. No real credential was accessed. No approval artifacts were
created. C4's 47 human checks remain NOT RUN; Issue #96 remains OPEN;
C5 untouched.

## 1. Baseline

| Item | Value |
|---|---|
| Current main | `6ca7657` (B2b-L prep #166 merge) + this hardening branch |
| B2b-L prep reviewed head / merge | `aa2b1f4` / `6ca7657` |
| Post-merge CI `44637333059` | 6/6 success |
| Migration head | `0026_a7` (unchanged — no DB change; all state is vault JSON + AppSetting KV) |
| This branch adds | `credential_generation` binding (vault→canonical request→both approvals→permit→admission→atomic read→send-time probe), armed-permit expiry (`not_after`), `cloud.live.validate_disarm`, strict `api_key` selection, proven pre-dispatch reservation release, hardening suite `test_c2b3_credential_generation.py` (38 tests) |

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
    "credential_generation": "<vault generation at arm time>",
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
cost cap, project, credential ref, **credential generation** —
produces a different fingerprint and invalidates approvals naming the
old one. Secret material is never part of the fingerprint.

**Credential generation** is a random UUID minted by the DPAPI vault
on *every* write that stores material (`set_fields` — including
re-saving an identical value, and fresh after delete+re-create). It is
never derived from the secret or ciphertext — two consecutive saves of
the same value produce different generations. It is persisted with the
credential metadata and exposed by `vault.status()` / the credential
status API / `live-status` as **non-secret** metadata (same class as
`set_at`). Rotation semantics: **any credential write invalidates all
outstanding approvals** — deliberate fail-safe; the vault cannot prove
"same key" so it treats every write as a rotation. Empty or failed
writes do not rotate.

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
  "credential_generation": "<from credential status / live-status>",
  "request_fingerprint": "<from refusal message or live-status>",
  "not_after": "<ISO-8601 expiry, ≤ ~1 h>"
}
```

Then the owner (or operator on their behalf) commits
`cloud.live.validate_arm` with payload `{credential_ref: "deepseek",
prompt: "Write one sentence about a lighthouse.", max_output_tokens:
32, max_cost_usd: "0.05"}`. `arm()` refuses unless every check passes:
permit free, prompt ≤500 chars, tokens ≤64, cost ≤$0.05, credential
configured (metadata) **with a generation marker**, full admission
chain (kill/enablement/model grant/evidence freshness/caps/
concurrency), approval file valid, provider/model/project/
**credential_generation** match, **and `request_fingerprint` match**.

**Permit expiry:** the approval's `not_after` is carried verbatim onto
the armed permit record. Consumption after expiry refuses
(`permit_expired`) without consuming; a missing or malformed expiry on
the record fails closed (`permit_expiry` — legacy permits cannot
become valid by omission). Expiry is re-checked at dispatch admission
inside `consume()` and persists across restart (AppSetting KV).
Expired permits remain safely disarmable.

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
| 8 | Credential replacement/rotation | permit binds `credential_ref` **AND `credential_generation`** — ref change refuses; rotation (any vault write, delete+re-create, even same-value re-save) mints a new generation → arm approval, fingerprint, consume-compare, atomic read, and send-time probe ALL fail closed |
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
- **Proven pre-dispatch refusal** (kill/disable race, credential
  rotation, missing credential field — all raised BEFORE
  `provider.submit`, provably nothing sent) → `_refuse_predispatch`
  releases the reservation with an auditable `pre-dispatch refusal:
  <code>` reconcile note. A provable non-event never permanently
  consumes budget.
- **Ambiguous** (5xx, timeout, reset, malformed 2xx, missing usage,
  anything past the send boundary) → reservation stays `open`,
  billing status uncertain, no retry, `spend.reconcile` required
  after owner inspects the provider console.

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
| Credential timing | `vault.get_fields_with_generation` only inside the boundary, after committed admission — decrypted fields + generation read atomically under one vault lock; mismatch vs permit → `credential_rotated` refusal before transport |
| Credential staleness at send | generation probe inside `LiveHTTPTransport.handle_async_request` — rechecked immediately before the wire send; rotation → `credential_rotated` (`none_confirmed`) |
| Secret redaction | provider error body never stored; audit carries metadata only |
| Cancellation | sync contract — `cancel()` is `UnsupportedRemoteOp` by design |
| Retries | `retries=0` in the pool; no retry code path exists |
| Dependency drift | `_assert_pool_internals` refuses construction if httpx/httpcore internals change |

**Verdict: a safe, reviewed real DeepSeek text transport EXISTS.**
B2b-L is NOT blocked on item 7 grounds. The image-artifact
`stream_pinned` path remains unused per Issue #96 — untouched.

**Residual race — disclosed honestly:** generation is verified at
consume, again atomically at credential read, and once more inside the
transport immediately before the wire send. A rotation landing *during*
the in-flight request cannot be prevented without holding a vault lock
across network I/O (never done — the lock is only ever held for
in-memory state reads/mutations). That final window is bounded by the
60 s total timeout and fails safe either way: a rotated/revoked key
gets a definitive provider refusal (no charge); a still-valid old key
completes under the credential that was approved at the last passing
check. No design claim is made beyond this.

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

`tests/test_c2b3_credential_generation.py` — **38 PASS** (new
hardening suite, same zero-egress guard): all three rotation windows
refuse closed; delete+re-create and same-value re-save invalidate;
disarm voids/replays/stale-targets/consumed-targets deterministically
and re-arm succeeds; expiry carried/boundaried/restart-durable/
legacy-refused; strict `api_key` field; proven pre-dispatch release vs
uncertain hold; real-vault generation lifecycle (Windows DPAPI).

Companion evidence: `test_b2bp_failure_matrix.py`,
`test_c2b2a_validation_permit.py`, `test_c2b1_live_transport.py`,
`test_c2_live_gate.py`, `test_c2_credentials.py` — all green after
the hardening change (two tests updated where they pinned the OLD
pre-dispatch hold semantics — both now assert released + reason).

## 12. Disarm — `cloud.live.validate_disarm`

Audited command, payload `{permit_id, permit_fingerprint, reason?}`.
It grants **no** authority (de-authorizing only — no owner approval
file required, same class as `cloud.live.disable`). Targets the armed
permit by EXACT id + fingerprint: a mismatch or a stale target
refuses without touching the armed record; replay returns the voided
record idempotently; a consumed target refuses `already_consumed`.
The voided record is preserved as evidence; a new arm succeeds
immediately after. This closes the previously identified
stale-armed-permit deadlock (an unrunnable armed permit used to block
all future arms forever).

## 13. Stop-condition checklist (all enforced in code)

Any of these refuses before dispatch: missing/invalid owner or exec
approval, missing fingerprint, missing credential, **credential
generation mismatch (any rotation, delete+re-create, or re-save)**,
missing/invalid/expired permit `not_after`, unverified model/endpoint
(catalog is the allowlist), fingerprint change (mismatch refusal),
pricing unverifiable, reservation failure, expired approval, consumed
or voided permit, missing required `api_key` field, transport not
approved (`EgressRefused` on unmarked transports), #96 boundary, any
background/retry need, any secret exposure path, or build mismatch.
Post-dispatch ambiguity → hold + report, never a second request.

## 14. Multi-instance & legacy state

- **Single-instance:** `instance_lock.acquire_data_dir_lock(data_dir)`
  (OS byte-range lock, process lifetime, `main.py` startup) runs
  BEFORE the vault is constructed — a second backend exits before
  touching `credentials.vault`. Multi-process vault access is
  impossible; intra-process access is serialized by the vault's RLock.
- **Legacy vault entries** (pre-generation files): a generation is
  lazily minted on first load — ciphertext untouched. **Legacy armed
  permits** (no `credential_generation`/`not_after`): consume fails
  closed (`permit_expiry`); disarm clears them. Approvals written
  pre-hardening lack `credential_generation`/`request_fingerprint`
  coverage → refused (fail closed), none exist in production anyway.
- **Compatibility:** `request_fingerprint` values differ from
  pre-hardening for identical inputs (new canonical field) — no live
  fingerprints exist to invalidate. Migration head stays `0026_a7`.

## 15. Sanitized evidence template (for the future run)

| Field | Capture |
|---|---|
| Build/SHA | installed exe SHA256 + source SHA |
| Timestamp | UTC ISO-8601 |
| Provider/model | `deepseek` / `deepseek-flash` |
| Request fingerprint | sha256 (canonical request) |
| Permit fingerprint | sha256 (armed record) |
| Approval IDs | arm/exec file names + scopes (no secrets) |
| Credential generation | bound generation + vault status generation |
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

## 16. Remaining blockers

- Owner must enter the real credential (their action, unassistable).
- Owner must write both approval files (their action) — the arm
  approval now requires `credential_generation` +
  `request_fingerprint`, both surfaced in `live-status`.
- A successful HTTP 200 alone is NOT acceptance — authorization,
  accounting, and audit invariants must all pass.
- Real-provider behavior (latency, balance edge cases) is unproven
  by design until the one authorized request.
- Residual race: in-flight rotation cannot be preempted (§9) — the
  send-time probe narrows it to the wire send itself; disclosed, not
  claimed eliminated.

## 17. Exact future execution-approval request

To execute, the owner must approve **B2b-L explicitly**, covering:
one (1) `POST /chat/completions` to `api.deepseek.com`, model
`deepseek-flash`, the frozen prompt above, `max_tokens` 32,
`stream` false, `thinking` disabled, ≤ $0.05 internal reservation,
owner-configured credential, owner-written arm + exec approval files.
Nothing else is requested — no follow-ups, no retries, no further
providers, no media, no tools.

## 18. Package contents

- `docs/b2b-deepseek-compatibility.md` (B2b-P, re-verified §2)
- `docs/b2b-preparation-report.md` (B2b-P gates/budget/boundary)
- `docs/b2b-live-validation-proposal.md` (prior procedure, superseded
  by this package's §3/§6/§7)
- `tests/test_b2bl_rehearsal.py` (the 20-check rehearsal)
- `tests/test_b2bp_failure_matrix.py` (the 30-item matrix)
- `tests/test_c2b3_credential_generation.py` (the 38-test hardening
  suite: generation binding, disarm, expiry, strict field, accounting
  taxonomy, real-vault lifecycle)
