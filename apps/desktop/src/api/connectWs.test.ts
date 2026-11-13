// SEC-A2.2 — WebSocket client first-frame authentication tests.
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { connectWs, invalidateBackendInfo } from "./client";
import type { WsEvent } from "./types";

const invoke = vi.fn();
vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...a: unknown[]) => invoke(...a),
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly OPEN = 1;
  readonly url: string;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000 });
  }
  // --- test helpers ---
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  deliver(data: string) {
    this.onmessage?.({ data });
  }
  fail(code: number) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

describe("connectWs — A2.2 first-frame auth", () => {
  let onEvent: (e: WsEvent) => void;
  let states: boolean[];

  beforeEach(() => {
    vi.useFakeTimers();
    invoke.mockReset();
    invoke.mockResolvedValue({ port: 8788, token: "tk-1" });
    invalidateBackendInfo();
    FakeWebSocket.instances = [];
    states = [];
    onEvent = vi.fn();
    window.__TAURI_INTERNALS__ = {};
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    delete window.__TAURI_INTERNALS__;
  });

  async function connect() {
    const dispose = connectWs(onEvent, (open) => states.push(open));
    await vi.advanceTimersByTimeAsync(0);   // flush getBackendInfo
    return dispose;
  }

  it("sends the auth frame first with no token in the URL", async () => {
    await connect();
    const ws = last();
    expect(ws.url).toBe("ws://127.0.0.1:8788/api/ws");
    expect(ws.url).not.toContain("?");
    expect(ws.url).not.toContain("tk-1");
    ws.open();
    expect(JSON.parse(ws.sent[0])).toEqual({ type: "auth", token: "tk-1" });
    expect(states).toEqual([]);           // not "open" until auth_ok
  });

  it("marks authenticated on auth_ok then delivers events", async () => {
    await connect();
    const ws = last();
    ws.open();
    ws.deliver(JSON.stringify({ type: "auth_ok" }));
    expect(states).toEqual([true]);
    ws.deliver(JSON.stringify({ event: "job.updated", payload: { id: "j" } }));
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ event: "job.updated" }));
  });

  it("drops event-shaped and malformed frames before auth_ok", async () => {
    await connect();
    const ws = last();
    ws.open();
    ws.deliver(JSON.stringify({ event: "job.updated" }));
    ws.deliver("not-json");
    ws.deliver(JSON.stringify({ type: "auth_error" }));
    expect(onEvent).not.toHaveBeenCalled();
    expect(states).toEqual([]);
  });

  it("never resubmits a refused token (1008 → non-retryable same creds)", async () => {
    await connect();
    const ws = last();
    ws.open();
    ws.fail(1008);                         // auth refused
    // Retry re-fetches credentials — same token returns → no resubmit.
    await vi.advanceTimersByTimeAsync(3000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(states).toContain(false);
  });

  it("resumes after token rotation — fresh creds re-authenticate", async () => {
    await connect();
    const ws = last();
    ws.open();
    ws.fail(1008);
    invoke.mockResolvedValue({ port: 8788, token: "tk-2" });
    await vi.advanceTimersByTimeAsync(3000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    const ws2 = last();
    ws2.open();
    expect(JSON.parse(ws2.sent[0])).toEqual({ type: "auth", token: "tk-2" });
    ws2.deliver(JSON.stringify({ type: "auth_ok" }));
    expect(states).toContain(true);
  });

  it("reconnects with bounded backoff and re-auths on server restart", async () => {
    await connect();
    const ws = last();
    ws.open();
    ws.deliver(JSON.stringify({ type: "auth_ok" }));
    ws.fail(1006);                         // network drop / server restart
    expect(states).toEqual([true, false]);
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeWebSocket.instances).toHaveLength(2);
    const ws2 = last();
    ws2.open();
    // Fresh authentication on every connection — never assumes auth.
    expect(JSON.parse(ws2.sent[0])).toEqual({ type: "auth", token: "tk-1" });
    // Pre-auth events on the new socket are dropped too.
    ws2.deliver(JSON.stringify({ event: "job.updated" }));
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("dispose cleans up the socket and stops reconnects", async () => {
    const dispose = await connect();
    const ws = last();
    ws.open();
    dispose();
    ws.fail(1006);
    await vi.advanceTimersByTimeAsync(10000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
