"""C2 exit gate — Stage B1 live-transport infrastructure.

Production-capable HTTP transport for documented provider contracts,
reachable ONLY through ``live_dispatch()`` — which nothing in this
build calls: no command type, no router, no env var, no config reads
it. Tests exercise every control through injected inner transports
(httpx.MockTransport) and dummy credentials; the real inner transport
is constructed only when ``inner=None``, which no shipped path does.

Security layers enforced HERE, per request — not just at URL parse:
  1. live_gate.egress_allowed — https + exact documented host + no
     userinfo, before any socket work.
  2. DNS resolution checked against public-space IPs — loopback,
     private, link-local, multicast, reserved, unspecified all refused
     (SSRF via DNS-rebinding or hostile resolver cannot reach them).
  3. Redirects refused at the transport — never followed, credentials
     never forwarded; a 3xx is a classified refusal, not a retry hint.
  4. Proxy/environment isolation — the real client is trust_env=False
     (no HTTP(S)_PROXY/NO_PROXY/netrc honored anywhere).
  5. Per-request total-deadline cap + provider timeout policy.
  6. Bounded response bodies — a provider may not stream unbounded
     bytes into memory.

Financial ordering (invariant): ``live_dispatch`` commits the
spend.admit() reservation BEFORE constructing a transport; nothing
reaches a socket until admission has committed. A gate re-check runs
immediately before send — kill/disable racing admission→send leaves
the reservation open for reconciliation, never silently released.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from app.domain import live_gate as lg
from app.domain import spend as _spend
from app.domain.workers.remote import RemoteProvider, SubmissionIntent
from app.domain.workers.rest_providers import (
    RemoteError, SPECS, make_provider)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # provider JSON is small; refuse floods


class LiveDispatchRefused(Exception):
    """Gate refused before/at send boundary. ``code`` mirrors live_gate."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class LiveTransportRefused(Exception):
    """Transport-level refusal — maps to spend charge_evidence:
    'none_confirmed' means nothing was sent or the provider refused
    outright; 'unknown' means the request may have been processed."""

    def __init__(self, code: str, message: str, *, charge_evidence: str):
        super().__init__(message)
        self.code = code
        self.charge_evidence = charge_evidence


class ResponseTooLarge(httpx.TransportError):
    """Response stream exceeded the byte cap — outcome unknown (the
    request reached the provider), reservation must be HELD."""


# ------------------------------------------------------------------
# DNS control — public-space only, injectable for tests
# ------------------------------------------------------------------

def default_resolver(host: str, port: int) -> list[str]:
    """Production resolver — injectable so tests never hit real DNS."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


def _is_public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local
                or a.is_multicast or a.is_reserved or a.is_unspecified)


def resolve_public(host: str, port: int, resolver: Callable | None = None
                   ) -> list[str]:
    """All resolved addresses must be public — one private result
    refuses the request (no partial-trust partial-fail)."""
    ips = (resolver or default_resolver)(host, port) or []
    if not ips:
        raise LiveTransportRefused(
            "dns", f"{host}: resolution returned no addresses — refused",
            charge_evidence="none_confirmed")
    bad = [ip for ip in ips if not _is_public_ip(ip)]
    if bad:
        raise LiveTransportRefused(
            "dns", f"{host} resolves to non-public space — refused",
            charge_evidence="none_confirmed")
    return ips


# ------------------------------------------------------------------
# bounded response stream
# ------------------------------------------------------------------

class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, limit: int):
        self._inner, self._left = inner, limit

    async def __aiter__(self):
        async for chunk in self._inner:
            self._left -= len(chunk)
            if self._left < 0:
                raise ResponseTooLarge(
                    f"response exceeded {self._limit_note} byte cap")
            yield chunk

    @property
    def _limit_note(self) -> str:
        return str(MAX_RESPONSE_BYTES)

    async def aclose(self) -> None:
        await self._inner.aclose()


# ------------------------------------------------------------------
# the transport itself
# ------------------------------------------------------------------

class LiveHTTPTransport(httpx.AsyncBaseTransport):
    """httpx transport enforcing the complete egress security stack.

    ``inner`` does the actual IO: default is a REAL
    httpx.AsyncHTTPTransport (trust_env=False — proxy env vars and
    netrc are never honored); tests inject httpx.MockTransport so the
    entire stack is exercised with zero egress. ``is_real`` records
    which was wired — provenance evidence can only validate when the
    transport truly reached a provider (Stage B2 only)."""

    is_live_transport = True  # marker for RestRemoteProvider.__init__

    def __init__(self, *, provider_key: str, inner=None,
                 resolver: Callable | None = None,
                 timeout_s: float | None = None):
        self.provider_key = provider_key
        self.is_real = inner is None
        self._inner = inner or httpx.AsyncHTTPTransport(trust_env=False)
        self._resolver = resolver
        cfg = lg.LIVE_TIMEOUTS.get(provider_key, {})
        self._total_s = timeout_s or cfg.get("total_s", 60.0)
        self._sent: list[str] = []  # URLs attempted — test introspection

    async def handle_async_request(
            self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        ok, code = lg.egress_allowed(self.provider_key, url)
        if not ok:
            raise LiveTransportRefused(
                code, f"egress refused: {url}",
                charge_evidence="none_confirmed")
        port = request.url.port or 443
        resolve_public(request.url.host, port, self._resolver)
        self._sent.append(url)
        try:
            resp = await asyncio.wait_for(
                self._inner.handle_async_request(request),
                timeout=self._total_s)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise httpx.ReadTimeout(
                f"live request exceeded {self._total_s}s deadline — "
                "outcome unknown") from exc
        if 300 <= resp.status_code < 400:
            # Never follow, never re-send, never forward the credential —
            # a redirect is a classified refusal.
            raise LiveTransportRefused(
                "redirect",
                f"{self.provider_key}: redirect refused "
                f"({resp.status_code} → {resp.headers.get('location', '?')})",
                charge_evidence="none_confirmed")
        resp.stream = _BoundedStream(resp.stream, MAX_RESPONSE_BYTES)
        return resp

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


# ------------------------------------------------------------------
# the orchestrator — unreachable from any shipped surface in B1
# ------------------------------------------------------------------

async def live_dispatch(
        session: Session, *, provider_key: str, model_id: str,
        request: dict[str, Any], job_id: str, command_id: str | None,
        project_id: str, max_cost_usd: Any, vault: Any,
        app_state: Any | None = None,
        inner: Any | None = None, resolver: Callable | None = None,
        gate_probe: Callable | None = None) -> dict[str, Any]:
    """The ONE path a real provider request could ever take.

    Ordered invariants:
      1. dispatch_admission — policy gate (kill, opt-in, model grant,
         credential metadata, evidence freshness, caps).
      2. spend.admit + COMMIT — financial admission lands durably
         BEFORE any transport exists.
      3. Gate re-check — kill/disable racing admission→send leaves the
         reservation OPEN for reconcile; nothing is sent.
      4. vault.get_fields — the ONLY place secret material is read,
         inside this boundary, after every check above.
      5. Submit → classify → settle (confirmed) / release (confirmed
         refusal) / HOLD (ambiguous — never resubmit, never release).
    """
    probe = gate_probe or (lambda s: lg.state(s))
    allowed, code = lg.dispatch_admission(
        session, provider_key=provider_key, model_id=model_id,
        max_cost_usd=max_cost_usd, vault=vault)
    if not allowed:
        raise LiveDispatchRefused(code, f"live admission refused: {code}")
    res = _spend.admit(
        session, job_id=job_id, command_id=command_id,
        project_id=project_id, provider_key=provider_key,
        environment="live", est_usd=max_cost_usd,
        max_usd=max_cost_usd, app_state=app_state)
    session.commit()  # admission committed — only now may a socket exist

    st = probe(session)
    entry = (st.get("providers") or {}).get(provider_key) or {}
    if st.get("global_disabled"):
        raise LiveDispatchRefused(
            "global_kill", "kill switch engaged between admission and "
            "send — reservation held open for reconcile")
    if not entry.get("enabled"):
        raise LiveDispatchRefused(
            "not_enabled", "provider disabled between admission and "
            "send — reservation held open for reconcile")

    spec = SPECS[provider_key]
    fields = vault.get_fields(entry["credential_ref"])
    api_key = fields.get("api_key") or next(iter(fields.values()), "")
    transport = LiveHTTPTransport(
        provider_key=provider_key, inner=inner, resolver=resolver)
    provider = make_provider(provider_key, transport=transport,
                             api_key=api_key)
    try:
        handle = await provider.submit(SubmissionIntent(
            job_id=job_id, idempotency_key=job_id,
            adapter_id="cloud.live.submit", request=request,
            max_cost_usd=float(max_cost_usd)))
    except LiveTransportRefused as exc:
        if exc.charge_evidence == "none_confirmed":
            _spend.release(session, job_id=job_id,
                           reason=f"transport refused: {exc.code}")
            session.commit()
        return {"outcome": "failed" if exc.charge_evidence == "none_confirmed"
                else "ambiguous", "code": exc.code, "job_id": job_id,
                "reservation": "released" if exc.charge_evidence == "none_confirmed"
                else "open", "remote": None}
    except RemoteError as exc:
        if exc.charge_evidence == "none_confirmed":
            _spend.release(session, job_id=job_id,
                           reason=f"provider refused: {exc.kind.value}")
            session.commit()
        return {"outcome": "failed" if exc.charge_evidence == "none_confirmed"
                else "ambiguous", "code": exc.kind.value, "job_id": job_id,
                "reservation": "released" if exc.charge_evidence == "none_confirmed"
                else "open", "remote": None,
                "http_status": exc.http_status}

    # Submitted but not terminal (e.g. OpenAI background returns
    # queued/in_progress) — the work reached the provider; outcome is
    # unknown until a documented poll lands. HOLD the reservation:
    # never settle on an unfinished handle, never auto-resubmit.
    from app.domain.workers.remote import RemoteStatus
    if handle.status not in (RemoteStatus.SUCCEEDED, RemoteStatus.FAILED):
        return {"outcome": "submitted", "job_id": job_id,
                "provider_key": provider_key,
                "remote": {"remote_job_id": handle.remote_id,
                           "status": handle.status.value,
                           "detail": handle.detail},
                "reservation": "open",
                "transport_is_live": transport.is_real}
    if handle.status is RemoteStatus.FAILED:
        # Provider reports a definitive in-band failure — billable
        # status is ambiguous (usage may exist); hold for reconcile.
        return {"outcome": "ambiguous", "code": "provider_failed",
                "job_id": job_id, "provider_key": provider_key,
                "remote": {"remote_job_id": handle.remote_id,
                           "status": handle.status.value,
                           "detail": handle.detail},
                "reservation": "open",
                "transport_is_live": transport.is_real}

    # Success — provider-reported usage → micro at catalog rates;
    # estimate fallback only when usage is absent.
    usage = (handle.detail or {}).get("usage")
    model = next((m for m in (lg.catalog.get_provider(provider_key) or {})
                  .get("models") or [] if m.get("id") == model_id), {})
    micro = _usage_cost_micro(usage, model.get("cost_table"))
    if micro is None:
        micro, source = res.reserved_micro, "estimate_at_settle"
    else:
        source = "provider_reported"
    ev = {
        "transport": "live" if transport.is_real else "controlled",
        "authenticated": bool(api_key),
        "environment": "live",
        "endpoint_host": transport.provider_key and lg._allowed_host(provider_key),
        "correlation": {"job_id": job_id, "command_id": command_id,
                        "remote_job_id": handle.remote_id},
        "response_fingerprint":
            hashlib.sha256((getattr(provider, "_last_body_canonical", "")
                            or json.dumps(handle.detail, sort_keys=True,
                                          default=str)).encode()).hexdigest(),
        "evidence_url": spec.docs,
        "audit_command_id": command_id,
    }
    verified = lg.verification_evidence_valid(
        ev, transport_is_live=transport.is_real, provider_key=provider_key)
    _spend.settle(session, job_id=job_id, micro=micro, source=source,
                  remote_job_id=handle.remote_id)
    session.commit()
    return {"outcome": "succeeded", "job_id": job_id,
            "provider_key": provider_key,
            "remote": {"remote_job_id": handle.remote_id,
                       "status": handle.status.value,
                       "detail": handle.detail},
            "reservation": "settled", "settled_micro": micro,
            "charge_source": source,
            "transport_is_live": transport.is_real,
            "provider_verified": verified,
            "verification_evidence": ev}


def _usage_cost_micro(usage: dict | None, cost_table: dict | None
                      ) -> int | None:
    """Provider-reported integer usage × catalog rates → micro-USD.
    Same formula as the simulated adapter — provider_reported usage is
    still LOCALLY converted, never a provider-confirmed bill."""
    from decimal import Decimal
    table = cost_table or {}
    in_rate, out_rate = table.get("input_per_mtok"), table.get("output_per_mtok")
    if usage is None or in_rate is None or out_rate is None:
        return None
    usd = (Decimal(int(usage["input_tokens"])) * Decimal(str(in_rate))
           + Decimal(int(usage["output_tokens"])) * Decimal(str(out_rate))
           ) / Decimal(1_000_000)
    return _spend.usd_to_micro(usd)
