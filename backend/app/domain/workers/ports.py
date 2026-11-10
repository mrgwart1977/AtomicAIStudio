"""Loopback port allocation for worker-bound HTTP servers.

Port selection is a TOCTOU race by nature — the port is free when probed
and may be taken before the server binds. Adapters must therefore treat
bind failure as retriable (allocate → spawn → readiness-fail → retry a
*different* port), which is exactly what WorkerPool's spawn timeout path
surfaces.
"""

from __future__ import annotations

import socket


def free_tcp_port() -> int:
    """A currently-free loopback TCP port. Ephemeral by OS choice."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
