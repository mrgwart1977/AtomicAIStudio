import type { BackendInfo, WsEvent } from "./types";

// Resolution order:
//  1. Inside the Tauri shell → ask Rust for the sidecar's port + session token.
//  2. Plain browser dev → connect to a backend started manually on :8788
//     with the "dev" token.
let cached: BackendInfo | null = null;

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export async function getBackendInfo(): Promise<BackendInfo> {
  if (cached) return cached;
  if (window.__TAURI_INTERNALS__) {
    const { invoke } = await import("@tauri-apps/api/core");
    const info = await invoke<{ port: number; token: string }>("get_backend_info");
    cached = { baseUrl: `http://127.0.0.1:${info.port}`, token: info.token };
  } else {
    cached = { baseUrl: "http://127.0.0.1:8788", token: "dev" };
  }
  return cached;
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const { baseUrl, token } = await getBackendInfo();
  const res = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Atomic-Token": token,
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

// SEC-A1: `assetFileUrl` (token-in-query URLs) was removed — asset bytes
// must be fetched with the X-Atomic-Token header via fetchAssetObjectUrl.

/** Authenticated binary fetch for an asset file — the session token
 * travels in the request header, never in a URL, so nothing lands in
 * server access logs, browser history, or caches. Returns an object
 * URL the caller must revoke. */
export async function fetchAssetObjectUrl(
  projectId: string, assetId: string,
): Promise<string> {
  const { baseUrl, token } = await getBackendInfo();
  const res = await fetch(
    `${baseUrl}/api/projects/${projectId}/assets/${assetId}/file`,
    { headers: { "X-Atomic-Token": token } },
  );
  if (!res.ok) throw new Error(`${res.status}`);
  return URL.createObjectURL(await res.blob());
}

/** Drop the cached backend info so the next connect re-asks the shell
 * for the sidecar's port + session token. Used after an authentication
 * refusal — a sidecar restart or token rotation mints a new token that
 * only the fresh invoke can see. */
export function invalidateBackendInfo(): void {
  cached = null;
}

/** A2.2 — WebSocket first-frame authentication.
 *
 * The session token travels in an in-band auth frame, never in the URL.
 * The socket is AUTHENTICATED only after the server's `auth_ok`;
 * anything arriving before that (including event-shaped messages) is
 * dropped. Close code 1008 = auth refusal → non-retryable until
 * credentials change: the cached token is invalidated and the next
 * attempt re-asks the shell — a sidecar restart/token rotation yields
 * a new token and resumes; the same token stops the loop so we never
 * resubmit known-bad credentials. Network failures retry with bounded
 * backoff and re-authenticate on every attempt. */
export function connectWs(onEvent: (e: WsEvent) => void, onState: (open: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 0;
  // The last token the server refused — never resubmit it; a retry is
  // only attempted after re-fetching credentials from the shell.
  let authFailedToken: string | null = null;

  async function open() {
    const { baseUrl, token } = await getBackendInfo();
    if (closed || token === authFailedToken) return;
    const sock = new WebSocket(`${baseUrl.replace(/^http/, "ws")}/api/ws`);
    ws = sock;
    let authed = false;
    let ping: ReturnType<typeof setInterval> | null = null;

    sock.onopen = () => {
      // First frame is authentication — nothing else may precede it.
      sock.send(JSON.stringify({ type: "auth", token }));
    };

    sock.onmessage = (m) => {
      if (ws !== sock) return;              // stale socket — drop
      if (!authed) {
        // Pre-auth: only auth_ok is meaningful; events or malformed
        // frames arriving before it are dropped, never delivered.
        try {
          const frame = JSON.parse(m.data) as { type?: unknown };
          if (frame?.type === "auth_ok") {
            authed = true;
            retry = 0;
            onState(true);
            ping = setInterval(
              () => sock.readyState === WebSocket.OPEN && sock.send("ping"),
              15000);
          }
        } catch { /* malformed handshake frame — ignore */ }
        return;
      }
      try {
        onEvent(JSON.parse(m.data) as WsEvent);
      } catch { /* non-JSON keepalive */ }
    };

    sock.onclose = (e) => {
      if (ws !== sock) return;
      if (ping !== null) clearInterval(ping);
      if (authed) onState(false);
      if (closed) return;
      if (e.code === 1008) {
        // Auth refusal — remember this token as known-bad. Drop the
        // cached backend info so the retry re-asks the shell: a
        // sidecar restart/rotation yields a new token and we resume;
        // the same token short-circuits in open() — no resubmission.
        authFailedToken = token;
        invalidateBackendInfo();
        onState(false);
        setTimeout(open, 2000);
        return;
      }
      // Network failure or post-auth drop — bounded backoff; every
      // attempt authenticates from scratch.
      setTimeout(open, Math.min(5000, 500 * ++retry));
    };
  }
  void open();
  return () => {
    closed = true;
    ws?.close();
  };
}
