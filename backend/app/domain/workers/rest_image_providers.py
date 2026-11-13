"""C3-A — REST image RemoteProvider contracts over injected transports.

Same egress discipline as the text providers: construction REQUIRES an
injected transport — ``httpx.MockTransport`` (simulated) or the marked
``is_live_transport`` wrapper (the only sanctioned live path, reachable
only through ``live_dispatch``). A default client is refused; there is
no production transport wiring here.

Image contracts declared declaratively via ``EndpointSpec`` so later
providers need only data + body/parser hooks:

- ``openai-images`` — OpenAI Images API (``POST /v1/images/generations``):
  synchronous — the response body IS the finished generation
  (``data[{b64_json, revised_prompt}]`` + ``usage``). No documented
  poll/cancel exists for generations. https://platform.openai.com/docs/

Async providers (e.g. FLUX's ``async_poll`` job model) plug into the
SAME contract: EndpointSpec.lifecycle="async_poll" with documented
poll_path/cancel_path — the adapter's bounded-poll loop already handles
that lifecycle for text and will drive it identically for images.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.domain.workers.remote import RemoteHandle, RemoteStatus
from app.domain.workers.rest_providers import (
    EndpointSpec, RemoteError, RemoteErrorKind, RestRemoteProvider)

#: Catalog provider keys with an implemented image REST contract.
IMAGE_REST_KEYS = ("openai-images",)

OPENAI_IMAGES_SPEC = EndpointSpec(
    provider_key="openai-images",
    base_url="https://api.openai.com",
    lifecycle="sync",
    submit_path="/v1/images/generations",
    # Generations retrieval/cancellation are NOT documented for the
    # sync Images API — query returns None (outcome unknown) and cancel
    # raises UnsupportedRemoteOp.
    poll_path=None,
    cancel_path=None,
    submit_extra={"n": 1},
    idempotency="none",
    docs="https://platform.openai.com/docs/api-reference/images",
)

IMAGE_SPECS = {OPENAI_IMAGES_SPEC.provider_key: OPENAI_IMAGES_SPEC}


def _image_usage(body: dict[str, Any]) -> dict[str, int] | None:
    """Normalize the Images-API usage object. Any missing or
    uncoercible count → None: unusable usage is unresolved exposure,
    never a fabricated charge."""
    u = body.get("usage")
    if not isinstance(u, dict):
        return None
    try:
        inp = int(u["input_tokens"])
        out = int(u["output_tokens"])
    except (KeyError, TypeError, ValueError):
        return None
    if inp < 0 or out < 0:
        return None
    return {"input_tokens": inp, "output_tokens": out}


class OpenAIImagesProvider(RestRemoteProvider):
    """OpenAI Images API — synchronous generation.

    submit → 200 + ``data[{b64_json, revised_prompt}]`` + ``usage``
    query  → no documented path → always None (outcome unknown)
    cancel → no documented path → UnsupportedRemoteOp
    """

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json"}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        body = {"model": req.get("model_id"),
                "prompt": req.get("prompt") or ""}
        if req.get("size"):
            body["size"] = req["size"]
        if req.get("quality"):
            body["quality"] = req["quality"]
        return body

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        data = body.get("data")
        if not isinstance(data, list) or not data:
            err = body.get("error")
            if isinstance(err, dict):
                raise RemoteError(
                    RemoteErrorKind.PROVIDER_FAILED,
                    f"provider reported error type={err.get('type')!r}",
                    charge_evidence="unknown")
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                "images response has no data entries — outcome unknown",
                charge_evidence="unknown")
        first = data[0] if isinstance(data[0], dict) else {}
        detail = {
            "transport_simulated": True,
            "image_b64": first.get("b64_json"),
            "image_url": first.get("url"),
            "revised_prompt": first.get("revised_prompt"),
            "images_returned": len(data),
            "request_id": headers.get("x-request-id"),
        }
        usage = _image_usage(body)
        if usage is not None:
            detail["usage"] = usage
        # The Images API returns no response object id — remote_id
        # stays None (honest, never fabricated).
        return RemoteHandle(self.name, body.get("id"),
                            RemoteStatus.SUCCEEDED, detail=detail)


def make_image_provider(provider_key: str, *, transport: Any,
                        api_key: str = "") -> RestRemoteProvider:
    spec = IMAGE_SPECS[provider_key]
    cls = {"openai-images": OpenAIImagesProvider}[provider_key]
    return cls(spec, transport=transport, api_key=api_key)
