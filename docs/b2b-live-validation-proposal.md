# B2b-P.10 — Proposed live validation procedure (B2b-L)

**This document is a procedure proposal only. It was NOT executed
under B2b-P and must never run automatically.** Nothing here is
scheduled, queued, or wired to any shipped control. Execution requires
explicit owner approval of the B2b-L stage plus the owner-approval
files described below.

> **SUPERSEDED — historical record.** This B2b-P.10 proposal
> originally specified the lighthouse prompt in the table below. The
> current candidate validation request is defined by
> `docs/b2bl-readiness-package.md` §3 — prompt `"Reply with exactly:
> ATOMIC_DEEPSEEK_OK"` — which also documents the hardened
> credential-generation binding, permit expiry, disarm, and pre-send
> checks added after this proposal was written. **Future operators
> must follow the readiness package**, not the prompt value here. This
> document is preserved unchanged as the procedure's history; its
> prompt and any fingerprint derived from it are superseded and not
> executable.

## Proposed single request

| Field | Value | Basis |
|---|---|---|
| Provider | `deepseek` | catalog entry |
| Model | `deepseek-flash` | current documented ID (compat doc) |
| Endpoint | `POST https://api.deepseek.com/chat/completions` | spec |
| Prompt | `"Write one sentence about a lighthouse."` | harmless fixed lighthouse prompt — no project data, no media, no tools, no web |
| `max_tokens` | `32` | documented output cap |
| `stream` | `false` | no streaming |
| `thinking` | `{"type": "disabled"}` | non-thinking contract |
| Retries | **0** | no retry path exists; none may be added |
| Reservation | **≤ USD $0.05** | ~1000× headroom over worst-case documented charge |

## Preconditions (all must hold or the run refuses)

1. Owner configures the DeepSeek API key **personally** through the
   app's provider credential UI (DPAPI vault — write-only, never
   displayed, never returned to the frontend, excluded from backups
   and diagnostics). Devin never sees or handles the key.
2. `docs/b2b-deepseek-compatibility.md` re-verified against the
   official docs within 48 h of execution.
3. Global kill switch OFF; provider `deepseek` enabled for
   `deepseek-flash` via the audited `cloud.live.enable` command.
4. Global budget `≥ $0.05` headroom (global cap $0.75, provider cap
   $0.15, per-request cap $0.10 — the $0.05 reservation passes all).
5. Owner writes `owner/arm-approval.json` (approves `validate_arm`
   for provider/model/project, `not_after` ≤ ~1 h).
6. `cloud.live.validate_arm` committed → single-use armed permit
   (permit cap $0.05, ≤64 output tokens, ≤500-char prompt).
7. Owner writes `owner/exec-approval.json` bound to the armed
   permit's fingerprint.

## Execution sequence (exactly one request)

1. `cloud.live.validate_run` committed → durable `validating` job row.
2. Post-commit dispatch (`live_dispatch`): admission re-check →
   $0.05 reservation COMMITTED → kill/enablement re-check →
   `vault.get_fields` (the only place the secret is read) →
   `LiveHTTPTransport` construction (DNS resolves to public IP —
   private/loopback/refused ranges rejected, redirects refused,
   `trust_env=False`, 10 s connect / 60 s total, 2 MiB body cap) →
   single POST → classification.
3. **No second request under any outcome.** Ambiguous outcomes
   (5xx, timeout, malformed 2xx) hold the reservation open and stop.

## Post-run reconciliation

| Outcome | Expected evidence | Reconcile |
|---|---|---|
| `succeeded` + usage | `usage.prompt_tokens`/`completion_tokens`; micro-USD computed at catalog peak rates | reservation `settled` at actual usage |
| `succeeded`, usage missing/malformed | `settlement_state: usage_unavailable` | reservation stays `open`; manual review — never fabricated as $0 |
| `failed` (400/401/402/403/404/422/429) | error code + `none_confirmed` | reservation `released` — nothing was charged |
| `ambiguous` (5xx/timeout/malformed) | `reservation: open`, job `unknown` | **holds open**; owner inspects provider billing console and reconciles explicitly; never resubmit |
| `refused` (any gate) | refusal code; job `failed` | nothing was sent; no reservation or released |

## Audit verification

- Command rows: `validate_arm`, `validate_run`, `cloud.live.enable`
  with actor + idempotency keys.
- Job row: `cloud.live.validate`, status terminal (`succeeded` /
  `failed` / `unknown`), error code if refused.
- Audit events carry provider/model/credential-ref/prompt-length/
  command-ids — **never** prompt body, never credential material.
- Reservation row state transitions visible in `spend_reservations`.
- The armed permit is consumed atomically at admission — replay
  refuses `no_permit`; both approval files must be removed/rotated
  by the owner after the run (approval invalidation).

## Hard limits

- One request total. Any repeat needs a new owner approval cycle.
- No balance-check or auth-probe requests — the single completion
  is itself the connectivity evidence.
- If the run is not executed within the approval window, approvals
  lapse (`not_after`) and must be re-issued.
