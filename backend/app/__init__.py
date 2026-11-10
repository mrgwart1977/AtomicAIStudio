"""Atomic Dream Engine backend service.

Local FastAPI sidecar started and stopped by the Tauri desktop shell.
Binds to loopback only; every API call (except /health) requires the
per-launch session token handed to the desktop app at startup.
"""

__version__ = "0.1.0"
