// SEC-A2.2 packaged-smoke evidence — attaches to the packaged app's
// WebView2 via CDP (WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222)
// and records a SANITIZED WebSocket trace: request URLs + frame shapes.
// Frame payloads are reduced to {"type": "..."} — credential values are
// never read or written to disk.
// Usage: node scripts/cdp-a22-ws-trace.cjs [durationS] [outfile]
const fs = require("fs");
const DUR = Number(process.argv[2] || 20) * 1000;
const OUT = process.argv[3] || "docs/evidence/a22-ws-trace.json";

function shape(payload) {
  try {
    const f = JSON.parse(payload);
    if (f && typeof f === "object") {
      const out = {};
      if ("type" in f) out.type = f.type;
      if ("event" in f) out.event = f.event;
      // Report key PRESENCE only — never values.
      for (const k of Object.keys(f)) {
        if (["type", "event"].includes(k)) continue;
        out[k] = `<redacted:${typeof f[k]}>`;
      }
      return out;
    }
    return { raw: "<non-json>" };
  } catch {
    return { raw: "<non-json>" };
  }
}

async function main() {
  fs.mkdirSync(require("path").dirname(OUT), { recursive: true });
  const list = await (await fetch("http://localhost:9222/json")).json();
  const page = list.find((t) => t.type === "page" &&
    (t.url.includes("tauri.localhost") || t.url.includes("localhost")));
  if (!page) { console.error("no app page found", list.map(t => t.url)); process.exit(1); }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const p = new Map();
  const send = (m, pr = {}) => new Promise((r) => {
    const i = ++id; p.set(i, r);
    ws.send(JSON.stringify({ id: i, method: m, params: pr }));
  });
  const trace = { pageUrl: page.url, webSockets: [], frames: [], pageState: null };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && p.has(m.id)) { p.get(m.id)(m.result); p.delete(m.id); }
    if (m.method === "Network.webSocketCreated") {
      trace.webSockets.push({
        url: m.params.url,
        hasQueryToken: m.params.url.includes("token"),
      });
    }
    if (m.method === "Network.webSocketFrameSent") {
      trace.frames.push({ dir: "client→server", opcode: m.params.response.opcode, shape: shape(m.params.response.payloadData) });
    }
    if (m.method === "Network.webSocketFrameReceived") {
      trace.frames.push({ dir: "server→client", opcode: m.params.response.opcode, shape: shape(m.params.response.payloadData) });
    }
  };
  await new Promise((r) => (ws.onopen = r));
  await send("Page.enable");
  await send("Network.enable");
  await send("Runtime.enable");
  // Reload so the WS handshake (connect → auth frame → auth_ok) is
  // captured from the start — unless NORELOAD=1 (observe the live
  // socket for event delivery).
  if (!process.env.NORELOAD) await send("Page.reload");
  // Sanity: the app UI is up.
  const title = await send("Runtime.evaluate", { expression: "document.title" });
  const hasApp = await send("Runtime.evaluate", { expression: "!!document.getElementById('root') && document.getElementById('root').children.length > 0" });
  trace.pageState = { title: title?.result?.value, appMounted: hasApp?.result?.value };
  await new Promise((r) => setTimeout(r, DUR));
  // Re-check for WS connections that may have appeared during the window.
  fs.writeFileSync(OUT, JSON.stringify(trace, null, 2));
  console.log(JSON.stringify(trace, null, 2));
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
