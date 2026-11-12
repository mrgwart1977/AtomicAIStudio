// One-off C2.0d evidence shots — Cloud Studio tab for the seeded project.
const fs = require("fs");
async function main() {
  const list = await (await fetch("http://localhost:9222/json")).json();
  const page = list.find((t) => t.type === "page" && t.url.includes("1420"));
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const p = new Map();
  const send = (m, pr = {}) => new Promise((r) => { const i = ++id; p.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: pr })); });
  ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && p.has(m.id)) { p.get(m.id)(m.result); p.delete(m.id); } };
  await new Promise((r) => (ws.onopen = r));
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1500, height: 1100, deviceScaleFactor: 1, mobile: false });
  const evalJs = (expr) => send("Runtime.evaluate", { expression: expr, awaitPromise: true });
  await send("Page.navigate", { url: `http://localhost:1420/#projects` });
  await new Promise((r) => setTimeout(r, 2500));
  const r1 = await evalJs(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.includes('C2.0d Evidence'));if(b)b.click();return !!b;})()`);
  console.log("clicked card:", JSON.stringify(r1.result));
  await new Promise((r) => setTimeout(r, 2200));
  await evalJs(`(()=>{const b=[...document.querySelectorAll('nav[aria-label="Workspace"] button')].find(x=>x.textContent.trim()==='Cloud');if(b)b.click();return !!b;})()`);
  await new Promise((r) => setTimeout(r, 3500));
  fs.mkdirSync("docs/c2d-evidence", { recursive: true });
  const s1 = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync("docs/c2d-evidence/01-cloud-studio.png", Buffer.from(s1.data, "base64"));
  console.log("shot 1 saved");
  // Scroll to budgets + reconciliation sections
  await evalJs(`(()=>{const els=[...document.querySelectorAll('h2')];const h=els.find(x=>x.textContent.includes('Spend'));if(h)h.scrollIntoView();return !!h;})()`);
  await new Promise((r) => setTimeout(r, 800));
  const s2 = await send("Page.captureScreenshot", { format: "png" });
  fs.writeFileSync("docs/c2d-evidence/02-cloud-budgets.png", Buffer.from(s2.data, "base64"));
  console.log("shot 2 saved");
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
