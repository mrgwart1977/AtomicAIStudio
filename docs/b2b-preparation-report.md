# B2b-P — DeepSeek secure-integration preparation report

Scope: **preparation + mock validation only.** No live DeepSeek call,
no real credential access, no provider spend, no production inference,
no artifact retrieval, no automatic retries. C4's 47 human checks are
untouched (all NOT RUN). C5 unstarted. Issue #96 remains OPEN.

## P.0 — Baseline

| Item | Value |
|---|---|
| Branch | `feat/b2b-p-deepseek` off main `4f7e1d2` (C4 tester-guide merge) |
| Migration head | `0026_a7` (unchanged) |
| Pre-existing DeepSeek code | `catalog.py` (provider+models+peak pricing), `rest_providers.py` (`DEEPSEEK_CHAT_SPEC`, `OpenAICompatChatProvider`), `rest_simulated.py` (fixture), `live_gate.py`, `live_permit.py` (B2a permit), `live_transport.py`, `cloud_rest.py` adapter |
| Prior stage | B2a built the single-use permit: `deepseek`/`deepseek-flash`, $0.05 cap, fingerprint-bound owner exec approval |
| Issue #96 | OPEN — DNS+resolved-IP filtering required before live image-artifact retrieval. Existing text-API path already filters; the tracked item is not closed by this stage |

Changes this branch (all preparation-scoped):
- `rest_providers.py`: `thinking: {"type": "disabled"}` added to
  `DEEPSEEK_CHAT_SPEC.submit_extra` (DeepSeek defaults to thinking);
  `402 → PAYMENT_REQUIRED/none_confirmed` map entry + enum member.
- `live_gate.py`: `status_report()` gains a read-only `permit` summary
  (armed fields + consumed count — prompt never exposed).
- `types.ts` / `LiveGatePanel.tsx`: readiness surface for the new
  report fields; heading updated to "preparation (B2b-P)".
- `tests/test_b2bp_failure_matrix.py`: the 30-item matrix (35 tests).

## P.2 — Credential security (existing vault verified)

Mechanism: Windows DPAPI `CryptProtectData`/`CryptUnprotectData`,
CurrentUser scope → `<data_dir>/credentials.vault` (base64 blobs per
provider/field). Verified against every requirement:

| # | Requirement | Evidence |
|---|---|---|
| 1 | User enters key in-app | `PUT /api/providers/{key}/credential` + `ProvidersPage.tsx` |
| 2 | Encrypted at rest | DPAPI; `set_fields` encrypts before write |
| 3–4 | Never returned/displayed | `GET credential` returns `status()` metadata only — fields, set_at, readable — never values |
| 5 | Configured/not-configured only | `CredentialVault.status()` |
| 6 | Replace + delete | `PUT` overwrites; `DELETE` removes |
| 7 | Not in SQLite settings | vault is a separate app-level file |
| 8 | Excluded from backups | `backup.py` strips secret-shaped keys (`credential|secret|token|password|api_key|apikey|private_key`); vault never packaged |
| 9 | Excluded from diagnostics | **no diagnostic-bundle/crash-report feature exists** — documented honestly, not claimed as tested |
| 10–11 | Never in logs/audit | matrix test 29 asserts secret absent from logs; `test_audit_minimization_no_prompt_or_secrets` |
| 12 | Not in crash reports | same as 9 — no such feature exists |
| 13 | Config ⇒ not execution | dispatch needs all of §P.4; config alone grants nothing |
| 14 | Project switching can't leak | values only leave the vault inside `live_dispatch`, post-admission |
| 15 | Fail closed | non-Windows/unreadable → `VaultUnavailable`; no plaintext fallback |

All credential tests run with `RecordingVault` + synthetic secrets.

## P.4 — Authorization matrix (every layer must pass)

| Layer | Mechanism | Refusal code |
|---|---|---|
| Global kill switch | `cloud.live.disable_all` toggle | `killed` |
| Provider enablement | audited `cloud.live.enable` opt-in | `not_enabled` |
| Model grant | enablement binds provider+model | `model_not_granted` |
| Credential readability | vault metadata check (never reads value) | `credential_*` |
| Per-request cap | $0.10 | `request_over_cap` |
| Provider exposure cap | $0.15 | `provider_cap` |
| Global exposure cap | $0.75 | `global_cap` |
| Single-flight | one open reservation per provider | `concurrency` |
| Owner arm approval | `owner/arm-approval.json` + `validate_arm` | `owner_approval:arm` |
| Armed permit | single atomic record, ≤$0.05/≤64 tok/≤500 chars | `no_permit`, `mismatch:*` |
| Exec approval | `owner/exec-approval.json` fingerprint-bound to armed permit | `owner_approval:exec` |
| Approval expiry | `not_after` honored | `owner_approval:*` |
| Single-use | consume is atomic with spend admission | `no_permit` on replay |
| Uncertain prior | open reservation blocks even re-arm | `uncertain_prior_dispatch` / `concurrency` |
| Spend admission | `spend.admit` COMMIT before transport exists | `budget_*` |
| Network boundary | post-admission DNS/IP filter, no redirects | `dns`, `redirect`, `private_ip` |

A configured credential satisfies **none** of these alone. No shipped
UI control can arm or run — arming/running are audited commands +
owner filesystem approvals only.

## P.5 — Budget / accounting-state matrix

| Scenario | Reservation lifecycle |
|---|---|
| Admission OK | `open` row COMMITTED before transport construction |
| Succeeded + usage | `settled` at actual micro-USD (catalog peak rates) |
| Succeeded, usage missing/malformed | `usage_unavailable` — stays `open`, never fabricated |
| Confirmed refusal (4xx mapped) | `released` |
| Ambiguous (5xx/timeout/malformed 2xx) | stays `open` for explicit reconcile — never resubmitted |
| Insufficient budget / over cap | refused at admission — no row |
| Duplicate settlement | idempotent — `settle` on settled row is a no-op |
| Restart with open reservation | row persists; re-arm refused `concurrency` |
| Project switch | reservations are global-scoped — no bypass |
| Cancellation | no DeepSeek cancel path documented; post-dispatch nothing to cancel |

## P.6 — Failure matrix results

`tests/test_b2bp_failure_matrix.py` — **35/35 PASS**, all mock
transports + synthetic secrets. Each of the 30 required scenarios maps
to a test (`test_01`…`test_30` + 5 contract assertions); per-test
assertions cover refusal code, whether the credential was read,
whether a socket could exist, reservation state, and recovery path.
Highlights: nothing sends before admission (19); ambiguous outcomes
hold + block re-dispatch (8–12, 24); the swapped-model tamper is
caught at admission `model_not_granted` (26); concurrent competition
settles on a single flight (30); secret never reaches logs (29);
`thinking`/`stream`/`max_tokens`/Bearer/URL shape asserted (contract).

## P.7 — Network / security boundary

- The only path a text-API request can take is
  `live_gate.dispatch_admission` → `live_dispatch`. `RestRemoteProvider`
  refuses `transport=None` — no default client can be constructed;
  only `httpx.MockTransport` or the hardened `LiveHTTPTransport`
  wrapper are accepted.
- `LiveHTTPTransport`/`_SecureHTTPTransport`: DNS resolves first —
  loopback/private/link-local/multicast/reserved/unspecified rejected;
  redirects refused (never followed); `trust_env=False` (no proxy/
  netrc/env); TLS verify on; per-provider timeouts; 2 MiB body cap.
- Text-API transport is a separate concern from artifact-download
  transport (image `stream_pinned` remains disabled per #96).
- No fallback transport path exists; nothing auto-activates. No
  background auth probes or balance checks — zero calls until the one
  authorized dispatch.
- **Issue #96 remains OPEN** — untouched by this stage.

## P.8 — UI readiness

`LiveGatePanel` now shows a **DeepSeek validation readiness** block
(read-only): credential configured/not-configured, selected model,
mock-validation status (labeled *simulated*, never "verified live"),
authorization/permit state from the new `permit` status field,
proposed ≤$0.05 reservation, and **Live dispatch: BLOCKED**. No
execution control exists — enable/kill toggles remain Stage-A audited
opt-in flags, and the panel states plainly *"No live dispatch exists
in this build."* Credential entry stays on `ProvidersPage`
(write-only). No secret value ever enters frontend state.

## P.9 — Regression evidence

- New matrix: **35/35** pass.
- Security/provider suites (`c2_credentials`, `c2_live_gate`,
  `c2_rest`, `c2_spend`, `c2b1_live_transport`, `c2_cloud_mock`,
  `c2b2a_validation_permit`): **325 pass, 3 skip** — zero regressions
  from the `thinking`/402/status-report changes.
- Full backend suite, frontend tests, tauri-check, packaging +
  installed validation: see PR body / CI evidence.

## Stop conditions honored

No live calls; no real-credential access; no spend; no inference;
no artifact retrieval; no retries; no credential disclosure; no gate
bypass; C4's 47 checks untouched (NOT RUN); no C5; no release.
