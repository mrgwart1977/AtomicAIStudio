"""Issue #175 — pinned, allowlisted transport for model artifact downloads.

Every byte that moves goes through the same gate chain:

    plan_fetch (URL hygiene + resolve + public-IP validation)
      → host allowlist check (provider-family suffix match on the
        IDNA-canonical host)
      → PinnedHTTPSConnection (dial the validated literal IP only —
        never the hostname again; TLS SNI + Host header still use the
        ORIGINAL host so certificate verification is unchanged)
      → manual redirect loop, <=5 hops, EVERY hop re-validated
      → bounded stream (byte cap, stall/deadline, cancel checks)

Design invariants:
- No hostname is ever re-resolved or dialed — DNS answers once per hop.
- Env proxies are NEVER consulted and NEVER used — fail-closed.
- Redirect-target URLs are transient state only. Signed CDN query
  params stay in memory: they are never written to the .part sidecar,
  logged, or embedded in error strings.
- Only the catalog-approved URL may seed a (re)download. A stored
  sidecar URL can never replace it.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import re
import socket
import ssl
import urllib.request
from urllib.parse import urljoin, urlsplit

from app.net import egress
from app.net.egress import EgressRefused, FetchPlan

log = logging.getLogger(__name__)


# ------------------------------------------------------------- allowlists

# Provider-family host suffixes (matched on the IDNA-canonical host with a
# dot boundary — "evilstuffhuggingface.co" does NOT match). Derived from
# spec.source["provider"]; a missing/unknown provider means no download.
HOST_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    # covers resolve/ origins, cdn-lfs* Fastly endpoints, and the Xet CAS
    # family (cas-bridge.xethub.hf.co etc.) — all under *.hf.co
    "huggingface": ("huggingface.co", "hf.co"),
    # api.github.com + release download endpoints
    # (objects/release-assets.github… )
    "github": ("github.com", "githubusercontent.com"),
}

MAX_REDIRECT_HOPS = 5
# Absolute transfer ceiling when the catalog declares no size — protects
# against unbounded streams (slow drip, endless chunked bodies).
GLOBAL_SIZE_CAP = 16 * 1024 ** 3  # 16 GiB
# Headroom above the declared size before the cap bites — a small slack for
# servers that report a slightly different Content-Length than the catalog.
SIZE_SLACK = 64 * 1024

_UA = {"User-Agent": "atomic-dream-engine"}


def allowed_hosts_for(provider: str | None) -> tuple[str, ...]:
    """Host suffixes a download may touch for a given source provider.
    Unknown/absent providers return () — an empty allowlist refuses."""
    return HOST_ALLOWLISTS.get(provider or "", ())


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """Exact-or-dot-boundary suffix match on a canonical host."""
    return any(host == s or host.endswith("." + s) for s in allowed_hosts)


def require_allowed_host(host: str, allowed_hosts: tuple[str, ...]) -> None:
    if not allowed_hosts:
        raise EgressRefused(
            "host_allowlist", "no download host allowlist configured")
    if not host_allowed(host, allowed_hosts):
        raise EgressRefused(
            "host_allowlist",
            f"{host} is outside the approved download hosts")


# ------------------------------------------------------------- proxy gate

def proxy_preflight(host: str) -> None:
    """Fail-closed proxy check — runs BEFORE any dial.

    The pinned transport never consults env proxies; an *applicable* proxy
    config means the user expects traffic routed through it, which pinned
    DNS cannot honor. Refuse clearly rather than silently bypass. A proxy
    entry that does not apply to this host (no_proxy coverage) is
    irrelevant and does not block."""
    proxies = urllib.request.getproxies()
    proxies = {k: v for k, v in proxies.items()
               if k not in ("no", "no_proxy") and v}
    if not proxies:
        return
    # urllib's bypass semantics (env no_proxy + registry bypass) decide
    # whether a proxy would actually serve this host.
    if urllib.request.proxy_bypass(host):
        return
    # Never echo proxy URLs in the error — they may embed credentials.
    raise EgressRefused(
        "proxy_unsupported",
        "an HTTP(S) proxy is configured for this host — the pinned model "
        "downloader cannot route through proxies in this build; unset the "
        "proxy variables or wait for corporate-proxy support")


# --------------------------------------------------------- pinned connect

class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that dials a *validated* literal IP from a
    FetchPlan, never the hostname. TLS SNI and the Host header keep the
    original hostname, so certificate + hostname verification are
    unchanged. `connect`/`wrap` are injectable for offline tests."""

    def __init__(self, plan: FetchPlan, *, timeout_s: float = 60.0,
                 connect=None, wrap=None):
        super().__init__(plan.host, port=plan.port,
                         timeout=timeout_s,
                         context=ssl.create_default_context())
        self._pinned_ips = [str(ip) for ip in plan.pinned.ips]
        self._sni_host = plan.sni_host
        self._connect = connect
        self._wrap = wrap

    def connect(self) -> None:
        # Dial pinned IPs in resolver order — failover within the validated
        # set only; never re-resolve (TOCTOU/rebinding defense).
        connect = self._connect or socket.create_connection
        last: OSError | None = None
        for ip in self._pinned_ips:
            try:
                self.sock = connect((ip, self.port),
                                    timeout=self.timeout)
                break
            except OSError as exc:
                last = exc
        if self.sock is None:
            raise last or OSError("no pinned address was connectable")
        wrap = self._wrap or self._context.wrap_socket
        self.sock = wrap(self.sock, server_hostname=self._sni_host)


# ------------------------------------------------------------ responses

_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


class _TransientHTTP(Exception):
    """Retryable HTTP condition (429/5xx, transport resets) — the outer
    retry budget applies; .part state survives."""
    def __init__(self, detail: str, retry_after: float | None = None):
        super().__init__(detail)
        self.retry_after = retry_after


class RangeUnsatisfiable(Exception):
    """HTTP 416 — the server refuses the resume offset. The caller resets
    partial state and restarts (the stored bytes cannot be trusted to be
    the artifact's prefix)."""


def _drain_close(resp, conn) -> None:
    try:
        resp.read()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _request_path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "") or "/"


def open_download_stream(url: str, *, resume_from: int,
                         allowed_hosts: tuple[str, ...],
                         expected_total: int | None = None,
                         timeout_s: float = 60.0,
                         resolver=None, connect=None, wrap=None,
                         conn_factory=None):
    """Open a validated GET stream for `url` following ≤ MAX_REDIRECT_HOPS
    manually re-gated redirects.

    Returns (conn, resp, resume_start, total):
      conn          — caller must close() after streaming
      resp          — http.client.HTTPResponse positioned at byte 0 of the
                      transfer (caller reads the body)
      resume_start  — offset the server agreed to stream from; 0 on a fresh
                      transfer OR when it ignored Range (caller restarts)
      total         — declared total length if known (progress only)

    Raises EgressRefused for policy violations, RangeUnsatisfiable for
    HTTP 416, _TransientHTTP for retryable statuses, ModelError-shaped
    ValueError for malformed responses."""
    plan_fn = egress.plan_fetch
    factory = conn_factory or (
        lambda plan: PinnedHTTPSConnection(
            plan, timeout_s=timeout_s, connect=connect, wrap=wrap))
    hops = 0
    while True:
        plan = plan_fn(url, resolver=resolver) \
            if resolver is not None else plan_fn(url)
        require_allowed_host(plan.host, allowed_hosts)
        proxy_preflight(plan.host)
        conn = factory(plan)
        headers = dict(_UA)
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
        conn.request("GET", _request_path(url), headers=headers)
        resp = conn.getresponse()
        status = resp.status

        if status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            _drain_close(resp, conn)
            hops += 1
            if hops > MAX_REDIRECT_HOPS:
                raise EgressRefused(
                    "redirect_limit",
                    f"redirect chain exceeded {MAX_REDIRECT_HOPS} hops")
            if not loc:
                raise EgressRefused("redirect", "3xx without a Location")
            # Per-hop revalidation happens at the top of the loop via
            # plan_fetch — validate_redirect joins + re-gates the target.
            url = egress.validate_redirect(
                url, loc, resolver=resolver
                or egress._default_resolver).url
            continue

        if status == 200:
            # On a resume request, 200 means the server ignored Range —
            # resume_start stays 0 so the caller restarts cleanly rather
            # than appending at a wrong offset.
            return conn, resp, 0, _content_length(resp)

        if status == 206:
            start, end, total = _parse_content_range(
                resp.getheader("Content-Range"))
            if start != resume_from:
                _drain_close(resp, conn)
                raise EgressRefused(
                    "range_mismatch",
                    f"206 Content-Range starts at {start}, resume asked "
                    f"for {resume_from}")
            if expected_total and total is not None \
                    and total != expected_total:
                _drain_close(resp, conn)
                raise EgressRefused(
                    "size_mismatch",
                    f"206 declares total {total}, catalog expects "
                    f"{expected_total}")
            cl = _content_length(resp)
            if cl is not None and cl != end - start + 1:
                _drain_close(resp, conn)
                raise EgressRefused(
                    "range_mismatch",
                    f"206 Content-Length {cl} does not match range "
                    f"{end - start + 1}")
            return conn, resp, start, total or cl

        if status == 416:
            _drain_close(resp, conn)
            raise RangeUnsatisfiable(
                "server refused the resume offset (416)")

        if status == 429 or status >= 500:
            retry_after = resp.getheader("Retry-After")
            _drain_close(resp, conn)
            wait = None
            if retry_after and retry_after.isdigit():
                wait = min(float(retry_after), 30.0)
            raise _TransientHTTP(f"HTTP {status}", retry_after=wait)

        _drain_close(resp, conn)
        raise EgressRefused("http_status",
                            f"unexpected HTTP {status}")


def _content_length(resp) -> int | None:
    v = resp.getheader("Content-Length")
    if v and v.isdigit():
        return int(v)
    return None


def _parse_content_range(value: str | None) -> tuple[int, int, int | None]:
    if not value:
        raise EgressRefused("range_mismatch", "206 missing Content-Range")
    m = _CONTENT_RANGE.match(value.strip())
    if not m:
        raise EgressRefused("range_mismatch",
                            "unparseable Content-Range")
    start, end = int(m.group(1)), int(m.group(2))
    total = int(m.group(3)) if m.group(3) != "*" else None
    if end < start:
        raise EgressRefused("range_mismatch",
                            "Content-Range end before start")
    if total is not None and end >= total:
        raise EgressRefused("range_mismatch",
                            "Content-Range end past total")
    return start, end, total


# ------------------------------------------------------------- small GET

def fetch_bytes(url: str, *, allowed_hosts: tuple[str, ...],
                max_bytes: int = 4 * 1024 * 1024,
                timeout_s: float = 30.0,
                resolver=None, connect=None, wrap=None,
                conn_factory=None) -> bytes:
    """Bounded, pinned GET used for metadata fetches (e.g. the GitHub
    release API) — the SAME gate chain as artifact downloads, just a small
    accumulator instead of a file stream."""
    conn, resp, _start, _total = open_download_stream(
        url, resume_from=0, allowed_hosts=allowed_hosts,
        timeout_s=timeout_s, resolver=resolver, connect=connect,
        wrap=wrap, conn_factory=conn_factory)
    try:
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise EgressRefused("size", "response exceeds metadata cap")
        return body
    finally:
        conn.close()


# -------------------------------------------------------------- fs safety

def ensure_component_name(name: str) -> None:
    """A declared artifact/component name must be a plain file name — no
    separators, no traversal. Anything else could escape the models root
    when joined into a destination path."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise EgressRefused(
            "path_traversal", f"unsafe artifact name {name!r}")


def ensure_contained(root, dest) -> None:
    """dest must resolve strictly inside root — protects .part writes when
    a declared name ever contained traversal (defense in depth)."""
    from pathlib import Path
    root_r = Path(root).resolve()
    dest_r = Path(dest).resolve()
    if dest_r != root_r and root_r not in dest_r.parents:
        raise EgressRefused(
            "path_traversal",
            f"destination {dest} escapes the models root")


def ensure_not_symlink(path) -> None:
    """Refuse to open a .part/.part.json/dest that is a symlink — writing
    through it would escape the models root."""
    from pathlib import Path
    if Path(path).is_symlink():
        raise EgressRefused("symlink",
                            f"{Path(path).name} is a symlink — refused")


def reconcile_sidecar(meta: dict | None, *, url: str,
                      expected_size: int | None) -> bool:
    """Decide whether stored sidecar state may be reused.

    The catalog-approved `url` is the ONLY source of truth — a sidecar
    whose stored url differs (tampered, stale, or from an older catalog
    revision) can never substitute a different source. Returns True when
    the stored state matches and the partial may resume; False means the
    caller must discard .part + sidecar and start clean."""
    if meta is None:
        return False
    if meta.get("url") != url:
        return False
    if expected_size is not None \
            and meta.get("expected_size") != expected_size:
        return False
    return True
