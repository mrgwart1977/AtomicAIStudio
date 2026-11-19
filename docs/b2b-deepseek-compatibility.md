# B2b-P.1 — DeepSeek API compatibility review

Public-documentation review only. **No authenticated or unauthenticated
request of any kind was made to `api.deepseek.com`** — no model
discovery, no balance check, no auth probe, no completion. Every claim
below cites the official docs; anything not documented is marked
UNVERIFIED, never assumed.

Sources (all fetched 2025-… — see review date below):
- `https://api-docs.deepseek.com/api/create-chat-completion`
- `https://api-docs.deepseek.com/quick_start/pricing`
- `https://api-docs.deepseek.com/quick_start/error_codes`
- `https://api-docs.deepseek.com/` (models & versioning pages)

Review date: 2026-02 — re-check immediately before any future live
validation; provider contracts drift.

## Verified contract

| Item | Documented value |
|---|---|
| Base URL | `https://api.deepseek.com` |
| Endpoint | `POST /chat/completions` (OpenAI-compatible) |
| Auth | `Authorization: Bearer <api_key>` header |
| Models (IDs) | `deepseek-flash`, `deepseek-v4-pro` |
| Model versions | `DeepSeek-V4.1-Flash-1017`, `DeepSeek-V4-Pro-0813` |
| Streaming | `stream` param; we always send `stream: false` |
| Output cap | `max_tokens` — documented range 1 … 393,216 |
| Usage reporting | `usage.prompt_tokens`, `usage.completion_tokens` |
| Response text | `choices[].message.content` |
| Thinking content | `choices[].message.reasoning_content` (thinking mode only) |

## Thinking / non-thinking behavior — the gap this stage fixed

DeepSeek documentation states that **thinking mode defaults to ENABLED
when the `thinking` parameter is omitted**. Non-thinking must be
requested explicitly with `thinking: {"type": "disabled"}` (or
`reasoning_effort: "none"`; the explicit `thinking` object takes
precedence when both are present).

**Gap found:** `OpenAICompatChatProvider` sent neither field — an
unmodified request would have run thinking mode, returning
`reasoning_content` and burning reasoning tokens we never asked for.
Both preparation targets (thinking disabled, 32 output tokens) would
have been silently violated.

**Fix (this branch):** `DEEPSEEK_CHAT_SPEC.submit_extra` now sends
`{"stream": False, "thinking": {"type": "disabled"}}`. The change is
spec-scoped — no other provider payload changes. Asserted in
`tests/test_b2bp_failure_matrix.py::TestB2bPRequestContract`.

## Pricing — catalog vs documented (peak rates, USD / 1M tokens)

| Model | Input cache-hit | Input cache-miss | Output | Catalog match |
|---|---|---|---|---|
| `deepseek-flash` | $0.006 | $0.30 | $1.20 | ✓ |
| `deepseek-v4-pro` | $0.044 | $1.32 | $3.96 | ✓ |

Docs note off-peak rates are half the peak values; the catalog
conservatively records PEAK rates for reservation math. Worst-case
bounded request (32 output tokens on `deepseek-flash`, generous input):
`32 × $1.20/1M + input × $0.30/1M` — well under the proposed $0.05
reservation. The $0.05 cap is ~1000× headroom over the theoretical
charge; it exists as a hard bound, not an estimate.

## Error codes — documented set and our classification

| HTTP | Meaning | Classified as | Charge evidence | Reservation |
|---|---|---|---|---|
| 400 | invalid format | validation | none_confirmed | released |
| 401 | authentication fails | authentication | none_confirmed | released |
| **402** | **insufficient balance** | **payment_required** | **none_confirmed** | **released** |
| 403 | (OpenAI-compat set) | forbidden | none_confirmed | released |
| 404 | not found | validation | none_confirmed | released |
| 408 | timeout | (transport) | unknown | **held open** |
| 409 | conflict | conflict | unknown | held open |
| 422 | unprocessable | validation | none_confirmed | released |
| 429 | rate limiting | rate_limited | none_confirmed | released |
| 500 | server error | server_error | unknown | **held open** |
| 503 | overloaded | server_error | unknown | **held open** |
| 504 | gateway timeout | server_error | unknown | **held open** |

**Gap found:** HTTP 402 had no explicit `_STATUS_MAP` entry and fell
through to `unknown` → hold. DeepSeek documents it as Insufficient
Balance — a pre-processing refusal on an empty account: confirmed
no-charge, released. Added `RemoteErrorKind.PAYMENT_REQUIRED` and the
`402 → (payment_required, none_confirmed)` map entry. Covered by
matrix test 06.

Design rule this preserves: an error is only `none_confirmed` when the
documentation establishes the refusal happens before processing. 5xx
and transport failures stay `unknown` forever — the request may have
been processed — so their reservations are held open for explicit
reconcile, never auto-released and never resubmitted.

## Rate limits & timeouts

DeepSeek documents 429 responses for rate limiting; no numeric limit
is published (UNVERIFIED — treated as arbitrary). Our bounded contract
issues exactly one request, no retries, so rate limiting manifests as
a clean `rate_limited` refusal (released reservation). Provider
timeouts enforced: connect 10 s / total 60 s
(`live_gate.LIVE_TIMEOUTS`), response body capped at 2 MiB.

## Preparation targets — confirmed against docs

| Target | Status |
|---|---|
| Provider DeepSeek / `deepseek-flash` | ✓ current documented ID — **no substitution needed** |
| `POST /chat/completions` | ✓ |
| Streaming disabled | ✓ `stream: false` sent explicitly |
| Thinking disabled | ✓ **now sent explicitly** (was missing) |
| Max output 32 tokens | ✓ within documented 1…393,216 |
| Zero automatic retries | ✓ enforced — no retry code path exists |
| $0.05 reservation | ✓ ≤ per-request cap $0.10 and permit cap $0.05 |

## Explicitly NOT verified (documentation cannot establish)

- That a real key authenticates — needs the owner's key + one live call.
- Real-world latency/availability.
- Actual `usage` field presence on a real response — the code treats
  missing usage as `usage_unavailable` (open reservation), so a
  surprise here fails safe.
