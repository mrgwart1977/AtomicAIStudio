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

export function assetFileUrl(projectId: string, assetId: string, info: BackendInfo): string {
  return `${info.baseUrl}/api/projects/${projectId}/assets/${assetId}/file?token=${encodeURIComponent(info.token)}`;
}

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

export function connectWs(onEvent: (e: WsEvent) => void, onState: (open: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 0;

  async function open() {
    const { baseUrl, token } = await getBackendInfo();
    const url = `${baseUrl.replace(/^http/, "ws")}/api/ws?token=${encodeURIComponent(token)}`;
    ws = new WebSocket(url);
    ws.onopen = () => {
      retry = 0;
      onState(true);
      // Keepalive so the server's read loop stays healthy.
      const ping = setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send("ping"), 15000);
      ws!.onclose = () => {
        clearInterval(ping);
        onState(false);
        if (!closed) setTimeout(open, Math.min(5000, 500 * ++retry));
      };
    };
    ws.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data) as WsEvent);
      } catch {
        /* non-JSON keepalive */
      }
    };
  }
  void open();
  return () => {
    closed = true;
    ws?.close();
  };
}
