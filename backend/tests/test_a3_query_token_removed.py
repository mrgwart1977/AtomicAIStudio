"""A3 — query-token authentication removal.

Regression coverage proving ``?token=`` no longer authenticates
anything: REST is header-only, the WS requires the first-frame auth
protocol regardless of query parameters, and no client-side code
produces token-bearing URLs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

import app.security as sec
from tests.conftest import TOKEN

AUTH = json.dumps({"type": "auth", "token": TOKEN})
REPO = Path(__file__).resolve().parents[2]


class TestRestQueryTokenRemoved:
    def test_query_token_alone_rejected(self, client):
        """REST ``?token=`` no longer authenticates — header only."""
        client.headers.pop("x-atomic-token", None)   # only query creds
        r = client.get(f"/api/projects?token={TOKEN}")
        assert r.status_code == 401

    def test_header_auth_still_works(self, client):
        r = client.get("/api/projects")
        assert r.status_code == 200

    def test_no_credentials_rejected(self, client):
        client.headers.pop("x-atomic-token", None)
        r = client.get("/api/projects")
        assert r.status_code == 401

    def test_wrong_header_token_rejected(self, client):
        r = client.get("/api/projects",
                       headers={"x-atomic-token": "wrong"})
        assert r.status_code == 401

    def test_file_endpoint_query_token_rejected(self, client):
        """Asset/file URLs lose the query-token fallback too."""
        pid = None
        r = client.get("/api/projects")
        if r.status_code == 200 and r.json():
            pid = r.json()[0]["id"]
        client.headers.pop("x-atomic-token", None)
        if pid:
            r2 = client.get(f"/api/projects/{pid}?token={TOKEN}")
            assert r2.status_code == 401


class TestWsQueryTokenRemoved:
    def test_ws_query_token_alone_rejected(self, client, monkeypatch):
        """``/api/ws?token=...`` without an auth frame times out —
        the query credential is ignored."""
        monkeypatch.setattr(sec, "WS_AUTH_TIMEOUT_S", 0.2)
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(
                    f"/api/ws?token={TOKEN}") as ws:
                ws.receive_text()
        assert e.value.code == 1008

    def test_first_frame_still_works(self, client):
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(AUTH)
            assert ws.receive_json() == {"type": "auth_ok"}

    def test_ws_invalid_frame_token_rejected(self, client):
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect("/api/ws") as ws:
                ws.send_text(json.dumps(
                    {"type": "auth", "token": "wrong"}))
                ws.receive_text()
        assert e.value.code == 1008


class TestNoTokenExposure:
    def test_auth_error_reveals_no_token(self, client):
        """401 body carries no echoed credential material."""
        client.headers.pop("x-atomic-token", None)
        secret = "do-not-echo-me-456"
        r = client.get(f"/api/projects?token={secret}")
        assert r.status_code == 401
        assert secret not in r.text

    def test_no_client_query_token_producers(self):
        """Repo guard: no frontend source may embed a session token in a
        URL — the last producer (connectWs) migrated in A2.2."""
        src = REPO / "apps" / "desktop" / "src"
        offenders = []
        for f in src.rglob("*"):
            if f.suffix not in (".ts", ".tsx"):
                continue
            if f.name.endswith((".test.ts", ".test.tsx")):
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            for marker in ("?token=", "&token=", "`token=", "token=${"):
                if marker in text:
                    offenders.append(f"{f.relative_to(REPO)}:{marker}")
        assert offenders == [], \
            f"client-side token-in-URL producers found: {offenders}"

    def test_no_server_query_token_consumers(self):
        """Repo guard: app code must not read ?token= anywhere."""
        offenders = []
        for f in (REPO / "backend" / "app").rglob("*.py"):
            text = f.read_text(encoding="utf-8", errors="replace")
            if 'query_params.get("token")' in text \
                    or "query_params['token']" in text:
                offenders.append(str(f.relative_to(REPO)))
        assert offenders == [], \
            f"server-side ?token= consumers found: {offenders}"
