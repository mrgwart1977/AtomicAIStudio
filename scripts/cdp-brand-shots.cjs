// Capture branding shots via CDP on :9222 — header close-up + sizes.
const fs = require("fs");
const OUT = process.argv[2] || "docs/brand-evidence";

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const list = await (await fetch("http://localhost:9222/json")).json();
  const page = list.find((t) => t.type === "page" && t.url.includes("1420"));
  if (!page) { console.error("no page on :1420"); process.exit(1); }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const p = new Map();
  const send = (m, pr = {}) => new Promise((r) => {
    const i = ++id; p.set(i, r);
    ws.send(JSON.stringify({ id: i, method: m, params: pr }));
  });
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && p.has(m.id)) { p.get(m.id)(m.result); p.delete(m.id); }
  };
  await new Promise((r) => (ws.onopen = r));
  await send("Page.enable");

  const shots = [
    ["01-full-1440x900", { width: 1440, height: 900 }, null],
    ["02-header-clip", { width: 1440, height: 900 },
      { x: 0, y: 0, width: 700, height: 110, scale: 2 }],
    ["03-min-window-1100x700", { width: 1100, height: 700 }, null],
    ["04-min-header-clip", { width: 1100, height: 700 },
      { x: 0, y: 0, width: 620, height: 110, scale: 2 }],
    ["05-wide-1920", { width: 1920, height: 1080 }, null],
  ];
  for (const [name, vp, clip] of shots) {
    await send("Emulation.setDeviceMetricsOverride",
      { ...vp, deviceScaleFactor: 1, mobile: false });
    await send("Page.navigate", { url: "http://localhost:1420/" });
    await new Promise((r) => setTimeout(r, 3500));
    const params = { format: "png" };
    if (clip) params.clip = clip;
    const s = await send("Page.captureScreenshot", params);
    fs.writeFileSync(`${OUT}/${name}.png`, Buffer.from(s.data, "base64"));
    console.log(`${OUT}/${name}.png`);
  }
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
