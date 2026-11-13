"""C2.0c — simulated HTTP transports for REST provider contracts.

``httpx.MockTransport`` handlers that reproduce the DOCUMENTED response
shapes of each provider (status fields, ids, usage objects) with
deterministic synthetic data. These are fixtures, not providers — no
socket is ever opened. Every response is produced in-process; usage
numbers are invented test data and feed only the ``simulated``
accounting environment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any, Callable

import httpx

from app.domain.workers.rest_providers import make_provider

SIMULATED_TEXT = ("[simulated transport response — no provider contact] "
                  "A short synthetic completion for contract testing.")


def _json(body: dict[str, Any], status: int = 200,
          headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "x-request-id": "req_simulated", **(headers or {})})


def openai_response_obj(*, resp_id: str, status: str,
                        text: str = SIMULATED_TEXT,
                        usage: dict | None = None,
                        error: dict | None = None) -> dict:
    """Documented Responses-API object shape (subset)."""
    obj: dict[str, Any] = {
        "id": resp_id, "object": "response", "status": status,
        "background": True, "model": "gpt-5", "error": error,
        "output": [{
            "type": "message", "id": f"msg_{resp_id[5:12]}", "role": "assistant",
            "status": "completed" if status == "completed" else "in_progress",
            "content": [{"type": "output_text", "text": text,
                         "annotations": []}],
        }] if status == "completed" else [],
    }
    if usage is not None:
        obj["usage"] = usage
    elif status == "completed":
        obj["usage"] = {"input_tokens": 17, "output_tokens": 33,
                        "total_tokens": 50}
    return obj


def anthropic_message_obj(*, msg_id: str, text: str = SIMULATED_TEXT,
                          usage: dict | None = None) -> dict:
    """Documented Messages-API object shape (subset)."""
    return {"id": msg_id, "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": usage or {"input_tokens": 11, "output_tokens": 7}}


def gemini_generate_obj(*, model: str, text: str = SIMULATED_TEXT,
                        usage: dict | None = None) -> dict:
    """Documented generateContent response shape (subset) — note the
    absence of any response id, matching the real contract."""
    u = usage or {"promptTokenCount": 19, "candidatesTokenCount": 31,
                  "totalTokenCount": 50}
    return {"candidates": [{
                "content": {"role": "model",
                            "parts": [{"text": text}]},
                "finishReason": "STOP", "index": 0}],
            "usageMetadata": u, "modelVersion": model}


def chat_completion_obj(*, comp_id: str, model: str,
                        text: str = SIMULATED_TEXT,
                        usage: dict | None = None) -> dict:
    """Documented OpenAI-compatible chat.completion shape (subset) —
    shared by DeepSeek and xAI fixtures."""
    u = usage or {"prompt_tokens": 21, "completion_tokens": 29,
                  "total_tokens": 50}
    return {"id": comp_id, "object": "chat.completion",
            "created": 1739301120, "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant",
                                     "content": text,
                                     "refusal": None},
                         "logprobs": None,
                         "finish_reason": "stop"}],
            "usage": u, "system_fingerprint": "fp_simulated"}


def simulated_handler(provider_key: str,
                      overrides: dict[str, Callable] | None = None):
    """Default in-memory handler for a provider's documented routes.

    ``overrides`` lets tests replace individual routes
    ("submit"|"retrieve"|"cancel") with scenario handlers while the
    rest keep sane simulated defaults.
    """
    seq = {"poll": 0, "n": 0}

    if provider_key == "openai":
        def submit(req: httpx.Request) -> httpx.Response:
            seq["n"] += 1
            return _json(openai_response_obj(
                resp_id=f"resp_sim_{seq['n']:04d}", status="queued"))

        def retrieve(req: httpx.Request) -> httpx.Response:
            rid = req.url.path.rsplit("/", 1)[-1]
            seq["poll"] += 1
            # First poll still running; second completes.
            status = "in_progress" if seq["poll"] < 2 else "completed"
            return _json(openai_response_obj(resp_id=rid, status=status))

        def cancel(req: httpx.Request) -> httpx.Response:
            rid = req.url.path.split("/")[-2]
            return _json(openai_response_obj(resp_id=rid, status="cancelled"))

        routes = {"submit": submit, "retrieve": retrieve, "cancel": cancel,
                  **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and req.url.path == "/v1/responses":
                return routes["submit"](req)
            if req.method == "GET" and re.fullmatch(
                    r"/v1/responses/[\w-]+", req.url.path):
                return routes["retrieve"](req)
            if req.method == "POST" and re.fullmatch(
                    r"/v1/responses/[\w-]+/cancel", req.url.path):
                return routes["cancel"](req)
            return _json({"error": {"type": "not_found",
                                    "message": "route"}}, 404)
        return handler

    if provider_key == "anthropic":
        def submit(req: httpx.Request) -> httpx.Response:
            seq["n"] += 1
            return _json(anthropic_message_obj(
                msg_id=f"msg_sim_{seq['n']:04d}"))

        routes = {"submit": submit, **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and req.url.path == "/v1/messages":
                return routes["submit"](req)
            return _json({"type": "error",
                          "error": {"type": "not_found_error",
                                    "message": "route"}}, 404)
        return handler

    if provider_key == "google-gemini":
        def submit(req: httpx.Request) -> httpx.Response:
            # Model id is the last path segment before :generateContent.
            model = req.url.path.split("/")[-1].split(":")[0]
            return _json(gemini_generate_obj(model=model))

        routes = {"submit": submit, **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if (req.method == "POST" and req.url.path.startswith("/v1beta/models/")
                    and req.url.path.endswith(":generateContent")):
                return routes["submit"](req)
            return _json({"error": {"code": 404, "status": "NOT_FOUND",
                                    "message": "route"}}, 404)
        return handler

    if provider_key in ("deepseek", "xai"):
        path = "/chat/completions" if provider_key == "deepseek" \
            else "/v1/chat/completions"

        def submit(req: httpx.Request) -> httpx.Response:
            seq["n"] += 1
            model = json.loads(req.content).get("model") or provider_key
            return _json(chat_completion_obj(
                comp_id=f"{provider_key}-sim-{seq['n']:04d}", model=model))

        routes = {"submit": submit, **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" and req.url.path == path:
                return routes["submit"](req)
            return _json({"error": {"message": "route", "type": "not_found",
                                    "code": "404"}}, 404)
        return handler

    raise KeyError(f"no simulated transport for {provider_key!r}")


def simulated_transport(provider_key: str,
                        overrides: dict[str, Callable] | None = None,
                        ) -> httpx.MockTransport:
    return httpx.MockTransport(simulated_handler(provider_key, overrides))


def build_simulated_providers(
        *, api_key: str = "simulated-no-credential") -> dict:
    """Providers for the app runtime — every transport is a MockTransport
    emitting documented shapes with synthetic data."""
    from app.domain.workers.rest_providers import REST_PROVIDER_KEYS
    return {key: make_provider(key, transport=simulated_transport(key),
                               api_key=api_key)
            for key in REST_PROVIDER_KEYS}


# ---------- C3-A — simulated image fixtures ----------

_SIZE_PIXELS = {"1024x1024": (1024, 1024), "1792x1024": (1792, 1024),
                "1024x1792": (1024, 1792)}


def _sim_image_png(prompt: str, seed: int, size: str) -> bytes:
    """Deterministic labeled test PNG — same request → same bytes.

    A workflow test artifact: colored gradient + SIMULATED stamp, never
    a representation of real provider image quality."""
    import io
    from PIL import Image, ImageDraw
    w, h = _SIZE_PIXELS.get(size, (1024, 1024))
    scale = max(1, min(w, h) // 512)   # keep fixtures small in memory
    w, h = w // scale, h // scale
    digest = hashlib.sha256(
        f"{prompt}|{seed}|{w}x{h}".encode()).digest()
    r, g, b = digest[0] // 2 + 40, digest[1] // 2 + 60, digest[2] // 2 + 80
    img = Image.new("RGB", (w, h), (14, 18, 28))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        draw.line([(0, y), (w, y)],
                  fill=(int(r * t), int(g * t), int(b * t)))
    draw.rectangle([8, 8, w - 8, h - 8], outline=(255, 196, 66), width=3)
    draw.text((16, 16), "SIMULATED — no provider contact",
              fill=(255, 220, 130))
    draw.text((16, 34), (prompt or "")[:80], fill=(230, 238, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def openai_images_obj(*, prompt: str, seed: int, size: str,
                      usage: dict | None = None) -> dict:
    """Documented Images-API object shape (subset)."""
    import base64
    png = _sim_image_png(prompt, seed, size)
    return {
        "created": 1763000000,
        "data": [{"b64_json": base64.b64encode(png).decode(),
                  "revised_prompt": (prompt or "")[:256]}],
        "usage": usage or {"input_tokens": 18, "output_tokens": 900,
                           "total_tokens": 918},
    }


def simulated_image_handler(provider_key: str,
                            overrides: dict[str, Callable] | None = None):
    """In-memory handler for image provider routes (C3-A fixtures)."""
    seq = {"n": 0}

    if provider_key == "openai-images":
        def submit(req: httpx.Request) -> httpx.Response:
            seq["n"] += 1
            body = json.loads(req.content or b"{}")
            return _json(openai_images_obj(
                prompt=body.get("prompt") or "",
                seed=int(body.get("seed") or 0),
                size=body.get("size") or "1024x1024"))

        routes = {"submit": submit, **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" \
                    and req.url.path == "/v1/images/generations":
                return routes["submit"](req)
            return _json({"error": {"type": "not_found",
                                    "message": "route"}}, 404)
        return handler

    # ---- C3-C fixtures — documented shapes, synthetic bytes ----

    if provider_key == "gemini-images":
        def submit(req: httpx.Request) -> httpx.Response:
            seq["n"] += 1
            model = req.url.path.split("/")[-1].split(":")[0]
            body = json.loads(req.content or b"{}")
            if req.url.path.endswith(":predict"):
                # Imagen :predict → predictions[bytesBase64Encoded]
                prompt = ((body.get("instances") or [{}])[0]
                          .get("prompt") or "")
                png = base64.b64encode(
                    _sim_image_png(prompt, 0, "1024x1024")).decode()
                return _json({"predictions": [{
                    "bytesBase64Encoded": png,
                    "mimeType": "image/png"}]})
            # generateContent → candidates[].content.parts[].inlineData
            prompt = (((body.get("contents") or [{}])[0]
                       .get("parts") or [{}])[0].get("text") or "")
            png = base64.b64encode(
                _sim_image_png(prompt, 0, "1024x1024")).decode()
            return _json({
                "candidates": [{
                    "content": {"role": "model",
                                "parts": [{"inlineData": {
                                    "mimeType": "image/png",
                                    "data": png}}]},
                    "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 12,
                                  "candidatesTokenCount": 1290,
                                  "totalTokenCount": 1302},
                "modelVersion": model})

        routes = {"submit": submit, **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if (req.method == "POST"
                    and req.url.path.startswith("/v1beta/models/")
                    and (req.url.path.endswith(":generateContent")
                         or req.url.path.endswith(":predict"))):
                return routes["submit"](req)
            return _json({"error": {"code": 404, "status": "NOT_FOUND",
                                    "message": "route"}}, 404)
        return handler

    if provider_key == "recraft":
        def submit(req: httpx.Request) -> httpx.Response:
            seq["n"] += 1
            body = json.loads(req.content or b"{}")
            png = base64.b64encode(_sim_image_png(
                body.get("prompt") or "", 0,
                body.get("size") or "1024x1024")).decode()
            return _json({
                "created": 1763000000,
                "data": [{"b64_json": png}],
                # API units charged for the request — sim reports the
                # documented per-image unit price of the model.
                "credits": 35,
            })

        routes = {"submit": submit, **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" \
                    and req.url.path == "/v1/images/generations":
                return routes["submit"](req)
            return _json({"error": {"type": "not_found",
                                    "message": "route"}}, 404)
        return handler

    if provider_key == "ideogram":
        def submit(req: httpx.Request) -> httpx.Response:
            seq["n"] += 1
            return _json({
                "created": "2026-11-13T00:00:00.000Z",
                "data": [{
                    "url": f"https://cdn.ideogram.ai/sim/img_{seq['n']:04d}.png",
                    "seed": 777000 + seq["n"],
                    "resolution": "1024x1024",
                    "is_image_safe": True,
                }]})

        routes = {"submit": submit, **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "POST" \
                    and req.url.path == "/v2/image/generate/ideogram-3":
                return routes["submit"](req)
            # Documented temporary artifact URL → synthetic bytes.
            if req.method == "GET" and req.url.path.startswith("/sim/"):
                return httpx.Response(
                    200, content=_sim_image_png("ideogram sim", 0,
                                                "1024x1024"),
                    headers={"Content-Type": "image/png"})
            return _json({"error": "route"}, 404)
        return handler

    if provider_key == "bfl-flux":
        poll_count = {"n": 0}

        def submit(req: httpx.Request) -> httpx.Response:
            seq["n"] += 1
            rid = f"sim-flux-{seq['n']:04d}"
            return _json({
                "id": rid,
                "polling_url": f"https://api.bfl.ai/v1/get_result?id={rid}",
                "status": "Pending"})

        def poll(req: httpx.Request) -> httpx.Response:
            # Deterministic lifecycle: first poll Pending, then Ready —
            # exercises the async driver across at least two ticks.
            poll_count["n"] += 1
            rid = req.url.params.get("id") or f"sim-flux-{seq['n']:04d}"
            if poll_count["n"] < 2:
                return _json({"id": rid, "status": "Pending",
                              "progress": 0.5})
            return _json({
                "id": rid, "status": "Ready",
                "result": {"sample":
                           f"https://delivery.bfl.ai/sim/{rid}.png",
                           "seed": 4242}})

        routes = {"submit": submit, "poll": poll,
                  **(overrides or {})}

        def handler(req: httpx.Request) -> httpx.Response:
            if (req.method == "POST"
                    and req.url.path.startswith("/v1/")
                    and "get_result" not in req.url.path):
                return routes["submit"](req)
            if req.method == "GET" \
                    and req.url.path == "/v1/get_result":
                return routes["poll"](req)
            # delivery.bfl.ai temporary artifact → synthetic bytes.
            if req.method == "GET" and req.url.path.startswith("/sim/"):
                return httpx.Response(
                    200, content=_sim_image_png("flux sim", 0,
                                                "1024x1024"),
                    headers={"Content-Type": "image/png"})
            return _json({"error": "route"}, 404)
        return handler

    raise KeyError(f"no simulated image transport for {provider_key!r}")


def simulated_image_transport(
        provider_key: str,
        overrides: dict[str, Callable] | None = None) -> httpx.MockTransport:
    return httpx.MockTransport(
        simulated_image_handler(provider_key, overrides))


def build_simulated_image_providers(
        *, api_key: str = "simulated-no-credential") -> dict:
    """Image providers for the app runtime — MockTransport fixtures
    emitting documented image shapes; zero egress by construction."""
    from app.domain.workers.rest_image_providers import (
        IMAGE_REST_KEYS, make_image_provider)
    return {key: make_image_provider(
                key, transport=simulated_image_transport(key),
                api_key=api_key)
            for key in IMAGE_REST_KEYS}
