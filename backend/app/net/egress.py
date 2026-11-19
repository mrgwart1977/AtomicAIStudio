"""Issue #96 — resolved-IP egress gate for artifact retrieval.

A live artifact fetch may only dial addresses this module has resolved
AND validated. Nothing here is reachable from the current product: every
transport remains injected/simulated — this is the transport-layer
contract a future live fetcher MUST satisfy (same posture as
``live_gate``), exercised in tests against fake resolvers/connectors.

Design contract:

1. ``check_url`` — URL hygiene: https only, no user-info, no
   non-default port, host present (IP-literal hosts are validated as
   IPs, never treated as names).
2. ``resolve_pinned`` — resolve ONCE, validate EVERY returned address
   against the private/reserved blocklist (v4+v6, mapped/embedded
   forms). A hostname with even one hostile address is refused outright.
3. ``open_pinned`` — the only dialing primitive: connects to a *pinned
   literal IP* from step 2. The hostname is never re-resolved and never
   dialed — DNS rebinding and TOCTOU cannot widen the pin. ``sni_host``
   is carried separately so TLS still authenticates the ORIGINAL host.
4. ``validate_redirect`` — a redirect target re-runs the entire gate
   (URL + resolve + IP); a 3xx can never widen policy. Default plan
   policy still refuses all redirects — per-hop validation exists for a
   future transport that chooses to follow them.
5. ``plan_fetch`` — the complete preflight a transport must hold:
   validated host, pinned IPs, SNI, byte cap, timeouts, and an audit
   record (resolved set + pin actually dialed).

Blocked ranges — explicit CIDRs rather than version-dependent
``is_private`` semantics: v4 loopback/RFC-1918/link-local (incl. cloud
metadata 169.254.169.254)/CGNAT/multicast/unspecified/broadcast/
documentation/reserved; v6 loopback/ULA/link-local/multicast/
unspecified/v4-mapped/NAT64-embedded (embedded v4 re-checked against
the v4 rules)/documentation.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

# ------------------------------------------------------------------
# refusal
# ------------------------------------------------------------------

class EgressRefused(Exception):
    """Refusal carrying a stable machine-readable code for audit/UI."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------
# blocked address space
# ------------------------------------------------------------------

_V4_BLOCKED = tuple(ipaddress.IPv4Network(n) for n in (
    "0.0.0.0/8",          # "this network" / unspecified source
    "10.0.0.0/8",         # RFC-1918
    "100.64.0.0/10",      # CGNAT (not covered by ipaddress.is_private)
    "127.0.0.0/8",        # loopback
    "169.254.0.0/16",     # link-local — covers cloud metadata 169.254.169.254
    "172.16.0.0/12",      # RFC-1918
    "192.0.0.0/24",       # IETF protocol assignments
    "192.0.2.0/24",       # TEST-NET-1 (documentation)
    "192.88.99.0/24",     # 6to4 relay anycast
    "192.168.0.0/16",     # RFC-1918
    "198.18.0.0/15",      # benchmark testing
    "198.51.100.0/24",    # TEST-NET-2
    "203.0.113.0/24",     # TEST-NET-3
    "224.0.0.0/4",        # multicast
    "240.0.0.0/4",        # reserved incl. 255.255.255.255 broadcast
))

_V6_BLOCKED = tuple(ipaddress.IPv6Network(n) for n in (
    "::/128",             # unspecified
    "::1/128",            # loopback
    "::ffff:0:0/96",      # v4-mapped — handled by unmapping below
    "64:ff9b::/96",       # NAT64 — embedded v4 re-checked below
    "64:ff9b:1::/48",     # NAT64 local-use
    "100::/64",           # discard-only
    "2001::/23",          # special-purpose (IETF) — incl. Teredo 2001::/32
    "2001:db8::/32",      # documentation
    "2002::/16",          # 6to4
    "fc00::/7",           # unique-local
    "fe80::/10",          # link-local — covers cloud metadata endpoints
    "fec0::/10",          # deprecated site-local
    "ff00::/8",           # multicast
))

# Well-known metadata endpoints — inside link-local but named here so
# audit reasons can state *why* the address is hostile, not just which
# supernet matched.
_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """Raise EgressRefused if `ip` is in any blocked range.

    v4-mapped (::ffff:a.b.c.d) and NAT64 (64:ff9b::/96) embedded
    addresses are unwrapped and re-checked against the v4 rules —
    refusing the IPv6 wrapper while allowing the embedded private v4
    would defeat the whole gate.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            _check_ip_v4(mapped, repr=str(ip))
            return
        nat64 = _nat64_embedded(ip)
        if nat64 is not None:
            _check_ip_v4(nat64, repr=str(ip))
            return
        for net in _V6_BLOCKED:
            if ip in net:
                raise EgressRefused("ip_blocked",
                                    f"resolved address {ip} is in blocked "
                                    f"range {net}")
        return
    _check_ip_v4(ip)


def _check_ip_v4(ip: ipaddress.IPv4Address, *, repr: str | None = None) -> None:
    shown = repr or str(ip)
    if str(ip) in _METADATA_IPS:
        raise EgressRefused("ip_metadata",
                            f"resolved address {shown} is a cloud "
                            "metadata endpoint")
    for net in _V4_BLOCKED:
        if ip in net:
            raise EgressRefused("ip_blocked",
                                f"resolved address {shown} is in blocked "
                                f"range {net}")


def _nat64_embedded(ip: ipaddress.IPv6Address
                    ) -> ipaddress.IPv4Address | None:
    """IPv4 embedded in the well-known NAT64 prefix 64:ff9b::/96."""
    pref = ipaddress.IPv6Network("64:ff9b::/96")
    if ip in pref:
        b = ip.packed
        return ipaddress.IPv4Address(b[12:16])
    return None


def check_ip_literal(value: str) -> ipaddress.IPv4Address | \
        ipaddress.IPv6Address | None:
    """Parse `value` as an IP literal, or None if it is a hostname.

    Handles the bracketed-IPv6 form urlparse strips to ``::1`` and the
    decimal/octal/hex obfuscations resolvers accept — anything not a
    straight IP is left for DNS resolution, where the resolved address
    is still validated.
    """
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


# ------------------------------------------------------------------
# URL hygiene
# ------------------------------------------------------------------

@dataclass(frozen=True)
class URLCheck:
    scheme: str
    host: str              # normalized hostname (or IP literal)
    port: int
    host_is_ip: bool
    ip_literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None


def check_url(url: str, *, allowed_schemes: frozenset[str] = frozenset(
        {"https"}), allowed_ports: frozenset[int] = frozenset({443})
              ) -> URLCheck:
    """Validate URL shape. Refuses non-https, user-info, off-port, and
    empty hosts. IP-literal hosts are flagged so the caller validates
    them as addresses, not names."""
    try:
        u = urlparse(url)
    except ValueError as e:
        raise EgressRefused("url_parse", str(e))
    if u.scheme.lower() not in allowed_schemes:
        raise EgressRefused("scheme", f"scheme {u.scheme!r} not allowed")
    if u.username is not None or u.password is not None:
        raise EgressRefused("userinfo",
                            "URL carries user-info — refused")
    host = (u.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise EgressRefused("host", "URL has no host")
    try:
        port = u.port
    except ValueError:
        raise EgressRefused("port", "unparseable port")
    if port is not None and port not in allowed_ports:
        raise EgressRefused("port", f"non-default port {port} refused")
    ip = check_ip_literal(host)
    if ip is not None:
        _check_ip(ip)  # literal hosts are validated immediately
    return URLCheck(scheme=u.scheme.lower(), host=host,
                    port=port or next(iter(allowed_ports)),
                    host_is_ip=ip is not None, ip_literal=ip)


# ------------------------------------------------------------------
# resolve + pin
# ------------------------------------------------------------------

Resolver = Callable[[str, int], list[tuple]]
Connector = Callable[..., Any]


def _default_resolver(host: str, port: int) -> list[tuple]:
    return socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


@dataclass(frozen=True)
class PinnedHost:
    """A validated host plus the COMPLETE allowed address set. The
    transport may dial only these literal IPs — never the name again."""
    host: str
    ips: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
    resolved_at: float = field(default_factory=time.time)


def resolve_pinned(url_check: URLCheck, *,
                   resolver: Resolver = _default_resolver) -> PinnedHost:
    """Resolve once; refuse if ANY resolved address is blocked.

    An allowlisted name that also resolves to a hostile address is
    hostile — partial validation would let DNS order pick the bad one.
    """
    if url_check.host_is_ip:
        return PinnedHost(host=url_check.host,
                          ips=(url_check.ip_literal,))
    try:
        infos = resolver(url_check.host, url_check.port)
    except (OSError, socket.gaierror) as e:
        raise EgressRefused("resolve",
                            f"{url_check.host} did not resolve: {e}")
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (IndexError, ValueError):
            continue
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise EgressRefused("resolve",
                            f"{url_check.host} resolved to no addresses")
    for ip in ips:
        _check_ip(ip)
    return PinnedHost(host=url_check.host, ips=tuple(ips))


# ------------------------------------------------------------------
# the only dialing primitive
# ------------------------------------------------------------------

def open_pinned(pinned: PinnedHost, port: int, *,
                connect: Connector | None = None,
                timeout_s: float = 10.0) -> Any:
    """Dial a PINNED literal IP — never the hostname.

    TOCTOU/rebinding defense lives here: the transport never re-resolves,
    so a hostile answer delivered after validation cannot be dialed. The
    returned socket's peer is exactly a validated address; TLS layers on
    top still verify ``sni_host`` (the original name).
    """
    connect = connect or socket.create_connection
    ip = str(pinned.ips[0])
    return connect((ip, port), timeout=timeout_s)


# ------------------------------------------------------------------
# redirect + fetch plan
# ------------------------------------------------------------------

def validate_redirect(current_url: str, location: str, *,
                      resolver: Resolver = _default_resolver
                      ) -> "FetchPlan":
    """A redirect hop re-runs the ENTIRE gate: relative locations are
    joined, then URL hygiene + resolve + IP validation all apply. A 3xx
    can never widen policy."""
    target = urljoin(current_url, location)
    return plan_fetch(target, resolver=resolver)


@dataclass(frozen=True)
class FetchPlan:
    """What a live transport must hold before any byte moves. Records
    the full resolved set plus the pin it will dial — the audit trail."""
    url: str
    host: str
    port: int
    sni_host: str
    pinned: PinnedHost
    max_bytes: int
    connect_timeout_s: float
    read_timeout_s: float
    follow_redirects: bool
    audit: dict


def plan_fetch(url: str, *, resolver: Resolver = _default_resolver,
               max_bytes: int = 16 * 1024 * 1024,
               connect_timeout_s: float = 10.0,
               read_timeout_s: float = 60.0,
               follow_redirects: bool = False) -> FetchPlan:
    """Complete preflight: URL hygiene → resolve → IP validation →
    pinned plan. Raises EgressRefused with a stable code on any failure."""
    uc = check_url(url)
    pinned = resolve_pinned(uc, resolver=resolver)
    return FetchPlan(
        url=url, host=uc.host, port=uc.port, sni_host=uc.host,
        pinned=pinned, max_bytes=max_bytes,
        connect_timeout_s=connect_timeout_s,
        read_timeout_s=read_timeout_s,
        follow_redirects=follow_redirects,
        audit={"resolved": [str(ip) for ip in pinned.ips],
               "dial": str(pinned.ips[0]),
               "sni": uc.host})


def enforce_stream_cap(read_fn: Callable[[int], bytes],
                       max_bytes: int) -> bytes:
    """Bounded read loop for a transport: accumulates until EOF or
    `max_bytes`; exceeding the cap refuses — never returns oversized
    content. `read_fn(n) -> bytes` yields chunks, b'' at EOF."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = read_fn(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise EgressRefused("size",
                                f"body exceeds {max_bytes}-byte cap")
        chunks.append(chunk)
    return b"".join(chunks)
