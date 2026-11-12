"""C2.0c — simulated HTTP transports for REST provider contracts.

``httpx.MockTransport`` handlers that reproduce the DOCUMENTED response
shapes of each provider (status fields, ids, usage objects) with
deterministic synthetic data. These are fixtures, not providers — no
socket is ever opened. Every response is produced in-process; usage
numbers are invented test data and feed only the ``simulated``
accounting environment.
"""

from __future__ import annotations

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

    raise KeyError(f"no simulated transport for {provider_key!r}")


def simulated_transport(provider_key: str,
                        overrides: dict[str, Callable] | None = None,
                        ) -> httpx.MockTransport:
    return httpx.MockTransport(simulated_handler(provider_key, overrides))


def build_simulated_providers(
        *, api_key: str = "simulated-no-credential") -> dict:
    """Providers for the app runtime — every transport is a MockTransport
    emitting documented shapes with synthetic data."""
    return {key: make_provider(key, transport=simulated_transport(key),
                               api_key=api_key)
            for key in ("openai", "anthropic")}
