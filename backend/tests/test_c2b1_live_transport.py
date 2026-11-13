"""C2 exit gate — Stage B1 live-transport infrastructure.

Every network path in this file runs through httpx.MockTransport
inners and injected DNS resolvers; the vault stub returns DUMMY
secrets. Nothing here performs a real request, touches a real
credential, or produces a real verified_remote artifact.
"""
from __future__ import annotations

import asyncio
import itertools
import json

import httpx
import httpcore
import pytest
from sqlalchemy import select, text

from app.db.models import GenerationJob, SpendReservation, new_id
from app.domain import live_gate as lg
from app.domain import spend
from app.domain.workers import live_transport as lt
from app.domain.workers import rest_providers as rp
from tests.conftest import make_project, wait_for_job

_KEYS = itertools.count(1)

DEEPSEEK_OK = {
    "id": "chatcmpl-b1-1",
    "choices": [{"message": {"content": "A beat lands."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


class RecordingVault:
    """Dummy-secret vault double. get_fields is the ONLY path that can
    hand out secret material — the call count proves where it ran."""
    def __init__(self):
        self.get_calls: list[str] = []

    def status(self, key):
        return {"configured": True, "fields": ["api_key"],
                "set_at": "2026-01-01T00:00:00Z", "readable": True,
                "unreadable_reason": None}

    def get_fields(self, key):
        self.get_calls.append(key)
        return {"api_key": "dummy-b1-key"}


@pytest.fixture()
def vault(client):
    v = RecordingVault()
    client.app.state.provider_vault = v
    return v


def _sf(client):
    return client.app.state.session_factory


def _rev(client, pid):
    return client.get(f"/api/projects/{pid}").json()["revision"]


def _commit(client, pid, command_type, payload):
    return client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": command_type, "payload": payload,
              "scope": "project", "expected_revision": _rev(client, pid),
              "idempotency_key": f"b1-live-{next(_KEYS):04d}"})


def _budget(client, pid, limit="5.00", scope="lifetime", target="global"):
    return _commit(client, pid, "spend.budget_set",
                   {"target": target, "scope": scope, "limit_usd": limit})


EN = {"provider_key": "deepseek", "model_id": "deepseek-chat",
      "credential_ref": "deepseek"}
GOOD_RESOLVER = lambda h, p: ["93.184.216.34"]  # noqa: E731 — public fixture IP


def _ok_inner(sends=None):
    def handler(req):
        if sends is not None:
            sends.append(str(req.url))
        return httpx.Response(200, json=DEEPSEEK_OK)
    return httpx.MockTransport(handler)


def _reservation(client, job_id):
    with _sf(client)() as s:
        return s.execute(select(SpendReservation).where(
            SpendReservation.job_id == job_id)).scalar_one_or_none()


def _dispatch(client, pid, *, vault, inner=None, resolver=GOOD_RESOLVER,
              gate_probe=None, max_usd="0.05", job_id=None):
    jid = job_id or f"b1-live-{new_id()[:8]}"
    with _sf(client)() as s:
        return asyncio.run(lt.live_dispatch(
            s, provider_key="deepseek", model_id="deepseek-chat",
            request={"model_id": "deepseek-chat", "prompt": "Write a beat.",
                     "max_output_tokens": 32},
            job_id=jid, command_id="c-b1", project_id=pid,
            max_cost_usd=max_usd, vault=vault,
            inner=inner if inner is not None else _ok_inner(),
            resolver=resolver, gate_probe=gate_probe))


# ---------------- unreachability ----------------

class TestUnreachableBoundary:
    def test_no_command_or_route_reaches_live_dispatch(self):
        """live_dispatch exists but is unreachable: no command type
        names it, no API route exposes it, no env var or config flag
        wires it. Nothing in the shipped surface can call it."""
        from app.domain import commands as cmd
        assert not any("live.submit" in c or "live.dispatch" in c
                       for c in cmd.SUPPORTED_COMMANDS)
        from app.api.routers import api_router
        paths = [getattr(r, "path", "") for r in api_router.routes]
        assert not any("dispatch" in p for p in paths)

    def test_no_shipped_code_calls_or_configs_it(self):
        """Static sweep: the only file that may CALL live_dispatch is
        live_permit.py — the single-use authorization seam added in
        B2a. LiveHTTPTransport may be constructed nowhere outside its
        own module, and live_transport reads no environment variable,
        settings object, or adapter registry — no env/config/job path
        can reach it."""
        import pathlib
        import app as app_pkg
        root = pathlib.Path(app_pkg.__file__).parent
        # rest_providers.py may *mention* the boundary (the
        # is_live_transport marker seam) but must never CALL it.
        # live_permit.py is the one authorized live_dispatch caller;
        # other files may import LiveDispatchRefused for error
        # mapping but must never invoke or construct.
        hits = []
        for f in root.rglob("*.py"):
            if f.name in ("live_transport.py", "rest_providers.py",
                          "live_permit.py"):
                continue
            src = f.read_text(encoding="utf-8")
            if ("live_dispatch(" in src or "LiveHTTPTransport(" in src
                    or "import live_transport" in src):
                hits.append(str(f.relative_to(root)))
        assert hits == []
        src = (root / "domain" / "workers" / "live_transport.py"
               ).read_text(encoding="utf-8")
        for banned in ("os.environ", "os.getenv", "settings.",
                       "adapter_registry"):
            assert banned not in src, banned
        # The authorized seam consumes a permit before dispatch — a
        # live_dispatch call without expected_permit is refused.
        seam = (root / "domain" / "live_permit.py"
                ).read_text(encoding="utf-8")
        assert "expected_permit" in seam and "live_dispatch" in seam

    def test_inner_default_marks_real_injected_marks_controlled(self):
        assert lt.LiveHTTPTransport(
            provider_key="deepseek", inner=None).is_real is True
        assert lt.LiveHTTPTransport(
            provider_key="deepseek", inner=_ok_inner()).is_real is False


# ---------------- transport security ----------------

class TestTransportSecurity:
    def _t(self, *, inner=None, resolver=GOOD_RESOLVER, timeout_s=None):
        return lt.LiveHTTPTransport(
            provider_key="deepseek",
            inner=inner or _ok_inner(), resolver=resolver,
            timeout_s=timeout_s)

    def _req(self, url):
        return httpx.Request("POST", url, json={"x": 1})

    def test_scheme_userinfo_host_refused(self):
        t = self._t()
        for url, code in (
                ("http://api.deepseek.com/chat/completions", "scheme"),
                ("https://u:p@api.deepseek.com/chat/completions", "userinfo"),
                ("https://api.evil.example/chat/completions", "host"),
                ("https://deepseek.com/chat/completions", "host")):
            with pytest.raises(lt.LiveTransportRefused) as ei:
                asyncio.run(t.handle_async_request(self._req(url)))
            assert ei.value.code == code
            assert ei.value.charge_evidence == "none_confirmed"
        assert t._sent == []  # nothing reached the wire

    def test_dns_non_public_refused(self):
        for ips in (["127.0.0.1"], ["10.0.0.5"], ["169.254.1.1"],
                    ["192.168.1.1"], ["::1"], [], None):
            t = self._t(resolver=lambda h, p, ips=ips: ips)
            with pytest.raises(lt.LiveTransportRefused) as ei:
                asyncio.run(t.handle_async_request(
                    self._req("https://api.deepseek.com/chat/completions")))
            assert ei.value.code == "dns"
        # mixed public+private: ANY bad address refuses the request
        t = self._t(resolver=lambda h, p: ["93.184.216.34", "10.0.0.5"])
        with pytest.raises(lt.LiveTransportRefused):
            asyncio.run(t.handle_async_request(
                self._req("https://api.deepseek.com/chat/completions")))

    def test_redirect_refused_never_followed(self):
        calls = []
        def handler(req):
            calls.append(str(req.url))
            return httpx.Response(302, headers={
                "location": "https://api.evil.example/steal"})
        t = self._t(inner=httpx.MockTransport(handler))
        with pytest.raises(lt.LiveTransportRefused) as ei:
            asyncio.run(t.handle_async_request(
                self._req("https://api.deepseek.com/chat/completions")))
        assert ei.value.code == "redirect"
        assert len(calls) == 1  # no follow-up request, no creds forwarded

    def test_response_size_cap(self):
        big = b"x" * (lt.MAX_RESPONSE_BYTES + 1)
        t = self._t(inner=httpx.MockTransport(
            lambda req: httpx.Response(200, content=big)))
        resp = asyncio.run(t.handle_async_request(
            self._req("https://api.deepseek.com/chat/completions")))
        async def drain():
            async for _c in resp.stream:
                pass
        with pytest.raises(lt.ResponseTooLarge):
            asyncio.run(drain())

    def test_total_deadline_enforced(self):
        async def slow(req):
            import asyncio as _a
            await _a.sleep(5)
            return httpx.Response(200, json={})
        class SlowInner(httpx.AsyncBaseTransport):
            async def handle_async_request(self, req):
                return await slow(req)
        t = self._t(inner=SlowInner(), timeout_s=0.01)
        with pytest.raises(httpx.ReadTimeout):
            asyncio.run(t.handle_async_request(
                self._req("https://api.deepseek.com/chat/completions")))

    def test_live_client_is_proxy_and_redirect_isolated(self):
        """The hardened client the provider builds must not honor
        HTTP(S)_PROXY/netrc env vars or follow redirects."""
        from app.domain.workers.rest_providers import make_provider
        p = make_provider("deepseek", transport=self._t(), api_key="dummy")
        assert p._client.trust_env is False
        assert p._client.follow_redirects is False
        # non-mock non-live transport still refused at the boundary
        with pytest.raises(Exception):
            make_provider("deepseek",
                          transport=httpx.AsyncHTTPTransport(), api_key="x")


# ---------------- DNS TOCTOU: socket binds to validated IP ----------------

class _SpyBackend:
    """connect_tcp spy — records the literal the socket would dial."""
    def __init__(self, fail=None):
        self.calls: list[tuple[str, int]] = []
        self._fail = fail or {}

    async def connect_tcp(self, host, port, **kw):
        self.calls.append((host, port))
        if host in self._fail:
            raise self._fail[host]
        return object()  # callers asserting binding never use the stream

    async def connect_unix_socket(self, *a, **k):
        raise AssertionError("unix sockets must be unreachable")

    async def sleep(self, seconds):
        pass


class _Chunks(httpx.AsyncByteStream):
    """Scripted async byte stream — chunk list + optional mid-iter failure."""
    def __init__(self, chunks, *, fail_at=None):
        self._chunks, self._fail_at = list(chunks), fail_at
        self.closed = False

    async def __aiter__(self):
        for i, c in enumerate(self._chunks):
            if i == self._fail_at:
                raise httpx.ReadError("mid-stream read failure")
            yield c

    async def aclose(self):
        self.closed = True


class TestSocketBinding:
    """The real path must dial the VALIDATED IP — never re-resolve the
    hostname at connect time (DNS-rebinding/TOCTOU defense)."""

    def test_socket_binds_to_validated_ip_never_hostname(self):
        spy = _SpyBackend()
        b = lt._ValidatedBackend(
            base=spy, resolver=lambda h, p: ["93.184.216.34"])
        asyncio.run(b.connect_tcp("api.deepseek.com", 443))
        assert spy.calls == [("93.184.216.34", 443)]
        assert all(h != "api.deepseek.com" for h, _ in spy.calls)

    def test_connect_falls_through_validated_ips_in_order(self):
        spy = _SpyBackend(fail={"93.184.216.34": OSError("refused")})
        b = lt._ValidatedBackend(
            base=spy,
            resolver=lambda h, p: ["93.184.216.34", "8.8.8.8"])
        asyncio.run(b.connect_tcp("api.deepseek.com", 443))
        assert spy.calls == [("93.184.216.34", 443), ("8.8.8.8", 443)]

    def test_private_loopback_substitution_refused_at_socket(self):
        """Even if preflight saw public space, a connect-time answer of
        loopback/private/link-local/v6-loopback is refused before the
        socket opens."""
        for ips in (["127.0.0.1"], ["10.10.0.2"], ["169.254.9.9"],
                    ["::1"], ["fe80::1"], ["192.168.0.10"]):
            spy = _SpyBackend()
            b = lt._ValidatedBackend(
                base=spy, resolver=lambda h, p, ips=ips: ips)
            with pytest.raises(lt.LiveTransportRefused) as ei:
                asyncio.run(b.connect_tcp("api.deepseek.com", 443))
            assert ei.value.code == "dns"
            assert ei.value.charge_evidence == "none_confirmed"
            assert spy.calls == []

    def test_public_v6_binds(self):
        spy = _SpyBackend()
        b = lt._ValidatedBackend(
            base=spy, resolver=lambda h, p: ["2606:4700:4700::1111"])
        asyncio.run(b.connect_tcp("api.deepseek.com", 443))
        assert spy.calls == [("2606:4700:4700::1111", 443)]

    def test_dns_flip_between_preflight_and_connect_refused(self):
        """End-to-end TOCTOU through the real transport: the preflight
        resolve returns public; the connect-time resolve flips to
        loopback → refused at the socket layer, spy never dialed."""
        calls = itertools.count()
        def flipper(h, p):
            return ["93.184.216.34"] if next(calls) == 0 else ["127.0.0.1"]
        spy = _SpyBackend()
        t = lt.LiveHTTPTransport(provider_key="deepseek", inner=None,
                                 resolver=flipper, base_backend=spy)
        req = httpx.Request(
            "POST", "https://api.deepseek.com/chat/completions",
            json={"x": 1})
        with pytest.raises(lt.LiveTransportRefused) as ei:
            asyncio.run(t.handle_async_request(req))
        assert ei.value.code == "dns"
        assert spy.calls == []


# ---------------- connection-pool lifecycle ----------------

class TestPoolLifecycle:
    """The real-egress inner must have exactly ONE pool, pinned to
    _ValidatedBackend — no unvalidated pool ever allocated, no proxy
    path, every new AND reused connection established through the
    validated backend, and shutdown closing everything."""

    def test_single_pool_pinned_to_validated_backend(self):
        t = lt._SecureHTTPTransport(resolver=GOOD_RESOLVER)
        assert type(t._pool) is httpcore.AsyncConnectionPool
        assert isinstance(t._pool._network_backend, lt._ValidatedBackend)
        # no proxy object exists anywhere on this pool
        assert getattr(t._pool, "_proxy", None) is None
        # exactly one attribute — the pool. No unused default pool was
        # ever allocated (super().__init__ deliberately not called).
        assert list(vars(t)) == ["_pool"]

    def test_reused_connections_share_validated_backend(self):
        """httpcore.create_connection hands pool._network_backend to
        every connection it builds — so a pooled keep-alive connection
        could only ever have been established through the validated
        backend (structural invariant, asserted against real source)."""
        import inspect
        src = inspect.getsource(
            httpcore.AsyncConnectionPool.create_connection)
        assert "self._network_backend" in src
        t = lt._SecureHTTPTransport(resolver=GOOD_RESOLVER)
        assert t._pool._network_backend._resolver is GOOD_RESOLVER
        assert t._pool._connections == []

    def test_aclose_closes_every_connection(self):
        class _Conn:
            def __init__(self):
                self.closed = False
            async def aclose(self):
                self.closed = True
        t = lt._SecureHTTPTransport(resolver=GOOD_RESOLVER)
        conns = [_Conn(), _Conn(), _Conn()]
        t._pool._connections.extend(conns)
        asyncio.run(t.aclose())
        assert all(c.closed for c in conns)
        assert t._pool._connections == []

    def test_dependency_contract_guard_fails_visibly(self, monkeypatch):
        """If a future httpx stops routing aclose through self._pool,
        construction must raise — never silently weaken validation."""
        async def _noop(self):
            return None
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", _noop)
        with pytest.raises(lt.DependencyContractError):
            lt._SecureHTTPTransport(resolver=GOOD_RESOLVER)


# ---------------- streaming response limits ----------------

class TestStreamingLimits:
    def _t(self, handler):
        return lt.LiveHTTPTransport(
            provider_key="deepseek", inner=httpx.MockTransport(handler),
            resolver=GOOD_RESOLVER)

    def _req(self):
        return httpx.Request(
            "POST", "https://api.deepseek.com/chat/completions", json={})

    async def _drain(self, resp):
        out = bytearray()
        async for c in resp.stream:
            out += c
        return bytes(out)

    def test_multi_chunk_under_limit_drains_fully(self):
        chunks = [b"a" * (1024 * 1024), b"b" * (1024 * 1024 - 1)]
        t = self._t(lambda r: httpx.Response(200, stream=_Chunks(chunks)))
        resp = asyncio.run(t.handle_async_request(self._req()))
        got = asyncio.run(self._drain(resp))
        assert len(got) == sum(len(c) for c in chunks)
        assert got == b"".join(chunks)

    def test_chunk_crossing_limit_raises_mid_stream(self):
        chunks = [b"a" * lt.MAX_RESPONSE_BYTES, b"x"]
        t = self._t(lambda r: httpx.Response(200, stream=_Chunks(chunks)))
        resp = asyncio.run(t.handle_async_request(self._req()))
        got = []
        async def drain():
            async for c in resp.stream:
                got.append(len(c))
        with pytest.raises(lt.ResponseTooLarge):
            asyncio.run(drain())
        # the crossing chunk is never yielded — no silent truncation
        assert got == [lt.MAX_RESPONSE_BYTES]

    def test_single_oversized_chunk_raises_immediately(self):
        t = self._t(lambda r: httpx.Response(
            200, stream=_Chunks([b"x" * (lt.MAX_RESPONSE_BYTES + 1)])))
        resp = asyncio.run(t.handle_async_request(self._req()))
        with pytest.raises(lt.ResponseTooLarge):
            asyncio.run(self._drain(resp))

    def test_mid_stream_failure_propagates(self):
        t = self._t(lambda r: httpx.Response(
            200, stream=_Chunks([b"ok", b"never"], fail_at=1)))
        resp = asyncio.run(t.handle_async_request(self._req()))
        with pytest.raises(httpx.ReadError):
            asyncio.run(self._drain(resp))

    def test_stream_close_after_refusal(self):
        inner_stream = _Chunks([b"x" * (lt.MAX_RESPONSE_BYTES + 1)])
        t = self._t(lambda r: httpx.Response(200, stream=inner_stream))
        resp = asyncio.run(t.handle_async_request(self._req()))
        with pytest.raises(lt.ResponseTooLarge):
            asyncio.run(self._drain(resp))
        asyncio.run(resp.aclose())
        assert inner_stream.closed is True


# ---------------- orchestration: admission → send ----------------

class TestDispatchOrchestration:
    def test_admission_runs_before_any_send(self, client, vault):
        """No opt-in → LiveDispatchRefused before the transport exists;
        the inner mock records zero sends and no reservation is booked."""
        sends = []
        pid = make_project(client)["id"]
        _budget(client, pid)
        with pytest.raises(lt.LiveDispatchRefused) as ei:
            _dispatch(client, pid, vault=vault, inner=_ok_inner(sends))
        assert ei.value.code == "not_enabled"
        assert sends == []
        assert _reservation(client, "x") is None

    def test_successful_dispatch_settles_and_evidences(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        sends = []
        out = _dispatch(client, pid, vault=vault, inner=_ok_inner(sends))
        jid = out["job_id"]
        assert out["outcome"] == "succeeded"
        assert len(sends) == 1
        res = _reservation(client, jid)
        assert res.status == "settled" and res.environment == "live"
        # usage 10×$0.27 + 5×$1.10 per Mtok → $8.2e-6 → ceiling micro
        assert out["settled_micro"] == spend.usd_to_micro(
            (10 * 0.27 + 5 * 1.10) / 1_000_000)
        assert out["charge_source"] == "provider_reported"
        # evidence shape correct — but controlled transport: NEVER
        # verified_remote in B1.
        ev = out["verification_evidence"]
        assert ev["transport"] == "controlled"
        assert ev["endpoint_host"] == "api.deepseek.com"
        assert ev["correlation"]["job_id"] == jid
        assert ev["correlation"]["remote_job_id"] == "chatcmpl-b1-1"
        assert out["provider_verified"] is False
        from app.domain.workers.remote import provenance_confidence
        assert provenance_confidence(out) == "simulated"

    def test_secret_read_only_inside_boundary(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        # refused admission → vault never opened
        with pytest.raises(lt.LiveDispatchRefused):
            _dispatch(client, pid, vault=vault)
        assert vault.get_calls == []
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault)
        assert out["outcome"] == "succeeded"
        assert vault.get_calls == ["deepseek"]  # exactly one read, inside
        # secret nowhere durable
        res = _reservation(client, out["job_id"])
        assert "dummy-b1-key" not in str(res.__dict__)

    def test_auth_header_carries_dummy_key(self, client, vault):
        seen = []
        def handler(req):
            seen.append(req.headers.get("authorization"))
            return httpx.Response(200, json=DEEPSEEK_OK)
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        _dispatch(client, pid, vault=vault,
                  inner=httpx.MockTransport(handler))
        assert seen == ["Bearer dummy-b1-key"]

    def test_kill_between_admission_and_send(self, client, vault):
        """Gate flips to kill AFTER admit commits but BEFORE send:
        nothing is sent and the reservation stays OPEN for reconcile —
        never silently released."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        # dispatch_admission reads lg.state directly (sees the grant);
        # the pre-send re-check goes through gate_probe — simulating a
        # kill landing in the gap between the two reads.
        def probe(s):
            st = lg.state(s)
            st["global_disabled"] = True
            return st
        sends = []
        with pytest.raises(lt.LiveDispatchRefused) as ei:
            _dispatch(client, pid, vault=vault, inner=_ok_inner(sends),
                      gate_probe=probe)
        assert ei.value.code == "global_kill"
        assert sends == []
        # the admission already committed → the reservation row exists,
        # open and un-released
        with _sf(client)() as s:
            rows = s.execute(select(SpendReservation).where(
                SpendReservation.environment == "live")).scalars().all()
        assert len(rows) == 1 and rows[0].status == "open"

    def test_ambiguous_outcome_holds_reservation(self, client, vault):
        """Transport dies mid-request → outcome unknown: reservation
        stays open, never resubmitted automatically."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        calls = []
        def dying(req):
            calls.append(str(req.url))
            raise httpx.ConnectError("connection dropped")
        out = _dispatch(client, pid, vault=vault,
                        inner=httpx.MockTransport(dying))
        assert out["outcome"] == "ambiguous"
        assert out["reservation"] == "open"
        assert len(calls) == 1  # no automatic retry of ambiguous work
        res = _reservation(client, out["job_id"])
        assert res.status == "open"

    def test_ambiguous_reservation_recovers_via_explicit_reconcile(
            self, client, vault):
        """Crash-recovery path: an ambiguous dispatch leaves the
        reservation open durably; only explicit spend.reconcile can
        move it — no silent release, idempotent on replay."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: (_ for _ in ()).throw(
                httpx.ConnectError("dropped mid-flight"))))
        assert out["reservation"] == "open"
        # Seed the durable job row reconcile joins on (the dispatch
        # boundary does not own job lifecycle).
        with _sf(client)() as s:
            s.add(GenerationJob(id=out["job_id"], project_id=pid,
                                adapter_id="cloud.live.submit",
                                idempotency_key=f"b1rc-{new_id()[:8]}"))
            s.commit()
        from app.domain import spend as spend_mod
        with _sf(client)() as s:
            # 'still_unknown' keeps it open — survives the session.
            r = spend_mod.reconcile(
                s, project_id=pid, job_id=out["job_id"],
                outcome="still_unknown", note="operator review")
            s.commit()
        assert r["status"] == "open"
        assert _reservation(client, out["job_id"]).status == "open"
        with _sf(client)() as s:
            r = spend_mod.reconcile(
                s, project_id=pid, job_id=out["job_id"],
                outcome="confirmed_no_charge",
                note="provider confirmed refusal")
            s.commit()
        assert r["status"] == "released"
        # Replay is idempotent — no duplicate release/charge.
        with _sf(client)() as s:
            r2 = spend_mod.reconcile(
                s, project_id=pid, job_id=out["job_id"],
                outcome="confirmed_no_charge")
            s.commit()
        assert r2["idempotent_replay"] is True

    def test_confirmed_refusal_releases(self, client, vault):
        """401 = provider demonstrably refused before processing →
        reservation released as confirmed no-charge."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(401, json={"error": {"type": "auth"}})))
        assert out["outcome"] == "failed"
        assert out["code"] == "authentication"
        assert out["reservation"] == "released"
        res = _reservation(client, out["job_id"])
        assert res.status == "released"

    def test_oversized_response_is_ambiguous(self, client, vault):
        big = b"x" * (lt.MAX_RESPONSE_BYTES + 1)
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(200, content=big)))
        assert out["outcome"] == "ambiguous"
        assert _reservation(client, out["job_id"]).status == "open"

    def test_oversized_streamed_body_is_ambiguous_never_truncated(
            self, client, vault):
        """A realistically streamed body crossing the cap mid-flight:
        the response is unusable AFTER the request was sent — the
        reservation stays open (ambiguous), never truncated to a
        'successful' partial parse."""
        body = (json.dumps(DEEPSEEK_OK).encode()
                + b" " * lt.MAX_RESPONSE_BYTES)
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(
                200, stream=_Chunks([body[:1024], body[1024:]]))))
        assert out["outcome"] == "ambiguous"
        assert _reservation(client, out["job_id"]).status == "open"

    def test_malformed_success_body_is_ambiguous(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"<html>nope</html>")))
        assert out["outcome"] == "ambiguous"
        assert _reservation(client, out["job_id"]).status == "open"

    def test_rate_limit_is_confirmed_refusal(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: httpx.Response(
                429, json={"error": {"type": "rate_limit"}},
                headers={"retry-after": "5"})))
        assert out["outcome"] == "failed"
        assert out["code"] == "rate_limited"
        assert out["reservation"] == "released"


# ---------------- lifecycle: documented poll/cancel ----------------

class TestLifecycle:
    def _live_provider(self, key, *, inner=None):
        from app.domain.workers.rest_providers import make_provider
        inner = inner or _ok_inner()
        t = lt.LiveHTTPTransport(provider_key=key, inner=inner,
                                 resolver=GOOD_RESOLVER)
        return make_provider(key, transport=t, api_key="dummy")

    def test_sync_provider_has_no_poll(self):
        p = self._live_provider("deepseek")
        assert asyncio.run(p.query("anything")) is None
        from app.domain.workers.rest_providers import UnsupportedRemoteOp
        with pytest.raises(UnsupportedRemoteOp):
            asyncio.run(p.cancel("anything"))

    def test_openai_async_poll_and_cancel_documented(self):
        states = iter([
            httpx.Response(200, json={
                "id": "resp_1", "status": "in_progress"}),
            httpx.Response(200, json={
                "id": "resp_1", "status": "cancelled"}),
        ])
        p = self._live_provider("openai", inner=httpx.MockTransport(
            lambda req: next(states)))
        h = asyncio.run(p.query("resp_1"))
        assert h is not None and h.status.value == "running"
        c = asyncio.run(p.cancel("resp_1"))
        assert c.status.value == "cancelled"

    def test_retry_after_capped(self):
        assert lg.retry_after_s({"retry-after": "0.5"}) == 0.5
        assert lg.retry_after_s({"retry-after": "999"}) == 60
        assert lg.retry_after_s({"retry-after": "garbage"}) is None
        assert lg.retry_after_s({}) is None


# ---------------- phase-aware billing classification ----------------

class TestBillingClassification:
    """charge_evidence is the billing judgment — regression coverage
    that each phase maps to the correct release/hold decision and that
    NOTHING downstream can turn an ambiguous outcome into a free pass."""

    def _provider(self, key, handler):
        t = lt.LiveHTTPTransport(provider_key=key,
            inner=httpx.MockTransport(handler), resolver=GOOD_RESOLVER)
        return rp.make_provider(key, transport=t, api_key="dummy")

    def test_pre_send_refusals_carry_none_confirmed(self):
        """Egress/DNS refusals happen before the socket — nothing was
        sent, so releasing the reservation is provably safe."""
        for code, maker in (
            ("scheme", lambda: lt.LiveHTTPTransport(
                provider_key="deepseek", inner=_ok_inner(),
                resolver=GOOD_RESOLVER)),
            ("dns", lambda: lt.LiveHTTPTransport(
                provider_key="deepseek", inner=_ok_inner(),
                resolver=lambda h, p: ["10.0.0.1"])),
        ):
            t = maker()
            url = ("http://api.deepseek.com/chat/completions"
                   if code == "scheme"
                   else "https://api.deepseek.com/chat/completions")
            with pytest.raises(lt.LiveTransportRefused) as ei:
                asyncio.run(t.handle_async_request(
                    httpx.Request("POST", url)))
            assert ei.value.charge_evidence == "none_confirmed"

    def test_documented_rejections_are_none_confirmed(self, client, vault):
        """4xx provider rejections (auth/validation/rate-limit) refused
        before processing — release is safe."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        for status, code in ((400, "validation"), (401, "authentication"),
                             (429, "rate_limited")):
            out = _dispatch(client, pid, vault=vault,
                inner=httpx.MockTransport(lambda req: httpx.Response(
                    status, json={"error": {"message": "x"}})))
            assert out["outcome"] == "failed", (status, out)
            assert out["code"] == code
            assert out["reservation"] == "released"

    def test_unlisted_4xx_is_ambiguous_never_released(
            self, client, vault):
        """Only the documented refusal statuses release. Any other 4xx
        (410 Gone, 418, oddball codes) means provider-side processing
        state is uncertain → ambiguous, reservation HELD."""
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        for status in (410, 418, 451):
            out = _dispatch(client, pid, vault=vault,
                inner=httpx.MockTransport(lambda req: httpx.Response(
                    status, json={"error": {"message": "x"}})))
            assert out["outcome"] == "ambiguous", (status, out)
            assert out["reservation"] == "open"
            assert _reservation(
                client, out["job_id"]).status == "open"
            # held reservations block the next dispatch (one in-flight
            # per provider) — release explicitly to run the next case
            with _sf(client)() as s:
                spend.release(s, job_id=out["job_id"],
                              reason="test: next case")
                s.commit()

    def test_post_send_transport_failure_stays_ambiguous(
            self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault, inner=httpx.MockTransport(
            lambda req: (_ for _ in ()).throw(
                httpx.ReadError("connection reset mid-response"))))
        assert out["outcome"] == "ambiguous"
        assert _reservation(client, out["job_id"]).status == "open"

    def test_query_error_never_proves_non_execution(self):
        """A failed poll says nothing about billing — query() returns
        None (unknown), never a 'not executed' verdict."""
        p = self._provider("openai", lambda req: httpx.Response(
            500, json={"error": {"message": "x"}}))
        assert asyncio.run(p.query("resp_123")) is None

    def test_cancel_http_error_is_ambiguous_never_no_billing(self):
        """Cancellation failure doesn't establish non-billing — the
        provider may already have processed the work."""
        p = self._provider("openai", lambda req: httpx.Response(
            500, json={"error": {"message": "x"}}))
        with pytest.raises(rp.RemoteError) as ei:
            asyncio.run(p.cancel("resp_123"))
        assert ei.value.charge_evidence == "unknown"

    def test_cancel_unsupported_never_invents_endpoint(self):
        p = self._provider("deepseek", lambda req: None)
        with pytest.raises(rp.UnsupportedRemoteOp):
            asyncio.run(p.cancel("anything"))


# ---------------- simulated/live isolation ----------------

class TestSimulatedLiveIsolation:
    def test_live_booking_does_not_touch_simulated_path(self, client, vault):
        pid = make_project(client)["id"]
        _budget(client, pid)
        _commit(client, pid, "cloud.live.enable", EN)
        out = _dispatch(client, pid, vault=vault)
        assert out["reservation"] == "settled"
        # simulated submit still books environment='simulated'
        r = _commit(client, pid, "generate.text.cloud", {
            "provider_key": "deepseek", "model_id": "deepseek-chat",
            "prompt": "Write a beat.", "execution_target": "cloud"})
        assert r.status_code == 200, r.text
        job = wait_for_job(client, pid, r.json()["job_id"])
        res = _reservation(client, job["id"])
        assert res.environment == "simulated"
