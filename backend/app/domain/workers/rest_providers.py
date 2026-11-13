"""C2.0c+ — REST RemoteProvider implementations over injected transports.

Documented A-class text API contracts — each lifecycle implemented
exactly as documented, nothing invented:

- ``openai`` — OpenAI Responses API (``POST /v1/responses`` with
  ``background: true``): asynchronous submission returning a real remote
  operation id (``resp_*``), documented polling via
  ``GET /v1/responses/{id}``, documented cancel via
  ``POST /v1/responses/{id}/cancel`` (background responses only).
  https://developers.openai.com/api/docs/guides/background
- ``anthropic`` — Anthropic Messages API (``POST /v1/messages``):
  synchronous completion — the response body IS the finished message
  (``msg_*`` id, content, usage). No documented poll or cancel exists
  for single messages (the Batches API is a different surface).
  https://platform.claude.com/docs/en/api/messages/create
- ``google-gemini`` — Gemini ``generateContent``
  (``POST /v1beta/models/{model}:generateContent``): synchronous, model
  in the URL, ``x-goog-api-key`` header auth. Response carries
  candidates + usageMetadata and NO response id (remote_id stays None —
  honest, not fabricated). Batch generation is a different surface.
  https://ai.google.dev/api/generate-content
- ``deepseek`` — DeepSeek chat completions (``POST /chat/completions``):
  OpenAI-compatible synchronous shape, Bearer auth,
  ``choices[0].message.content`` + ``usage``. No documented poll/cancel.
  https://api-docs.deepseek.com/api/create-chat-completion
- ``xai`` — xAI chat completions (``POST /v1/chat/completions``):
  OpenAI-compatible synchronous shape, Bearer auth. Deferred-completion
  retrieval exists only for explicitly deferred requests — not this
  contract. https://docs.x.ai/developers/rest-api-reference/inference/chat-completions

HARD EGRESS BOUNDARY: every provider requires an explicitly injected
``httpx`` transport, and the ONLY sanctioned transport is
``httpx.MockTransport``. Passing None (which would let httpx build a
real network transport) or any live transport class raises
``EgressRefused`` — there is no production transport factory, no
default client, and no code path that can reach a real endpoint.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.domain.workers.remote import (RemoteHandle, RemoteStatus,
                                       SubmissionIntent)

#: Provider keys with a REST contract implementation. generate.text.cloud
#: routes these to the REST adapter; everything else stays on the pure
#: in-memory mock.
REST_PROVIDER_KEYS = ("openai", "anthropic", "google-gemini",
                      "deepseek", "xai")

_TIMEOUT_S = 30.0


def lg_timeouts(provider_key: str) -> dict:
    """Live deadline policy for the hardened client — lazy import:
    live_gate depends on this module's SPECS, so the reverse edge must
    not exist at module load."""
    from app.domain import live_gate as _lg
    return _lg.LIVE_TIMEOUTS.get(provider_key, {})


class EgressRefused(Exception):
    """Raised when a REST provider is constructed without an injected
    transport or with a transport that could perform real network IO."""


class UnsupportedRemoteOp(Exception):
    """The provider documents no such operation (e.g. Messages cancel)."""


class RemoteErrorKind(str, enum.Enum):
    VALIDATION = "validation"          # 400/404/422 — request refused
    AUTHENTICATION = "authentication"  # 401 — credential refused
    FORBIDDEN = "forbidden"            # 403 — not permitted
    CONFLICT = "conflict"              # 409 — state conflict, ambiguous
    RATE_LIMITED = "rate_limited"      # 429 — refused before processing
    SERVER_ERROR = "server_error"      # 5xx — may have processed
    TRANSPORT = "transport"            # timeout/connect — outcome unknown
    MALFORMED = "malformed"            # 2xx we cannot parse — ambiguous
    PROVIDER_FAILED = "provider_failed"  # provider's own failure status


class RemoteError(Exception):
    """A classified remote failure.

    ``charge_evidence`` is the billing judgment:
      "none_confirmed" — the provider demonstrably REFUSED the request
        before any processing (4xx rejection classes) → the C2.0b
        reservation may be released as a confirmed no-charge.
      "unknown" — the request may have been processed (5xx, transport
        failure mid-flight, unparseable success) → the reservation must
        be HELD for explicit reconciliation. Never silently released.
    """

    def __init__(self, kind: RemoteErrorKind, message: str, *,
                 charge_evidence: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.charge_evidence = charge_evidence
        self.http_status = http_status


@dataclass(frozen=True)
class EndpointSpec:
    """Documented HTTP contract for one provider — declarative data.

    Only fields backed by provider documentation are populated; a
    missing poll/cancel path means the operation is unsupported, never
    invented.
    """

    provider_key: str
    base_url: str
    lifecycle: str                    # "sync" | "async_poll"
    submit_path: str
    poll_path: str | None             # "{id}" → documented retrieval
    cancel_path: str | None           # "{id}" → documented cancel
    submit_extra: dict[str, Any]      # e.g. {"background": True}
    idempotency: str                  # RemoteProvider contract
    docs: str                         # evidence URL
    poll_statuses: dict[str, RemoteStatus] = field(default_factory=dict)


OPENAI_RESPONSES_SPEC = EndpointSpec(
    provider_key="openai",
    base_url="https://api.openai.com",
    lifecycle="async_poll",
    submit_path="/v1/responses",
    poll_path="/v1/responses/{id}",
    cancel_path="/v1/responses/{id}/cancel",
    submit_extra={"background": True},
    # The Responses API documents no server-side idempotency for create.
    # Deduplication lives only in our durable job key — ambiguous
    # outcomes reconcile, never resubmit.
    idempotency="none",
    docs="https://developers.openai.com/api/docs/guides/background",
    poll_statuses={
        "queued": RemoteStatus.SUBMITTED,
        "in_progress": RemoteStatus.RUNNING,
        "incomplete": RemoteStatus.FAILED,
        "completed": RemoteStatus.SUCCEEDED,
        "failed": RemoteStatus.FAILED,
        "cancelled": RemoteStatus.CANCELLED,
    },
)

ANTHROPIC_MESSAGES_SPEC = EndpointSpec(
    provider_key="anthropic",
    base_url="https://api.anthropic.com",
    lifecycle="sync",
    submit_path="/v1/messages",
    # Single-message retrieval/cancellation are NOT documented — query
    # returns None (outcome unknown → reconcile) and cancel raises
    # UnsupportedRemoteOp. The Batches API is a different surface.
    poll_path=None,
    cancel_path=None,
    submit_extra={},
    idempotency="none",
    docs="https://platform.claude.com/docs/en/api/messages/create",
)

GEMINI_GENERATE_SPEC = EndpointSpec(
    provider_key="google-gemini",
    base_url="https://generativelanguage.googleapis.com",
    lifecycle="sync",
    # Model travels in the path (:generateContent verb), unlike any
    # other provider here — submit() formats {model}.
    submit_path="/v1beta/models/{model}:generateContent",
    # Single-request generateContent is synchronous; batchGenerateContent
    # is a different API surface (not this contract). No documented
    # poll/cancel exists for single requests.
    poll_path=None,
    cancel_path=None,
    submit_extra={},
    idempotency="none",
    docs="https://ai.google.dev/api/generate-content",
)

DEEPSEEK_CHAT_SPEC = EndpointSpec(
    provider_key="deepseek",
    base_url="https://api.deepseek.com",
    lifecycle="sync",
    submit_path="/chat/completions",
    poll_path=None,
    cancel_path=None,
    submit_extra={"stream": False},
    idempotency="none",
    docs="https://api-docs.deepseek.com/api/create-chat-completion",
)

XAI_CHAT_SPEC = EndpointSpec(
    provider_key="xai",
    base_url="https://api.x.ai",
    lifecycle="sync",
    submit_path="/v1/chat/completions",
    # xAI documents a deferred-completion retrieval path, but only for
    # requests explicitly made deferred — our requests are synchronous,
    # so no retrieval applies. No documented cancel for completions.
    poll_path=None,
    cancel_path=None,
    submit_extra={"stream": False},
    idempotency="none",
    docs="https://docs.x.ai/developers/rest-api-reference/inference/chat-completions",
)

SPECS = {s.provider_key: s for s in
         (OPENAI_RESPONSES_SPEC, ANTHROPIC_MESSAGES_SPEC,
          GEMINI_GENERATE_SPEC, DEEPSEEK_CHAT_SPEC, XAI_CHAT_SPEC)}

# HTTP status → (kind, charge_evidence). 4xx classes are NOT lumped:
# validation/auth/forbidden/rate-limit rejections mean the request was
# refused before processing (confirmed no-charge); 409 conflicts and all
# 5xx mean the server may already hold work (unknown → hold).
_STATUS_MAP = {
    400: (RemoteErrorKind.VALIDATION, "none_confirmed"),
    401: (RemoteErrorKind.AUTHENTICATION, "none_confirmed"),
    403: (RemoteErrorKind.FORBIDDEN, "none_confirmed"),
    404: (RemoteErrorKind.VALIDATION, "none_confirmed"),
    409: (RemoteErrorKind.CONFLICT, "unknown"),
    422: (RemoteErrorKind.VALIDATION, "none_confirmed"),
    429: (RemoteErrorKind.RATE_LIMITED, "none_confirmed"),
}


def _classify(resp: httpx.Response, *, op: str) -> RemoteError | None:
    """Non-2xx → RemoteError or None when the response is usable."""
    if 200 <= resp.status_code < 300:
        return None
    if resp.status_code >= 500:
        kind, evidence = RemoteErrorKind.SERVER_ERROR, "unknown"
    else:
        kind, evidence = _STATUS_MAP.get(
            resp.status_code,
            (RemoteErrorKind.VALIDATION, "unknown"))
    try:
        body = resp.json()
    except Exception:
        body = {}
    err = body.get("error") or {}
    # Provider error TYPE/CODE only — a provider message could echo
    # request content; it is never stored.
    msg = f"{op} failed: HTTP {resp.status_code} ({err.get('type') or err.get('code') or kind.value})"
    return RemoteError(kind, msg, charge_evidence=evidence,
                       http_status=resp.status_code)


class RestRemoteProvider:
    """One provider's documented HTTP contract behind an injected
    transport. Subclass hooks supply the body builder, auth headers and
    response parser."""

    spec: EndpointSpec
    name: str
    idempotency: str

    def __init__(self, spec: EndpointSpec, *, transport: Any,
                 api_key: str = "") -> None:
        # Transport is REQUIRED and must be an in-memory mock in C2.0c —
        # any real transport class (or None, which lets httpx pick a
        # live default) is refused. This is the egress boundary.
        # Stage B1 adds ONE alternative: an object marked
        # is_live_transport (the hardened LiveHTTPTransport wrapper) —
        # reachable only through live_dispatch(), never a shipped path.
        if transport is None:
            raise EgressRefused(
                "explicit transport required — a default client would "
                "create a real network egress path")
        if isinstance(transport, httpx.MockTransport):
            self._client = httpx.AsyncClient(
                transport=transport, base_url=spec.base_url,
                timeout=httpx.Timeout(_TIMEOUT_S))
        elif getattr(transport, "is_live_transport", False):
            # Hardened client for the live boundary: trust_env=False so
            # proxy env vars and netrc are never honored; redirects are
            # never auto-followed (the transport refuses them outright);
            # provider-scoped live deadlines replace the default.
            cfg = lg_timeouts(spec.provider_key)
            self._client = httpx.AsyncClient(
                transport=transport, base_url=spec.base_url,
                timeout=httpx.Timeout(
                    cfg.get("total_s", _TIMEOUT_S),
                    connect=cfg.get("connect_s", 10.0)),
                follow_redirects=False, trust_env=False)
        else:
            raise EgressRefused(
                f"transports must be httpx.MockTransport or the marked "
                f"live wrapper (got {type(transport).__name__}) — no "
                "default/live client wiring exists")
        self.spec = spec
        self.name = f"rest-{spec.provider_key}"
        self.idempotency = spec.idempotency
        self._transport = transport
        self._api_key = api_key

    @property
    def transport_is_simulated(self) -> bool:
        return isinstance(self._transport, httpx.MockTransport)

    # ---- provider-specific surface (subclass hooks) ----

    def _auth_headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        raise NotImplementedError

    # ---- contract ----

    async def _call(self, method: str, path: str, *, op: str,
                    json_body: dict | None = None) -> tuple[dict, httpx.Headers]:
        """One classified request. Raises RemoteError; returns (json, headers)."""
        try:
            resp = await self._client.request(
                method, path, headers=self._auth_headers(),
                json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RemoteError(
                RemoteErrorKind.TRANSPORT,
                f"{op}: transport failure ({type(exc).__name__}) — "
                "outcome unknown, never auto-resubmit",
                charge_evidence="unknown") from exc
        err = _classify(resp, op=op)
        if err is not None:
            raise err
        try:
            body = resp.json()
        except Exception as exc:
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                f"{op}: HTTP {resp.status_code} with unparseable body — "
                "outcome unknown",
                charge_evidence="unknown",
                http_status=resp.status_code) from exc
        if not isinstance(body, dict):
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                f"{op}: non-object JSON response — outcome unknown",
                charge_evidence="unknown",
                http_status=resp.status_code)
        # Canonical form of the last 2xx body — the live boundary
        # fingerprints this for provenance evidence (B1+).
        self._last_body_canonical = json.dumps(body, sort_keys=True,
                                               default=str)
        return body, resp.headers

    async def submit(self, intent: SubmissionIntent) -> RemoteHandle:
        # Paths without placeholders format to themselves; Gemini
        # embeds the model in the URL ({model}:generateContent).
        path = self.spec.submit_path.format(
            model=intent.request.get("model_id") or "")
        body, headers = await self._call(
            "POST", path, op="submit",
            json_body={**self._submit_body(intent.request),
                       **self.spec.submit_extra})
        try:
            return self._parse(body, headers)
        except RemoteError:
            raise
        except Exception as exc:
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                f"submit: unparseable success body ({exc}) — outcome unknown",
                charge_evidence="unknown") from exc

    async def query(self, remote_id: str) -> RemoteHandle | None:
        """Documented retrieval only. None = outcome unknown —
        providers without a retrieval path return None always."""
        if self.spec.poll_path is None:
            return None
        try:
            body, headers = await self._call(
                "GET", self.spec.poll_path.format(id=remote_id),
                op="retrieve")
        except RemoteError:
            # A failed retrieval says nothing about billing — the job
            # may still be running. Unknown, never "no charge".
            return None
        try:
            return self._parse(body, headers)
        except Exception:
            return None

    async def cancel(self, remote_id: str) -> RemoteHandle:
        """Documented cancellation only — providers without it raise
        UnsupportedRemoteOp rather than inventing an endpoint."""
        if self.spec.cancel_path is None:
            raise UnsupportedRemoteOp(
                f"{self.name}: no documented cancel endpoint")
        body, headers = await self._call(
            "POST", self.spec.cancel_path.format(id=remote_id),
            op="cancel")
        return self._parse(body, headers)


# ---------- OpenAI Responses ----------

def _openai_text(body: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in body.get("output") or []:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            if block.get("type") == "output_text" and block.get("text"):
                parts.append(block["text"])
    return "".join(parts)


def _openai_usage(body: dict[str, Any]) -> dict[str, int] | None:
    u = body.get("usage")
    if not isinstance(u, dict):
        return None
    return {"input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0)}


def _deepseek_usage(usage: Any) -> dict[str, int] | None:
    """Normalize the chat-completions usage object. Both token counts
    must be present and integer-coercible — anything missing, partial,
    or malformed returns None so settlement treats it as unresolved
    exposure (never fabricated zeros, never a confirmed charge)."""
    if not isinstance(usage, dict):
        return None
    inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if inp is None or out is None:
        return None  # null counts are schema noise, not honest zeros
    try:
        return {"input_tokens": int(inp), "output_tokens": int(out)}
    except (TypeError, ValueError):
        return None


class OpenAIResponsesProvider(RestRemoteProvider):
    """OpenAI Responses API — async lifecycle via ``background: true``.

    submit → resp_* id + status (queued/in_progress)
    query  → GET /v1/responses/{id}  (documented)
    cancel → POST /v1/responses/{id}/cancel  (background responses only)
    """

    def _auth_headers(self) -> dict[str, str]:
        # Bearer auth per docs. Key is injected at construction (test
        # doubles carry dummy values); it is never logged or persisted.
        return {"Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json"}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"model": req.get("model_id"),
                "input": req.get("prompt") or ""}

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        status = self.spec.poll_statuses.get(str(body.get("status")),
                                             RemoteStatus.UNKNOWN)
        detail = {
            "remote_status_raw": body.get("status"),
            "transport_simulated": True,
            "text": _openai_text(body),
            # x-request-id is provider bookkeeping — metadata only; it
            # is NOT verification evidence (provider_verified stays
            # unset: only a real authorized transport can produce it).
            "request_id": headers.get("x-request-id"),
        }
        usage = _openai_usage(body)
        if usage is not None:
            detail["usage"] = usage
        err = body.get("error")
        if isinstance(err, dict):
            detail["error"] = {"type": err.get("type"),
                               "code": err.get("code")}
        if status == RemoteStatus.UNKNOWN:
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                f"unrecognized response status {body.get('status')!r}",
                charge_evidence="unknown")
        return RemoteHandle(self.name, body.get("id"), status,
                            detail=detail)


# ---------- Anthropic Messages ----------

class AnthropicMessagesProvider(RestRemoteProvider):
    """Anthropic Messages API — synchronous completion only.

    submit → finished message (msg_* id, content, usage) — no remote
    operation id exists; the message id is a RESPONSE id.
    query  → None always (no documented retrieval for single messages)
    cancel → UnsupportedRemoteOp (no documented cancel)
    """

    _API_VERSION = "2023-06-01"

    def _auth_headers(self) -> dict[str, str]:
        # x-api-key + anthropic-version per docs.
        return {"x-api-key": self._api_key,
                "anthropic-version": self._API_VERSION,
                "Content-Type": "application/json"}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"model": req.get("model_id"),
                "max_tokens": int(req.get("max_output_tokens") or 1024),
                "messages": [{"role": "user",
                              "content": req.get("prompt") or ""}]}

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        if body.get("type") == "error" or not body.get("id"):
            err = body.get("error") or {}
            raise RemoteError(
                RemoteErrorKind.PROVIDER_FAILED,
                f"provider reported error type={err.get('type')!r}",
                charge_evidence="unknown")
        text = "".join(b.get("text") or ""
                       for b in body.get("content") or []
                       if b.get("type") == "text")
        usage = body.get("usage") or {}
        detail = {
            "transport_simulated": True,
            "text": text,
            "stop_reason": body.get("stop_reason"),
            "usage": {"input_tokens": int(usage.get("input_tokens") or 0),
                      "output_tokens": int(usage.get("output_tokens") or 0)},
        }
        return RemoteHandle(self.name, body["id"], RemoteStatus.SUCCEEDED,
                            detail=detail)


# ---------- Google Gemini generateContent ----------

class GeminiGenerateProvider(RestRemoteProvider):
    """Gemini ``generateContent`` — synchronous, model in the URL.

    submit → finished response (candidates + usageMetadata). The
    documented response carries NO request/response id — remote_id is
    deliberately None rather than a fabricated value.
    query  → None always (no single-request retrieval)
    cancel → UnsupportedRemoteOp
    """

    def _auth_headers(self) -> dict[str, str]:
        # x-goog-api-key header per docs (never a ?key= URL param —
        # query strings leak into proxy/server logs).
        return {"x-goog-api-key": self._api_key,
                "Content-Type": "application/json"}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        return {
            "contents": [{"role": "user",
                          "parts": [{"text": req.get("prompt") or ""}]}],
            "generationConfig": {
                "maxOutputTokens": int(req.get("max_output_tokens") or 1024)},
        }

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        err = body.get("error")
        if isinstance(err, dict):
            # A documented error object inside the JSON body — billing
            # outcome unverifiable from here → provider failure (hold).
            raise RemoteError(
                RemoteErrorKind.PROVIDER_FAILED,
                f"provider reported error status={err.get('status')!r}",
                charge_evidence="unknown")
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                "generateContent response has no candidates — outcome unknown",
                charge_evidence="unknown")
        text = "".join(
            p.get("text") or ""
            for p in (candidates[0].get("content") or {}).get("parts") or [])
        usage_meta = body.get("usageMetadata") or {}
        detail = {
            "transport_simulated": True,
            "text": text,
            "finish_reason": candidates[0].get("finishReason"),
            "model_version": body.get("modelVersion"),
            "no_remote_id": True,  # generateContent returns no response id
            "usage": {"input_tokens": int(usage_meta.get("promptTokenCount") or 0),
                      "output_tokens": int(usage_meta.get("candidatesTokenCount") or 0)},
        }
        return RemoteHandle(self.name, None, RemoteStatus.SUCCEEDED,
                            detail=detail)


# ---------- OpenAI-compatible chat completions (DeepSeek, xAI) ----------

class OpenAICompatChatProvider(RestRemoteProvider):
    """Shared shape for documented OpenAI-compatible chat-completions
    APIs — DeepSeek ``/chat/completions`` and xAI ``/v1/chat/completions``.

    Both are synchronous: submit → ``chat.completion`` object with an
    ``id``, ``choices[0].message.content`` and ``usage``. Neither
    documents retrieval or cancellation for a completed request —
    compatible request shape does NOT imply identical lifecycle or
    billing semantics; only documented operations are wired.
    """

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json"}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"model": req.get("model_id"),
                "messages": [{"role": "user",
                              "content": req.get("prompt") or ""}],
                "max_tokens": int(req.get("max_output_tokens") or 1024)}

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            err = body.get("error")
            if isinstance(err, dict):
                raise RemoteError(
                    RemoteErrorKind.PROVIDER_FAILED,
                    f"provider reported error type={err.get('type')!r}",
                    charge_evidence="unknown")
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                "chat.completion response has no choices — outcome unknown",
                charge_evidence="unknown")
        message = choices[0].get("message") or {}
        # Missing usage stays None — never fabricate zeros: a real
        # completion without usage is anomalous, and zeros would settle
        # at $0 instead of falling back to the reserved-cap estimate.
        usage = body.get("usage")
        usage = _deepseek_usage(usage)  # None when unusable
        detail = {
            "transport_simulated": True,
            "text": message.get("content") or "",
            "finish_reason": choices[0].get("finish_reason"),
            "usage": usage,
        }
        return RemoteHandle(self.name, body.get("id"),
                            RemoteStatus.SUCCEEDED, detail=detail)


_PROVIDER_CLS = {
    "openai": (OpenAIResponsesProvider, OPENAI_RESPONSES_SPEC),
    "anthropic": (AnthropicMessagesProvider, ANTHROPIC_MESSAGES_SPEC),
    "google-gemini": (GeminiGenerateProvider, GEMINI_GENERATE_SPEC),
    "deepseek": (OpenAICompatChatProvider, DEEPSEEK_CHAT_SPEC),
    "xai": (OpenAICompatChatProvider, XAI_CHAT_SPEC),
}


def make_provider(provider_key: str, *, transport: Any,
                  api_key: str = "") -> RestRemoteProvider:
    """Construct the documented provider impl for a catalog key.
    Transport must be httpx.MockTransport (enforced)."""
    try:
        cls, spec = _PROVIDER_CLS[provider_key]
    except KeyError:
        raise KeyError(
            f"no REST contract implementation for {provider_key!r}") from None
    return cls(spec, transport=transport, api_key=api_key)
