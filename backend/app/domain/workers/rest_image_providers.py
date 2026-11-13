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
IMAGE_REST_KEYS = ("openai-images", "gemini-images", "recraft",
                   "ideogram", "bfl-flux")

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

# C3-C — Google image generation on the shared Gemini API host.
# Two documented surfaces share this provider_key:
#   • gemini-2.5-flash-image → POST /v1beta/models/{model}:generateContent
#     (contents/parts → candidates[].content.parts[].inlineData)
#   • imagen-4.0-*-generate-001 → POST /v1beta/models/{model}:predict
#     (instances/parameters → predictions[].bytesBase64Encoded)
# Both are synchronous, x-goog-api-key auth, no poll/cancel surface.
GEMINI_IMAGES_SPEC = EndpointSpec(
    provider_key="gemini-images",
    base_url="https://generativelanguage.googleapis.com",
    lifecycle="sync",
    submit_path="/v1beta/models/{model}:generateContent",
    poll_path=None,
    cancel_path=None,
    submit_extra={},
    idempotency="none",
    docs="https://ai.google.dev/gemini-api/docs/image-generation",
)

# C3-C — Recraft: synchronous POST /v1/images/generations, Bearer auth.
# Requesting response_format="b64_json" returns inline bytes (the
# default url mode issues ~24h temporary links — we ask for bytes so
# the artifact lands in CAS immediately). Response reports `credits`
# (API units consumed) — settle from the reported count.
RECRAFT_SPEC = EndpointSpec(
    provider_key="recraft",
    base_url="https://external.api.recraft.ai",
    lifecycle="sync",
    submit_path="/v1/images/generations",
    poll_path=None,
    cancel_path=None,
    submit_extra={"n": 1, "response_format": "b64_json"},
    idempotency="none",
    docs="https://www.recraft.ai/docs/api-reference/endpoints",
)

# C3-C — Ideogram: synchronous multipart POST
# /v2/image/generate/ideogram-3, Api-Key header. The documented
# response carries temporary image URLs (data[].url) — retrieved via
# fetch_artifact() on the documented ideogram.ai delivery hosts.
# The API also documents an `async` flag + GET /v2/generations/{id}
# polling path; this contract implements the synchronous mode only.
IDEOGRAM_SPEC = EndpointSpec(
    provider_key="ideogram",
    base_url="https://api.ideogram.ai",
    lifecycle="sync",
    submit_path="/v2/image/generate/ideogram-3",
    poll_path=None,
    cancel_path=None,
    submit_extra={"num_images": 1},
    idempotency="none",
    docs="https://developer.ideogram.ai/api-reference/images/generate/ideogram-3",
    content="multipart",
    artifact_hosts=("ideogram.ai", "cdn.ideogram.ai"),
)

# C3-C — Black Forest Labs: asynchronous generation. POST
# /v1/{model} returns {id, polling_url}; GET /v1/get_result?id=
# polls until status Ready / Error / Failed. Results are temporary
# URLs (result.sample) on the delivery.bfl.ai CDN — retrieved via
# fetch_artifact(). BFL documents NO cancellation endpoint, so
# cancel_path stays None and cancel() raises UnsupportedRemoteOp.
BFL_FLUX_SPEC = EndpointSpec(
    provider_key="bfl-flux",
    base_url="https://api.bfl.ai",
    lifecycle="async_poll",
    submit_path="/v1/{model}",
    poll_path="/v1/get_result?id={id}",
    cancel_path=None,
    submit_extra={"output_format": "png"},
    idempotency="none",
    docs="https://docs.bfl.ai/flux_models/flux_1_1_pro",
    artifact_hosts=("delivery.bfl.ai", "api.bfl.ai"),
)

IMAGE_SPECS = {s.provider_key: s for s in (
    OPENAI_IMAGES_SPEC, GEMINI_IMAGES_SPEC, RECRAFT_SPEC,
    BFL_FLUX_SPEC,
    IDEOGRAM_SPEC)}


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


class GeminiImagesProvider(RestRemoteProvider):
    """Google image generation on the Gemini API — synchronous.

    ``gemini-2.5-flash-image`` uses ``:generateContent``; ``imagen-4.0-*``
    uses ``:predict`` — the verb is part of the documented contract, so
    submit() picks it from the model id rather than inventing a path.
    Neither surface documents retrieval or cancellation.
    """

    @staticmethod
    def _is_imagen(model_id: str | None) -> bool:
        return (model_id or "").startswith("imagen-")

    async def submit(self, intent: SubmissionIntent) -> RemoteHandle:
        model = intent.request.get("model_id") or ""
        if self._is_imagen(model):
            body, headers = await self._call(
                "POST", f"/v1beta/models/{model}:predict", op="submit",
                json_body=self._submit_body(intent.request))
        else:
            path = self.spec.submit_path.format(model=model)
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

    def _auth_headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key,
                "Content-Type": "application/json"}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        prompt = req.get("prompt") or ""
        ratio = req.get("size")           # catalog sizes are "W:H" ratios
        if self._is_imagen(req.get("model_id")):
            params: dict[str, Any] = {"sampleCount": 1}
            if ratio:
                params["aspectRatio"] = ratio
            return {"instances": [{"prompt": prompt}],
                    "parameters": params}
        body: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        if ratio:
            body["generationConfig"]["imageConfig"] = {
                "aspectRatio": ratio}
        return body

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        detail: dict[str, Any] = {"transport_simulated": True}
        # Imagen :predict → predictions[{bytesBase64Encoded, mimeType}]
        preds = body.get("predictions")
        if isinstance(preds, list) and preds:
            first = preds[0] if isinstance(preds[0], dict) else {}
            detail["image_b64"] = first.get("bytesBase64Encoded")
            detail["images_returned"] = len(preds)
            return RemoteHandle(self.name, None,
                                RemoteStatus.SUCCEEDED, detail=detail)
        # generateContent → candidates[].content.parts[].inlineData.data
        cands = body.get("candidates")
        if isinstance(cands, list) and cands:
            images = 0
            b64 = None
            for cand in cands:
                parts = ((cand.get("content") or {}).get("parts")
                         if isinstance(cand, dict) else None) or []
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    inline = part.get("inlineData") or part.get("inline_data")
                    if isinstance(inline, dict) and inline.get("data"):
                        images += 1
                        b64 = b64 or inline["data"]
            if b64:
                detail["image_b64"] = b64
                detail["images_returned"] = images
                usage = body.get("usageMetadata")
                if isinstance(usage, dict):
                    detail["usage"] = usage
                return RemoteHandle(self.name, None,
                                    RemoteStatus.SUCCEEDED, detail=detail)
            # Safety-finished candidates carry finishReason but no image.
            reason = (cands[0].get("finishReason")
                      if isinstance(cands[0], dict) else None)
            raise RemoteError(
                RemoteErrorKind.PROVIDER_FAILED,
                f"generation finished without image "
                f"(finishReason={reason!r}) — provider refusal class",
                charge_evidence="none_confirmed")
        raise RemoteError(
            RemoteErrorKind.MALFORMED,
            "image response has no predictions/candidates — outcome unknown",
            charge_evidence="unknown")


class RecraftProvider(RestRemoteProvider):
    """Recraft — synchronous ``POST /v1/images/generations``.

    ``response_format=b64_json`` (in submit_extra) returns inline bytes;
    the response's ``credits`` field reports the API units consumed —
    settle uses the reported count, never an assumed one. ``style`` is
    not transmitted: styles are model-specific curated names not yet
    catalogued, and an unverified style id must not be sent.
    """

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json"}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        body = {"model": req.get("model_id"),
                "prompt": req.get("prompt") or ""}
        if req.get("size"):
            body["size"] = req["size"]
        # negative_prompt is documented on V3 only — transmit it solely
        # for V3 models; nothing is claimed for other lines.
        if req.get("negative_prompt") and "v3" in (req.get("model_id") or ""):
            body["negative_prompt"] = req["negative_prompt"]
        return body

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        data = body.get("data")
        if not isinstance(data, list) or not data:
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                "generations response has no data entries — outcome unknown",
                charge_evidence="unknown")
        first = data[0] if isinstance(data[0], dict) else {}
        detail: dict[str, Any] = {
            "transport_simulated": True,
            "image_b64": first.get("b64_json"),
            "image_url": first.get("url"),
            "images_returned": len(data),
        }
        credits = body.get("credits")
        if isinstance(credits, (int, float)):
            detail["credits"] = credits
            detail["usage"] = {"credits": int(credits)}
        return RemoteHandle(self.name, body.get("id"),
                            RemoteStatus.SUCCEEDED, detail=detail)


class IdeogramProvider(RestRemoteProvider):
    """Ideogram 3.0 — synchronous multipart generation.

    ``POST /v2/image/generate/ideogram-3`` as multipart/form-data
    (Api-Key header). Sync mode returns ``data[{url, seed, resolution}]``
    whose links are temporary — the adapter retrieves the artifact via
    ``fetch_artifact``. The documented ``async`` mode + polling path is
    not claimed by this contract.
    """

    def _auth_headers(self) -> dict[str, str]:
        # Content-Type is left to httpx's multipart encoder.
        return {"Api-Key": self._api_key}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        form: dict[str, Any] = {"prompt": req.get("prompt") or ""}
        if req.get("size"):
            form["aspect_ratio"] = req["size"]
        if req.get("quality"):
            form["rendering_speed"] = req["quality"]
        if req.get("seed") is not None:
            form["seed"] = req["seed"]
        if req.get("negative_prompt"):
            form["negative_prompt"] = req["negative_prompt"]
        return form

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        data = body.get("data")
        if not isinstance(data, list) or not data:
            raise RemoteError(
                RemoteErrorKind.MALFORMED,
                "ideogram response has no data entries — outcome unknown",
                charge_evidence="unknown")
        first = data[0] if isinstance(data[0], dict) else {}
        detail: dict[str, Any] = {
            "transport_simulated": True,
            "image_url": first.get("url"),
            "images_returned": len(data),
            "seed": first.get("seed"),
            "resolution": first.get("resolution"),
        }
        return RemoteHandle(self.name, None,
                            RemoteStatus.SUCCEEDED, detail=detail)


class BflFluxProvider(RestRemoteProvider):
    """FLUX async contract (C3-C): submit → id, poll get_result.

    Documented poll statuses: Pending, Ready, Error, Failed,
    Request Moderated, Content Moderated, Task not found. Moderation
    is a provider refusal — no charge evidence; terminal failure.
    ``Task not found`` maps to UNKNOWN: we submitted but the record
    is unreachable — reconcile, never resubmit.
    """

    _POLL = {"pending": RemoteStatus.RUNNING,
             "ready": RemoteStatus.SUCCEEDED,
             "error": RemoteStatus.FAILED,
             "failed": RemoteStatus.FAILED,
             "request moderated": RemoteStatus.FAILED,
             "content moderated": RemoteStatus.FAILED,
             "task not found": RemoteStatus.UNKNOWN}

    def _auth_headers(self) -> dict[str, str]:
        return {"x-key": self._api_key}

    def _submit_body(self, req: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"prompt": req.get("prompt") or "",
                                "prompt_upsampling": False,
                                "safety_tolerance": 2}
        size = req.get("size") or ""
        # Documented dimension range 256–1440, multiples of 32; the
        # catalog only declares compliant "WxH" presets.
        if "x" in size:
            w, _, h = size.partition("x")
            body["width"], body["height"] = int(w), int(h)
        if req.get("seed") is not None:
            body["seed"] = req["seed"]
        return body

    def _parse(self, body: dict[str, Any], headers: httpx.Headers) -> RemoteHandle:
        # Submit response: {"id": ..., "polling_url": ...} (no status).
        if "status" not in body:
            rid = body.get("id")
            if not rid:
                raise RemoteError(
                    RemoteErrorKind.MALFORMED,
                    "flux submit returned no request id — outcome unknown",
                    charge_evidence="unknown")
            return RemoteHandle(self.name, str(rid),
                                RemoteStatus.SUBMITTED,
                                detail={"transport_simulated": True,
                                        "polling_url": body.get(
                                            "polling_url")})
        raw = str(body.get("status") or "").strip().lower()
        status = self._POLL.get(raw, RemoteStatus.UNKNOWN)
        detail: dict[str, Any] = {"transport_simulated": True,
                                  "remote_status_raw": body.get("status")}
        if status == RemoteStatus.SUCCEEDED:
            result = body.get("result") or {}
            detail["image_url"] = result.get("sample")
            detail["seed"] = result.get("seed")
            detail["images_returned"] = 1
        if status == RemoteStatus.FAILED:
            detail["failure"] = (body.get("result") or {}).get("details") \
                or body.get("status")
        return RemoteHandle(self.name, str(body.get("id") or "") or None,
                            status, detail=detail)


def make_image_provider(provider_key: str, *, transport: Any,
                        api_key: str = "") -> RestRemoteProvider:
    spec = IMAGE_SPECS[provider_key]
    cls = {"openai-images": OpenAIImagesProvider,
           "gemini-images": GeminiImagesProvider,
           "recraft": RecraftProvider,
           "ideogram": IdeogramProvider,
           "bfl-flux": BflFluxProvider}[provider_key]
    return cls(spec, transport=transport, api_key=api_key)
