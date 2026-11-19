"""C2.5 — issue #96 resolved-IP egress gate.

All tests use injected fake resolvers/connectors — zero real DNS,
zero sockets to anything but the fake. Proves: every resolved address
is validated, private/reserved/metadata/mapped ranges refuse, the pin
is what gets dialed (TOCTOU/rebinding dead), redirects re-run the gate,
size caps enforce, and refusals carry stable audit codes.
"""
from __future__ import annotations

import ipaddress
import socket

import pytest

from app.net import egress


def resolver_returning(*ips: str):
    """Fake getaddrinfo — same shape: (family, type, proto, canon, sa)."""
    def _r(host, port):
        out = []
        for ip in ips:
            v6 = ":" in ip
            out.append((socket.AF_INET6 if v6 else socket.AF_INET,
                        socket.SOCK_STREAM, 6, "", (ip, port)))
        return out
    return _r


def public(name="cdn.example.com", ip="93.184.216.34"):
    return resolver_returning(ip)(name, 443)


def code(fn, *a, **k):
    with pytest.raises(egress.EgressRefused) as e:
        fn(*a, **k)
    return e.value.code


# ---------- URL hygiene ----------

def test_scheme_refused():
    assert code(egress.check_url, "http://cdn.example.com/x") == "scheme"
    assert code(egress.check_url, "ftp://cdn.example.com/x") == "scheme"


def test_userinfo_refused():
    assert code(egress.check_url,
                "https://user:pw@cdn.example.com/x") == "userinfo"
    assert code(egress.check_url,
                "https://user@cdn.example.com/x") == "userinfo"


def test_offport_refused():
    assert code(egress.check_url,
                "https://cdn.example.com:8443/x") == "port"


def test_missing_host_refused():
    assert code(egress.check_url, "https:///x") == "host"


# ---------- blocked address space ----------

@pytest.mark.parametrize("ip", [
    "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255",
    "192.168.1.1", "127.0.0.1", "127.53.1.9",
    "169.254.1.1", "100.64.0.1", "100.127.255.254",
    "0.0.0.0", "224.0.0.1", "255.255.255.255", "240.0.0.1",
    "192.0.2.1", "198.51.100.1", "203.0.113.9", "198.18.0.5",
])
def test_v4_blocked_ranges(ip):
    assert code(egress.resolve_pinned,
                egress.check_url("https://cdn.example.com/x"),
                resolver=resolver_returning(ip)) == "ip_blocked"


@pytest.mark.parametrize("ip", [
    "::1", "fe80::1", "fec0::1", "fc00::1", "fd00::1",
    "ff02::1", "::", "2001:db8::1",
])
def test_v6_blocked_ranges(ip):
    assert code(egress.resolve_pinned,
                egress.check_url("https://cdn.example.com/x"),
                resolver=resolver_returning(ip)) == "ip_blocked"


def test_metadata_endpoints_named():
    assert code(egress.resolve_pinned,
                egress.check_url("https://cdn.example.com/x"),
                resolver=resolver_returning(
                    "169.254.169.254")) == "ip_metadata"


def test_v4_mapped_v6_unwrapped_and_refused():
    # ::ffff:10.0.0.1 is private space in disguise
    assert code(egress.resolve_pinned,
                egress.check_url("https://cdn.example.com/x"),
                resolver=resolver_returning(
                    "::ffff:10.0.0.1")) == "ip_blocked"
    assert code(egress.resolve_pinned,
                egress.check_url("https://cdn.example.com/x"),
                resolver=resolver_returning(
                    "::ffff:169.254.169.254")) == "ip_metadata"


def test_nat64_embedded_private_refused():
    # 64:ff9b::a00:1 embeds 10.0.0.1
    assert code(egress.resolve_pinned,
                egress.check_url("https://cdn.example.com/x"),
                resolver=resolver_returning(
                    "64:ff9b::a00:1")) == "ip_blocked"


def test_any_hostile_address_refuses_whole_name():
    # 3 clean + 1 hostile — ALL are validated, so the name is refused
    ips = ("93.184.216.34", "93.184.216.35", "10.1.2.3", "8.8.8.8")
    assert code(egress.resolve_pinned,
                egress.check_url("https://cdn.example.com/x"),
                resolver=resolver_returning(*ips)) == "ip_blocked"


def test_resolution_failure_refused():
    def boom(host, port):
        raise socket.gaierror("NXDOMAIN")
    assert code(egress.resolve_pinned,
                egress.check_url("https://gone.example.com/x"),
                resolver=boom) == "resolve"


def test_empty_resolution_refused():
    assert code(egress.resolve_pinned,
                egress.check_url("https://cdn.example.com/x"),
                resolver=lambda h, p: []) == "resolve"


# ---------- IP-literal hosts ----------

def test_private_ip_literal_refused():
    assert code(egress.check_url, "https://10.0.0.5/x") == "ip_blocked"
    assert code(egress.check_url, "https://[::1]/x") == "ip_blocked"
    assert code(egress.check_url,
                "https://[fe80::1]/x") == "ip_blocked"


def test_obfuscated_ip_literals_resolved_then_validated():
    # resolver treats 010.0.0.1 / 167772161 / 0x0a000001 as 10.0.0.1 —
    # the resolved address is what gets validated, so obfuscation
    # cannot smuggle a private target.
    for host in ("010.0.0.1", "167772161", "0x0a000001"):
        uc = egress.check_url(f"https://{host}/x")
        assert code(egress.resolve_pinned, uc,
                    resolver=resolver_returning("10.0.0.1")) \
            == "ip_blocked"


# ---------- pinning: TOCTOU + rebinding ----------

def test_dialed_target_is_the_pinned_ip_not_the_name():
    calls = []
    pinned = egress.resolve_pinned(
        egress.check_url("https://cdn.example.com/x"),
        resolver=resolver_returning("93.184.216.34"))
    egress.open_pinned(pinned, 443,
                       connect=lambda a, **k: calls.append(a) or "sock")
    assert calls == [("93.184.216.34", 443)]
    # the hostname string is never presented to connect
    assert "cdn.example.com" not in str(calls)


def test_rebinding_cannot_widen_the_pin():
    # DNS flips hostile AFTER pin — open_pinned still dials the first
    # validated answer; a transport never re-resolves.
    seq = iter([["93.184.216.34"], ["10.0.0.1"]])

    def flip(host, port):
        return resolver_returning(*next(seq))(host, port)

    pinned = egress.resolve_pinned(
        egress.check_url("https://cdn.example.com/x"), resolver=flip)
    dialed = []
    egress.open_pinned(pinned, 443,
                       connect=lambda a, **k: dialed.append(a))
    assert dialed == [("93.184.216.34", 443)]


def test_sni_is_original_host_not_ip():
    plan = egress.plan_fetch("https://cdn.example.com/x",
                             resolver=resolver_returning("93.184.216.34"))
    assert plan.sni_host == "cdn.example.com"
    assert plan.audit["sni"] == "cdn.example.com"
    assert plan.audit["dial"] == "93.184.216.34"
    assert plan.audit["resolved"] == ["93.184.216.34"]


# ---------- redirects ----------

def test_redirect_each_hop_full_revalidation():
    plan = egress.validate_redirect(
        "https://a.example.com/x", "https://b.example.com/y",
        resolver=resolver_returning("93.184.216.34"))
    assert plan.host == "b.example.com"


def test_redirect_to_private_refused():
    assert code(egress.validate_redirect,
                "https://a.example.com/x",
                "https://internal.example.com/i",
                resolver=resolver_returning("10.0.0.1")) == "ip_blocked"


def test_redirect_relative_join_then_validated():
    plan = egress.validate_redirect(
        "https://a.example.com/x", "/z",
        resolver=resolver_returning("93.184.216.34"))
    assert plan.host == "a.example.com"


def test_redirect_scheme_change_refused():
    assert code(egress.validate_redirect,
                "https://a.example.com/x", "http://a.example.com/x") \
        == "scheme"


def test_default_plan_refuses_redirects():
    plan = egress.plan_fetch("https://cdn.example.com/x",
                             resolver=resolver_returning("93.184.216.34"))
    assert plan.follow_redirects is False


# ---------- byte cap ----------

def test_stream_cap_enforced():
    feed = iter([b"x" * 100, b"y" * 100])
    assert code(egress.enforce_stream_cap,
                lambda n: next(feed, b""), 150) == "size"


def test_stream_cap_exact_ok():
    feed = iter([b"x" * 64, b""])
    assert egress.enforce_stream_cap(lambda n: next(feed, b""), 64) \
        == b"x" * 64


# ---------- audit shape ----------

def test_plan_carries_audit_evidence():
    plan = egress.plan_fetch("https://cdn.example.com/x",
                             resolver=resolver_returning(
                                 "93.184.216.34", "93.184.216.35"))
    assert plan.audit["dial"] == "93.184.216.34"
    assert set(plan.audit["resolved"]) == \
        {"93.184.216.34", "93.184.216.35"}


def test_refusal_codes_stable():
    for fn, code_ in (
            (lambda: egress.check_url("http://x"), "scheme"),
            (lambda: egress.check_url("https://u@x/"), "userinfo")):
        try:
            fn()
        except egress.EgressRefused as e:
            assert e.code == code_


# ---------- fetch_artifact wiring (issue #96 hard prerequisite) ----------

class _FakeLiveTransport:
    """Marked live wrapper stand-in: is_live_transport + stream_pinned.
    Records whether it received the validated FetchPlan or a raw URL."""
    is_live_transport = True

    def __init__(self):
        self.plans = []
        self.raw_urls = []

    def handle_async_request(self, request):
        # If httpx ever routes the raw URL through, it lands here.
        self.raw_urls.append(str(request.url))
        import httpx
        return httpx.Response(200, content=b"x",
                              request=request)

    def stream_pinned(self, client, plan):
        self.plans.append(plan)
        import contextlib

        @contextlib.asynccontextmanager
        async def _cm():
            class _Resp:
                status_code = 200
                headers = {"content-length": "3"}

                async def aiter_bytes(self, n):
                    yield b"art"

            yield _Resp()
        return _cm()


def _image_provider(transport, **kw):
    from app.domain.workers.rest_image_providers import (
        make_image_provider)
    return make_image_provider("ideogram", transport=transport, **kw)


@pytest.mark.asyncio
async def test_live_transport_requires_resolver_at_construction():
    from app.domain.workers.rest_providers import EgressRefused
    with pytest.raises(EgressRefused):
        _image_provider(_FakeLiveTransport())


@pytest.mark.asyncio
async def test_live_transport_gets_plan_never_raw_url():
    t = _FakeLiveTransport()
    p = _image_provider(t, artifact_resolver=resolver_returning(
        "93.184.216.34"))
    data = await p.fetch_artifact("https://cdn.ideogram.ai/x.png")
    assert data == b"art"
    assert len(t.plans) == 1
    plan = t.plans[0]
    assert plan.sni_host == "cdn.ideogram.ai"
    assert str(plan.pinned.ips[0]) == "93.184.216.34"
    assert t.raw_urls == []


@pytest.mark.asyncio
async def test_live_transport_hostile_resolution_refused_before_transport():
    t = _FakeLiveTransport()
    p = _image_provider(t, artifact_resolver=resolver_returning(
        "169.254.169.254"))
    with pytest.raises(egress.EgressRefused) as e:
        await p.fetch_artifact("https://cdn.ideogram.ai/x.png")
    assert e.value.code == "ip_metadata"
    assert t.plans == [] and t.raw_urls == []


@pytest.mark.asyncio
async def test_mock_transport_with_resolver_exercises_gate():
    import httpx
    from app.domain.workers.rest_providers import RemoteError
    called = []
    mt = httpx.MockTransport(
        lambda r: called.append(r) or httpx.Response(
            200, content=b"art", request=r))
    p = _image_provider(mt, artifact_resolver=resolver_returning(
        "93.184.216.34"))
    data = await p.fetch_artifact("https://cdn.ideogram.ai/x.png")
    assert data == b"art"
    assert len(called) == 1  # mock stream still used — no pin needed
    # hostile resolution refuses before the mock is ever invoked
    called.clear()
    p2 = _image_provider(mt, artifact_resolver=resolver_returning(
        "10.0.0.1"))
    with pytest.raises(egress.EgressRefused):
        await p2.fetch_artifact("https://cdn.ideogram.ai/x.png")
    assert called == []


@pytest.mark.asyncio
async def test_mock_transport_without_resolver_keeps_simulated_path():
    import httpx
    called = []
    mt = httpx.MockTransport(
        lambda r: called.append(r) or httpx.Response(
            200, content=b"art", request=r))
    p = _image_provider(mt)  # no resolver — no DNS in simulation
    data = await p.fetch_artifact("https://cdn.ideogram.ai/x.png")
    assert data == b"art"
    assert len(called) == 1
