"""Issue #175 — pinned, allowlisted model-download transport.

Fully offline and deterministic: DNS resolvers are faked
(getaddrinfo-shaped), connections are scripted _Conn stubs — no socket
ever opens, no TLS handshake runs, no real network is touched."""
import asyncio
import hashlib
import json
import socket
import time
import urllib.request
from pathlib import Path

import pytest

from app.domain import download_security as ds
from app.domain import model_files, model_catalog
from app.net import egress
from app.net.egress import EgressRefused


# ------------------------------------------------------------- fakes

PUBLIC = "93.184.216.34"  # example.com — public
PUBLIC2 = "151.101.1.140"  # second public addr


def _resolver(ips=None):
    table = dict(ips or {})

    def res(host, port):
        ip = table.get(host, PUBLIC)
        return [(2, 1, 6, "", (ip, 443))]
    return res


def _plan(url, ips=None):
    return egress.plan_fetch(url, resolver=_resolver(ips))


class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self._b = body
        self._h = {k.lower(): v for k, v in (headers or {}).items()}
        self._pos = 0

    def getheader(self, name, default=None):
        return self._h.get(name.lower(), default)

    def read(self, n=-1):
        if n is None or n < 0:
            out = self._b[self._pos:]
            self._pos = len(self._b)
            return out
        out = self._b[self._pos:self._pos + n]
        self._pos += len(out)
        return out


class _Conn:
    """Scripted connection — records the FetchPlan it was opened with so
    tests can assert which host/pinned IP was dialed."""
    def __init__(self, plan, script):
        self.plan = plan
        self._script = script
        self.requests = []
        self.closed = False

    def request(self, method, path, headers=None):
        self.requests.append(
            {"method": method, "path": path, "headers": headers or {}})

    def getresponse(self):
        return self._script(self)

    def close(self):
        self.closed = True


def _factory(script, conns=None):
    def factory(plan):
        c = _Conn(plan, script)
        if conns is not None:
            conns.append(c)
        return c
    return factory


def _by_host(resps):
    """Script: answer from a per-host response map (lists consumed in order)."""
    def script(conn):
        r = resps[conn.plan.host]
        return r.pop(0) if isinstance(r, list) else r
    return script


def _dest(tmp_path, name="model.safetensors"):
    d = tmp_path / "models" / "m.key"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


HF = ds.HOST_ALLOWLISTS["huggingface"]
GH = ds.HOST_ALLOWLISTS["github"]


# ------------------------------------------------------------- URL gate

@pytest.mark.parametrize("url", [
    "http://huggingface.co/x.safetensors",
    "file:///etc/passwd",
    "ftp://huggingface.co/x",
])
def test_scheme_refused_before_dial(url):
    with pytest.raises(EgressRefused, match="scheme"):
        ds.open_download_stream(
            url, resume_from=0, allowed_hosts=HF,
            resolver=_resolver(), conn_factory=_factory(_by_host({})))


def test_ip_literal_private_refused():
    for url in ("https://10.0.0.5/x", "https://[::1]/x",
                "https://[::ffff:10.0.0.1]/x",
                "https://[64:ff9b::a00:1]/x",
                "https://169.254.169.254/latest"):
        with pytest.raises(EgressRefused):
            _plan(url)


def test_private_resolution_refused():
    for bad in ("10.0.0.1", "127.0.0.1", "169.254.169.254",
                "192.168.1.1", "100.64.0.1"):
        with pytest.raises(EgressRefused, match="ip_"):
            egress.plan_fetch("https://huggingface.co/x",
                              resolver=_resolver({"huggingface.co": bad}))


def test_dns_rebinding_any_bad_refused():
    """A host resolving to [public, private] is refused outright —
    partial validation would let DNS order pick the hostile answer."""
    def res(host, port):
        return [(2, 1, 6, "", (PUBLIC, 443)), (2, 1, 6, "", ("127.0.0.1", 443))]
    with pytest.raises(EgressRefused, match="ip_"):
        egress.plan_fetch("https://huggingface.co/x", resolver=res)


def test_allowlist_suffix_boundary():
    assert ds.host_allowed("huggingface.co", HF)
    assert ds.host_allowed("cdn-lfs.hf.co", HF)
    assert ds.host_allowed("cas-bridge.xethub.hf.co", HF)
    assert ds.host_allowed("objects.githubusercontent.com", GH)
    for bad in ("evilstuffhuggingface.co", "huggingface.co.evil.com",
                "hf.co.evil.com", "evilhf.co", "hugface.co"):
        assert not ds.host_allowed(bad, HF)


def test_allowlist_refuses_unlisted_host():
    with pytest.raises(EgressRefused, match="host_allowlist"):
        ds.open_download_stream(
            "https://evil.example.com/x", resume_from=0, allowed_hosts=HF,
            resolver=_resolver(),
            conn_factory=_factory(_by_host({"evil.example.com": _Resp(200)})))


def test_allowlist_empty_refuses():
    with pytest.raises(EgressRefused, match="host_allowlist"):
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=0, allowed_hosts=(),
            resolver=_resolver(), conn_factory=_factory(_by_host({})))


def test_idna_canonicalization():
    """Full-width dots and unicode forms canonicalize to the ASCII host
    the allowlist sees."""
    uc = egress.check_url("https://huggingface。co/a.safetensors")
    assert uc.host == "huggingface.co"
    uc2 = egress.check_url("https://xn--hggngna-4wab.co/x")
    assert uc2.host == "xn--hggngna-4wab.co"


def test_userinfo_and_port_refused():
    with pytest.raises(EgressRefused, match="userinfo"):
        egress.check_url("https://user:pw@huggingface.co/x")
    with pytest.raises(EgressRefused, match="port"):
        egress.check_url("https://huggingface.co:8443/x")


# ------------------------------------------------------------- pinning

def test_pinned_connection_dials_ip_only():
    """connect() must dial a validated literal IP — never the hostname —
    while TLS SNI keeps the original host."""
    plan = _plan("https://huggingface.co/x")
    dialed, sni = [], []

    class _Sock:
        def settimeout(self, t):
            pass

    def fake_connect(addr, timeout=None):
        dialed.append(addr)
        return _Sock()

    def fake_wrap(sock, server_hostname=None):
        sni.append(server_hostname)
        return sock

    conn = ds.PinnedHTTPSConnection(plan, connect=fake_connect,
                                    wrap=fake_wrap)
    conn.connect()
    assert dialed == [(str(plan.pinned.ips[0]), 443)]
    assert sni == ["huggingface.co"]


def test_pinned_connection_failover_within_set():
    """A failed pin falls through to the next VALIDATED ip — never
    re-resolves."""
    def res(host, port):
        return [(2, 1, 6, "", (PUBLIC, 443)), (2, 1, 6, "", (PUBLIC2, 443))]
    plan = egress.plan_fetch("https://huggingface.co/x", resolver=res)
    dialed = []

    def flaky(addr, timeout=None):
        dialed.append(addr[0])
        if addr[0] == PUBLIC:
            raise OSError("refused")
        return object()

    conn = ds.PinnedHTTPSConnection(
        plan, connect=flaky, wrap=lambda s, server_hostname=None: s)
    conn.connect()
    assert dialed == [PUBLIC, PUBLIC2]


def test_stream_never_re_resolves():
    """Only ONE resolution per hop: dial goes to the pinned ip even if a
    second resolve would return something else (rebinding defense)."""
    calls = []

    def res(host, port):
        calls.append(host)
        return [(2, 1, 6, "", (PUBLIC, 443))]
    conns = []
    ds.open_download_stream(
        "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
        resolver=res,
        conn_factory=_factory(_by_host({"huggingface.co": _Resp(200, b"ab")}),
                              conns))
    assert calls == ["huggingface.co"]            # resolved once
    assert conns[0].plan.pinned.ips[0] is not None
    assert [str(i) for i in conns[0].plan.pinned.ips] == [PUBLIC]


# ------------------------------------------------------------- proxy

def test_applicable_proxy_refused(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"https": "http://corp-proxy:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda h: False)
    with pytest.raises(EgressRefused, match="proxy_unsupported") as e:
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
            resolver=_resolver(), conn_factory=_factory(_by_host({})))
    assert "corp-proxy" not in str(e.value)   # never echo proxy details


def test_bypassed_proxy_does_not_block(monkeypatch):
    """A proxy that does not apply to this host is not a refusal —
    pinned transport never uses proxies anyway."""
    monkeypatch.setattr(urllib.request, "getproxies",
                        lambda: {"https": "http://corp:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda h: True)
    conn, resp, _, _ = ds.open_download_stream(
        "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
        resolver=_resolver(),
        conn_factory=_factory(_by_host({"huggingface.co": _Resp(200, b"ok")})))
    assert resp.read() == b"ok"
    conn.close()


def test_no_proxy_env_fine(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    conn, resp, _, _ = ds.open_download_stream(
        "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
        resolver=_resolver(),
        conn_factory=_factory(_by_host({"huggingface.co": _Resp(200, b"x")})))
    conn.close()


# ------------------------------------------------------------- redirects

def test_hf_cdn_redirect_followed():
    """The legitimate hf.co → CDN hop: re-gated per hop, streamed from
    the pinned CDN connection."""
    conns = []
    resps = {
        "huggingface.co": _Resp(302, headers={
            "Location": "https://cdn-lfs.hf.co/repos/abc?token=SIGNED"}),
        "cdn-lfs.hf.co": _Resp(200, b"WEIGHTS"),
    }
    conn, resp, start, total = ds.open_download_stream(
        "https://huggingface.co/repo/resolve/rev/f.safetensors",
        resume_from=0, allowed_hosts=HF, resolver=_resolver(),
        conn_factory=_factory(_by_host(resps), conns))
    assert resp.read() == b"WEIGHTS"
    assert [c.plan.host for c in conns] == ["huggingface.co", "cdn-lfs.hf.co"]
    conn.close()


def test_xet_cas_redirect_followed():
    conns = []
    resps = {
        "huggingface.co": _Resp(302, headers={
            "Location": "https://cas-bridge.xethub.hf.co/x?sig=S"}),
        "cas-bridge.xethub.hf.co": _Resp(200, b"XET"),
    }
    conn, resp, _, _ = ds.open_download_stream(
        "https://huggingface.co/r/resolve/v/f.safetensors",
        resume_from=0, allowed_hosts=HF, resolver=_resolver(),
        conn_factory=_factory(_by_host(resps), conns))
    assert resp.read() == b"XET"
    assert conns[-1].plan.host == "cas-bridge.xethub.hf.co"
    conn.close()


def test_github_release_redirect_followed():
    conns = []
    resps = {
        "github.com": _Resp(302, headers={
            "Location": "https://objects.githubusercontent.com/a/b.zip"}),
        "objects.githubusercontent.com": _Resp(200, b"ZIP"),
    }
    conn, resp, _, _ = ds.open_download_stream(
        "https://github.com/o/r/releases/download/t/a.zip",
        resume_from=0, allowed_hosts=GH, resolver=_resolver(),
        conn_factory=_factory(_by_host(resps), conns))
    assert resp.read() == b"ZIP"
    conn.close()


def test_cross_provider_redirect_refused():
    resps = {"huggingface.co": _Resp(302, headers={
        "Location": "https://github.com/o/r/x"})}
    with pytest.raises(EgressRefused, match="host_allowlist"):
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
            resolver=_resolver(), conn_factory=_factory(_by_host(resps)))


def test_redirect_to_disallowed_scheme_refused():
    for loc in ("http://cdn-lfs.hf.co/x", "file:///etc/passwd",
                "ftp://cdn-lfs.hf.co/x"):
        resps = {"huggingface.co": _Resp(302, headers={"Location": loc})}
        with pytest.raises(EgressRefused):
            ds.open_download_stream(
                "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
                resolver=_resolver(),
                conn_factory=_factory(_by_host(resps)))


def test_redirect_to_private_ip_refused():
    resps = {"huggingface.co": _Resp(302, headers={
        "Location": "https://internal.local/x"})}
    resolver = _resolver({"internal.local": "192.168.0.10"})
    with pytest.raises(EgressRefused, match="ip_"):
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
            resolver=resolver, conn_factory=_factory(_by_host(resps)))


def test_redirect_hop_cap():
    resps = {"huggingface.co": _Resp(302, headers={
        "Location": "https://huggingface.co/next"})}
    with pytest.raises(EgressRefused, match="redirect_limit"):
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
            resolver=_resolver(),
            conn_factory=_factory(_by_host(resps)))


def test_redirect_without_location_refused():
    resps = {"huggingface.co": _Resp(302)}
    with pytest.raises(EgressRefused, match="redirect"):
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=0, allowed_hosts=HF,
            resolver=_resolver(), conn_factory=_factory(_by_host(resps)))


def test_redirect_target_url_never_persisted(tmp_path):
    """Signed CDN query params are transient: the sidecar stores only
    the ENTRY url."""
    resps = {
        "huggingface.co": _Resp(302, headers={
            "Location": "https://cdn-lfs.hf.co/x?token=SECRET123"}),
        "cdn-lfs.hf.co": _Resp(200, b"DATA"),
    }
    body = b"DATA"
    dest = _dest(tmp_path)
    r = model_files.download_to(
        dest, "https://huggingface.co/x", expected_size=len(body),
        allowed_hosts=HF, resolver=_resolver(),
        conn_factory=_factory(_by_host(resps)))
    assert r["sha256"] == hashlib.sha256(body).hexdigest()
    for p in dest.parent.glob("*.json"):
        assert "SECRET123" not in p.read_text()


# ------------------------------------------------------------- range

def test_206_range_validated():
    cr = {"Content-Range": "bytes 10-19/100", "Content-Length": "10"}
    conn, resp, start, total = ds.open_download_stream(
        "https://huggingface.co/x", resume_from=10, allowed_hosts=HF,
        expected_total=100, resolver=_resolver(),
        conn_factory=_factory(_by_host(
            {"huggingface.co": _Resp(206, b"0123456789", cr)})))
    assert (start, total) == (10, 100)
    conn.close()


def test_206_start_mismatch_refused():
    cr = {"Content-Range": "bytes 0-19/100"}
    with pytest.raises(EgressRefused, match="range_mismatch"):
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=10, allowed_hosts=HF,
            resolver=_resolver(),
            conn_factory=_factory(_by_host(
                {"huggingface.co": _Resp(206, b"", cr)})))


def test_206_missing_or_malformed_range_refused():
    for headers in ({}, {"Content-Range": "garbage"},
                    {"Content-Range": "bytes 50-10/100"},
                    {"Content-Range": "bytes 10-100/100"}):
        with pytest.raises(EgressRefused, match="range_mismatch"):
            ds.open_download_stream(
                "https://huggingface.co/x", resume_from=10,
                allowed_hosts=HF, resolver=_resolver(),
                conn_factory=_factory(_by_host(
                    {"huggingface.co": _Resp(206, b"", headers)})))


def test_206_total_vs_expected_refused():
    cr = {"Content-Range": "bytes 10-19/200"}
    with pytest.raises(EgressRefused, match="size_mismatch"):
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=10, allowed_hosts=HF,
            expected_total=100, resolver=_resolver(),
            conn_factory=_factory(_by_host(
                {"huggingface.co": _Resp(206, b"", cr)})))


def test_206_content_length_consistency():
    cr = {"Content-Range": "bytes 10-19/100", "Content-Length": "5"}
    with pytest.raises(EgressRefused, match="range_mismatch"):
        ds.open_download_stream(
            "https://huggingface.co/x", resume_from=10, allowed_hosts=HF,
            expected_total=100, resolver=_resolver(),
            conn_factory=_factory(_by_host(
                {"huggingface.co": _Resp(206, b"", cr)})))


def test_416_resets_and_recovers(tmp_path, monkeypatch):
    """416 → partial state discarded, clean restart on retry."""
    monkeypatch.setattr("time.sleep", lambda *a: None)
    body = b"fresh-body-01"
    script_calls = []

    def script(conn):
        script_calls.append(conn.requests[-1]["headers"].get("Range"))
        if len(script_calls) == 1:
            return _Resp(416)
        return _Resp(200, body)

    dest = _dest(tmp_path)
    dest.with_suffix(".safetensors.part").write_bytes(b"STALEDATA!!")
    dest.with_suffix(".safetensors.part.json").write_text(json.dumps(
        {"url": "https://huggingface.co/x", "expected_size": len(body)}))
    r = model_files.download_to(
        dest, "https://huggingface.co/x", expected_size=len(body),
        allowed_hosts=HF, resolver=_resolver(),
        conn_factory=_factory(script))
    assert r["sha256"] == hashlib.sha256(body).hexdigest()
    assert dest.read_bytes() == body
    assert script_calls[0] is not None      # first tried a Range resume
    assert script_calls[-1] is None         # restart asked for the whole file


# ---------------------------------------------------------- download_to

def test_resume_appends_via_206(tmp_path):
    body = b"0123456789" * 4
    dest = _dest(tmp_path)
    part = dest.with_suffix(".safetensors.part")
    part.write_bytes(body[:20])
    dest.with_suffix(".safetensors.part.json").write_text(json.dumps(
        {"url": "https://huggingface.co/x", "expected_size": len(body)}))
    seen = []

    def script(conn):
        seen.append(conn.requests[-1]["headers"].get("Range"))
        have = 20
        return _Resp(206, body[20:], {
            "Content-Range": f"bytes 20-{len(body) - 1}/{len(body)}",
            "Content-Length": str(len(body) - 20)})

    r = model_files.download_to(
        dest, "https://huggingface.co/x", expected_size=len(body),
        allowed_hosts=HF, resolver=_resolver(),
        conn_factory=_factory(script))
    assert seen == ["bytes=20-"]
    assert dest.read_bytes() == body
    assert r["sha256"] == hashlib.sha256(body).hexdigest()


def test_200_on_resume_restarts_clean(tmp_path):
    body = b"whole-fresh-body"
    dest = _dest(tmp_path)
    part = dest.with_suffix(".safetensors.part")
    part.write_bytes(b"STALE-STALE-STALE")
    dest.with_suffix(".safetensors.part.json").write_text(json.dumps(
        {"url": "https://huggingface.co/x", "expected_size": len(body)}))
    r = model_files.download_to(
        dest, "https://huggingface.co/x", expected_size=len(body),
        allowed_hosts=HF, resolver=_resolver(),
        conn_factory=_factory(_by_host(
            {"huggingface.co": _Resp(200, body)})))
    assert dest.read_bytes() == body            # truncated, not appended


def test_tampered_sidecar_url_resets(tmp_path):
    """A sidecar pointing at a different source can never seed a
    download — mismatched state is discarded, never fetched."""
    body = b"approved-bytes"
    dest = _dest(tmp_path)
    part = dest.with_suffix(".safetensors.part")
    part.write_bytes(b"EVIL")
    dest.with_suffix(".safetensors.part.json").write_text(json.dumps(
        {"url": "https://evil.example/x", "expected_size": len(body)}))
    conns = []
    r = model_files.download_to(
        dest, "https://huggingface.co/x", expected_size=len(body),
        allowed_hosts=HF + ("evil.example",),
        resolver=_resolver(),
        conn_factory=_factory(_by_host(
            {"huggingface.co": _Resp(200, body)}), conns))
    assert [c.plan.host for c in conns] == ["huggingface.co"]
    assert dest.read_bytes() == body            # evil bytes discarded


def test_tampered_sidecar_size_resets(tmp_path):
    body = b"0123456789"
    dest = _dest(tmp_path)
    dest.with_suffix(".safetensors.part").write_bytes(b"OLDSTALE")
    dest.with_suffix(".safetensors.part.json").write_text(json.dumps(
        {"url": "https://huggingface.co/x", "expected_size": 999}))
    r = model_files.download_to(
        dest, "https://huggingface.co/x", expected_size=len(body),
        allowed_hosts=HF, resolver=_resolver(),
        conn_factory=_factory(_by_host(
            {"huggingface.co": _Resp(200, body)})))
    assert dest.read_bytes() == body


def test_malformed_sidecar_resets(tmp_path):
    body = b"clean"
    dest = _dest(tmp_path)
    dest.with_suffix(".safetensors.part").write_bytes(b"xyz")
    dest.with_suffix(".safetensors.part.json").write_text("{not json")
    r = model_files.download_to(
        dest, "https://huggingface.co/x", expected_size=len(body),
        allowed_hosts=HF, resolver=_resolver(),
        conn_factory=_factory(_by_host(
            {"huggingface.co": _Resp(200, body)})))
    assert dest.read_bytes() == body


def test_exact_size_enforced(tmp_path):
    dest = _dest(tmp_path)
    with pytest.raises(model_files.ModelError, match="incomplete"):
        model_files.download_to(
            dest, "https://huggingface.co/x", expected_size=100,
            allowed_hosts=HF, resolver=_resolver(),
            conn_factory=_factory(_by_host(
                {"huggingface.co": _Resp(200, b"short")})))


def test_stream_byte_cap(tmp_path):
    """Transfer cap (expected + slack) is separate from the exact-size
    contract — an oversized stream is cut off, not tolerated."""
    dest = _dest(tmp_path)
    with pytest.raises(EgressRefused, match="size"):
        model_files.download_to(
            dest, "https://huggingface.co/x", expected_size=10,
            allowed_hosts=HF, resolver=_resolver(),
            conn_factory=_factory(_by_host(
                {"huggingface.co": _Resp(200, b"x" * 200 * 1024)})))


def test_global_cap_without_expected(tmp_path):
    dest = _dest(tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ds, "GLOBAL_SIZE_CAP", 64)
    try:
        with pytest.raises(EgressRefused, match="size"):
            model_files.download_to(
                dest, "https://huggingface.co/x", expected_size=None,
                allowed_hosts=HF, resolver=_resolver(),
                conn_factory=_factory(_by_host(
                    {"huggingface.co": _Resp(200, b"x" * 100)})))
    finally:
        monkeypatch.undo()


def test_deadline_refusal(tmp_path):
    dest = _dest(tmp_path)
    with pytest.raises(model_files.ModelError, match="deadline"):
        model_files.download_to(
            dest, "https://huggingface.co/x", expected_size=10,
            allowed_hosts=HF, resolver=_resolver(), deadline_s=-1,
            conn_factory=_factory(_by_host(
                {"huggingface.co": _Resp(200, b"0123456789")})))


def test_stall_refusal(tmp_path):
    dest = _dest(tmp_path)
    with pytest.raises(model_files.ModelError, match="stalled"):
        model_files.download_to(
            dest, "https://huggingface.co/x", expected_size=10,
            allowed_hosts=HF, resolver=_resolver(), stall_s=-1,
            conn_factory=_factory(_by_host(
                {"huggingface.co": _Resp(200, b"0123456789")})))


def test_cancel_midstream(tmp_path):
    class JobCancelled(Exception):
        pass
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        if calls["n"] > 1:
            raise JobCancelled()

    dest = _dest(tmp_path)
    with pytest.raises(JobCancelled):
        model_files.download_to(
            dest, "https://huggingface.co/x", expected_size=10,
            allowed_hosts=HF, resolver=_resolver(), cancel_check=cancel,
            conn_factory=_factory(_by_host(
                {"huggingface.co": _Resp(200, b"0123456789")})))
    assert dest.with_suffix(".safetensors.part").exists()  # kept, resumable


def test_retry_budget_and_sidecar_kept(tmp_path, monkeypatch):
    """A transient failure preserves .part + sidecar for resume."""
    monkeypatch.setattr("time.sleep", lambda *a: None)
    dest = _dest(tmp_path)

    def boom(conn):
        raise OSError("connection reset")

    with pytest.raises(OSError):
        model_files.download_to(
            dest, "https://huggingface.co/x", expected_size=10,
            allowed_hosts=HF, resolver=_resolver(),
            conn_factory=_factory(boom))
    sc = dest.with_suffix(".safetensors.part.json")
    assert sc.exists()
    assert json.loads(sc.read_text())["url"] == "https://huggingface.co/x"


def test_symlink_part_refused(tmp_path, monkeypatch):
    """A .part that is (or becomes) a symlink refuses before any open —
    guard is checked on every attempt. (OS symlink creation needs
    privileges on Windows, so is_symlink is simulated.)"""
    dest = _dest(tmp_path)
    part = dest.with_suffix(".safetensors.part")
    part.write_bytes(b"X")
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self):
        return True if self == part else real_is_symlink(self)
    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with pytest.raises(EgressRefused, match="symlink"):
        model_files.download_to(
            dest, "https://huggingface.co/x", expected_size=10,
            allowed_hosts=HF, resolver=_resolver(),
            conn_factory=_factory(_by_host({})))


def test_component_name_and_containment():
    for bad in ("../evil.safetensors", "a/b.safetensors", "a\\b.st", "..",
                ""):
        with pytest.raises(EgressRefused, match="path_traversal"):
            ds.ensure_component_name(bad)
    ds.ensure_component_name("ok.safetensors")
    root = Path("/tmp/models")
    ds.ensure_contained(root, root / "key" / "f.safetensors")
    with pytest.raises(EgressRefused, match="path_traversal"):
        ds.ensure_contained(root, root / ".." / "evil" / "f.st")


# --------------------------------------------------------- fetch_bytes

def test_fetch_bytes_bounded():
    conn_factory = _factory(_by_host(
        {"api.github.com": _Resp(200, b'{"a": 1}')}))
    out = ds.fetch_bytes(
        "https://api.github.com/r", allowed_hosts=GH,
        resolver=_resolver(), conn_factory=conn_factory)
    assert json.loads(out) == {"a": 1}


def test_fetch_bytes_cap():
    with pytest.raises(EgressRefused, match="size"):
        ds.fetch_bytes(
            "https://api.github.com/r", allowed_hosts=GH, max_bytes=4,
            resolver=_resolver(),
            conn_factory=_factory(_by_host(
                {"api.github.com": _Resp(200, b"1234567890")})))


def test_fetch_bytes_host_refused():
    with pytest.raises(EgressRefused, match="host_allowlist"):
        ds.fetch_bytes("https://evil.example/r", allowed_hosts=GH,
                       resolver=_resolver(), conn_factory=_factory(
                           _by_host({"evil.example": _Resp(200)})))


def test_release_asset_uses_pinned_gate(monkeypatch):
    """resolve_release_asset's default fetch goes through fetch_bytes —
    the API request itself is gated, not just the artifact."""
    calls = []

    def fake_fetch(url, *, allowed_hosts, max_bytes, timeout_s):
        calls.append((url, allowed_hosts))
        return json.dumps({
            "tag_name": "v1",
            "assets": [{"name": "sd-x-bin-win-vulkan-x64.zip",
                        "browser_download_url": "https://github.com/o/z.zip",
                        "size": 100}]}).encode()
    monkeypatch.setattr(ds, "fetch_bytes", fake_fetch)
    spec = model_catalog.get_runtime("sdcpp")
    asset = model_files.resolve_release_asset(spec, "vulkan")
    assert calls and calls[0][1] == ds.HOST_ALLOWLISTS["github"]
    assert asset["name"].endswith(".zip")


# ----------------------------------------------------- legacy source_url

def test_source_url_ignored_by_job(client, tmp_path, monkeypatch):
    """A legacy job request carrying source_url never changes the
    download source — the catalog URL is used and a warning logged."""
    seen = {}

    def fake_dl(dest, url, **kw):
        seen["url"] = url
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"FAKE")
        return {"bytes": 4, "sha256": "0" * 64}
    monkeypatch.setattr(model_files, "download_to", fake_dl)
    monkeypatch.setattr(model_files, "verify_component",
                        lambda p, f, fmt: (True, "ok"))

    from app.adapters.inference import ModelDownloadAdapter
    adapter = ModelDownloadAdapter(client.app.state.session_factory,
                                   client.app.state.settings)
    spec = model_catalog.get("sd15.fp16")

    class _Handle:
        request = {"model_key": "sd15.fp16", "skip_runtime": True,
                   "source_url": "https://evil.example/steal"}
        def check_cancel(self):
            pass

    async def drain():
        out = []
        async for ev in adapter.run(_Handle()):
            out.append(ev)
        return out
    asyncio.run(drain())
    assert seen["url"] == spec.files[0].url    # catalog URL, not the override


# ----------------------------------------------------- catalog contract

def test_catalog_entries_have_known_providers():
    """Every installable catalog file must map to a host allowlist —
    a new provider without one fails closed."""
    for m in model_catalog.catalog():
        if not m.files:
            continue
        provider = (m.source or {}).get("provider")
        assert ds.allowed_hosts_for(provider), \
            f"{m.key}: provider {provider!r} has no host allowlist"
