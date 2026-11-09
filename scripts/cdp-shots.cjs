// Capture a series of app screens via CDP on :9222.
// Usage: node scripts/cdp-shots.cjs
const fs = require("fs");
const SHOTS = [
  ["#projects", "docs/m1-evidence/01-projects.png"],
  ["#project/overview", "docs/m1-evidence/02-overview.png"],
  ["#project/cast", "docs/m1-evidence/03-cast.png"],
  ["#project/assets", "docs/m1-evidence/04-assets.png"],
  ["#project/backups", "docs/m1-evidence/05-backups.png"],
  ["#project/settings", "docs/m1-evidence/06-settings.png"],
  ["#models", "docs/m1-evidence/07-models.png"],
];

async function main() {
  const list = await (await fetch("http://localhost:9222/json")).json();
  const page = list.find((t) => t.type === "page" && t.url.includes("1420"));
  if (!page) { console.error("no page"); process.exit(1); }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const p = new Map();
  const send = (m, pr = {}) =>
    new Promise((r) => { const i = ++id; p.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: pr })); });
  ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && p.has(m.id)) { p.get(m.id)(m.result); p.delete(m.id); } };
  await new Promise((r) => (ws.onopen = r));
  await send("Page.enable");
  for (const [hash, file] of SHOTS) {
    await send("Page.navigate", { url: "about:blank" });
    await new Promise((r) => setTimeout(r, 400));
    await send("Page.navigate", { url: `http://localhost:1420/${hash}` });
    await new Promise((r) => setTimeout(r, 4500));
    const s = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(file, Buffer.from(s.data, "base64"));
    console.log(file);
  }
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
