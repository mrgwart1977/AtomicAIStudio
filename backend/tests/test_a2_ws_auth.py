"""A2.1 — WebSocket first-frame authentication.

Covers the UNAUTHENTICATED → AUTHENTICATED → CLOSED state machine:
auth before subscription, strict timeout/size bounds, malformed and
duplicate frames, the bounded unauthenticated pool, origin policy,
legacy ?token= compatibility, disconnect cleanup, and the guarantee
that the session token never reaches logs.
"""
from __future__ import annotations

import json
import logging
import time

import pytest
from starlette.websockets import WebSocketDisconnect

import app.security as sec
from tests.conftest import TOKEN

AUTH = json.dumps({"type": "auth", "token": TOKEN})


def _connect(client, **kw):
    return client.websocket_connect("/api/ws", **kw)


def _publish(client, payload_id):
    """EventBus.publish is async — run it on the app loop via the
    TestClient portal."""
    client.portal.call(
        client.app.state.event_bus.publish,
        "job.updated", None, None, {"id": payload_id})


class TestFirstFrameAuth:
    def test_auth_ok_then_events_flow(self, client):
        with _connect(client) as ws:
            ws.send_text(AUTH)
            assert ws.receive_json() == {"type": "auth_ok"}
            _publish(client, "j1")
            evt = ws.receive_json()
            assert evt["event"] == "job.updated"
            assert evt["payload"]["id"] == "j1"

    def test_no_event_delivery_before_auth(self, client):
        """An unauthenticated socket is never subscribed: an event
        published while it holds a slot is never delivered — the first
        message it can receive is auth_ok."""
        with _connect(client) as ws:
            _publish(client, "leak-check")
            ws.send_text(AUTH)
            assert ws.receive_json() == {"type": "auth_ok"}
            _publish(client, "after")
            evt = ws.receive_json()
            assert evt["payload"]["id"] == "after"

    def test_wrong_token_refused(self, client):
        with pytest.raises(WebSocketDisconnect) as e:
            with _connect(client) as ws:
                ws.send_text(json.dumps(
                    {"type": "auth", "token": "wrong"}))
                ws.receive_text()
        assert e.value.code == 1008

    def test_missing_token_refused(self, client):
        with pytest.raises(WebSocketDisconnect) as e:
            with _connect(client) as ws:
                ws.send_text(json.dumps({"type": "auth", "token": ""}))
                ws.receive_text()
        assert e.value.code == 1002

    def test_auth_timeout(self, client, monkeypatch):
        monkeypatch.setattr(sec, "WS_AUTH_TIMEOUT_S", 0.2)
        with pytest.raises(WebSocketDisconnect) as e:
            with _connect(client) as ws:
                ws.receive_text()   # no frame sent — server times out
        assert e.value.code == 1008

    def test_oversized_auth_frame_refused(self, client):
        with pytest.raises(WebSocketDisconnect) as e:
            with _connect(client) as ws:
                ws.send_text("x" * (sec.WS_AUTH_MAX_BYTES + 1))
                ws.receive_text()
        assert e.value.code == 1009

    def test_malformed_frame_refused(self, client):
        with pytest.raises(WebSocketDisconnect) as e:
            with _connect(client) as ws:
                ws.send_text("{not-json")
                ws.receive_text()
        assert e.value.code == 1002

    def test_pre_auth_nonauth_message_refused(self, client):
        with pytest.raises(WebSocketDisconnect) as e:
            with _connect(client) as ws:
                ws.send_text(json.dumps({"type": "subscribe",
                                         "channel": "jobs"}))
                ws.receive_text()
        assert e.value.code == 1002

    def test_duplicate_auth_refused(self, client):
        with _connect(client) as ws:
            ws.send_text(AUTH)
            assert ws.receive_json() == {"type": "auth_ok"}
            ws.send_text(AUTH)
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_text()
            assert e.value.code == 1002

    def test_unauth_disconnect_releases_slot(self, client):
        with _connect(client):
            pass                      # disconnect without auth
        for _ in range(100):
            if client.app.state.ws_unauth_count == 0:
                break
            time.sleep(0.02)
        assert client.app.state.ws_unauth_count == 0


class TestUnauthenticatedPoolBound:
    def test_pool_cap_refuses_extra(self, client, monkeypatch):
        import contextlib
        monkeypatch.setattr(sec, "WS_MAX_UNAUTHENTICATED", 2)
        with contextlib.ExitStack() as stack:
            stack.enter_context(_connect(client))
            stack.enter_context(_connect(client))
            # Third unauthenticated handshake is refused while both
            # slots are held.
            with pytest.raises(WebSocketDisconnect) as e:
                _connect(client).__enter__()
            assert e.value.code == 1008

    def test_authenticated_clients_not_blocked(self, client,
                                               monkeypatch):
        """The bound applies to UNAUTHENTICATED sockets only — a legacy
        ?token= client still connects while the pool is full."""
        import contextlib
        monkeypatch.setattr(sec, "WS_MAX_UNAUTHENTICATED", 1)
        with contextlib.ExitStack() as stack:
            stack.enter_context(_connect(client))   # holds the 1 slot
            with client.websocket_connect(
                    f"/api/ws?token={TOKEN}") as ws2:
                _publish(client, "legacy")
                assert ws2.receive_json()["payload"]["id"] == "legacy"

    def test_slot_released_after_auth(self, client, monkeypatch):
        """An authenticating client holds the slot only during the
        handshake — after auth_ok the slot frees for the next."""
        monkeypatch.setattr(sec, "WS_MAX_UNAUTHENTICATED", 1)
        with _connect(client) as ws1:
            ws1.send_text(AUTH)
            assert ws1.receive_json() == {"type": "auth_ok"}
            # ws1 is AUTHENTICATED — the unauth slot is free again.
            with _connect(client) as ws2:
                ws2.send_text(AUTH)
                assert ws2.receive_json() == {"type": "auth_ok"}


class TestOriginPolicy:
    def test_untrusted_browser_origin_refused(self, client):
        with pytest.raises(WebSocketDisconnect):
            _connect(client, headers={
                "origin": "https://evil.example"}).__enter__()

    def test_null_origin_refused(self, client):
        with pytest.raises(WebSocketDisconnect):
            _connect(client, headers={"origin": "null"}).__enter__()

    def test_loopback_and_tauri_origins_accepted(self, client):
        for origin in ("http://tauri.localhost",
                       "http://localhost:1420",
                       "http://127.0.0.1:8788"):
            with _connect(client, headers={"origin": origin}) as ws:
                ws.send_text(AUTH)
                assert ws.receive_json() == {"type": "auth_ok"}

    def test_deceptive_origin_strings_refused(self, client):
        """Suffix tricks, embedded allowed-hosts, userinfo deception,
        non-http schemes, and unparseable ports are all refused —
        matching is exact hostname, never substring."""
        refused = [
            "http://localhost.evil.example",
            "http://127.0.0.1.evil.example",
            "http://evil.example#localhost",
            "http://localhost.evil.example:1420",
            "ws://localhost",                      # not an origin scheme
            "javascript://localhost",
            "http://tauri.localhost.evil.example",
            "http://localhost:99999",              # out-of-range port
            "http://localhost:abc",                # non-numeric port
            "HTTP://LOCALHOST.evil.example",
        ]
        for origin in refused:
            try:
                with _connect(client, headers={"origin": origin}):
                    pass
            except WebSocketDisconnect:
                continue
            pytest.fail(f"origin {origin!r} was not refused")

    def test_unexpected_ports_on_allowed_hosts(self, client):
        """Any port on an allowed host is accepted — dev tools and the
        sidecar itself bind varying loopback ports."""
        for origin in ("http://localhost:9999",
                       "http://127.0.0.1:65535",
                       "https://tauri.localhost:443"):
            with _connect(client, headers={"origin": origin}) as ws:
                ws.send_text(AUTH)
                assert ws.receive_json() == {"type": "auth_ok"}

    def test_origin_does_not_bypass_token(self, client):
        """Origin is defense-in-depth, not auth — a trusted origin with
        a bad token is still refused."""
        with pytest.raises(WebSocketDisconnect):
            with _connect(client, headers={
                    "origin": "http://localhost:1420"}) as ws:
                ws.send_text(json.dumps(
                    {"type": "auth", "token": "wrong"}))
                ws.receive_text()


class TestLegacyCompat:
    def test_query_token_still_works(self, client):
        with client.websocket_connect(
                f"/api/ws?token={TOKEN}") as ws:
            _publish(client, "legacy")
            evt = ws.receive_json()
            assert evt["event"] == "job.updated"

    def test_bad_query_token_refused(self, client):
        with pytest.raises(WebSocketDisconnect):
            client.websocket_connect("/api/ws?token=wrong").__enter__()


class TestTokenHygiene:
    def test_token_never_logged(self, client, caplog):
        secret = "sensitive-token-value-123"
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(WebSocketDisconnect):
                with _connect(client) as ws:
                    ws.send_text(json.dumps(
                        {"type": "auth", "token": secret}))
                    ws.receive_text()
        assert secret not in caplog.text

    def test_auth_error_reveals_no_token(self, client):
        """The close carries only a status code — no echoed frame, no
        token material in any outbound message."""
        with pytest.raises(WebSocketDisconnect):
            with _connect(client) as ws:
                ws.send_text(json.dumps(
                    {"type": "auth", "token": "echo-me-please"}))
                msg = ws.receive_text()
                assert "echo-me-please" not in (msg or "")
