// C1.5 evidence — drive the Assisted text panel in Cloud Studio via CDP.
// Prereqs: backend :8788 (token dev), vite :1420, Edge on :9222.
// Usage: node scripts/cdp-c15-shots.cjs [outdir]
const fs = require("fs");
const OUT = process.argv[2] || "docs/c15-evidence";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const list = await (await fetch("http://localhost:9222/json")).json();
  const page = list.find((t) => t.type === "page" && t.url.includes("1420"));
  if (!page) { console.error("no page on 1420"); process.exit(1); }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const p = new Map();
  const send = (m, pr = {}) =>
    new Promise((r) => { const i = ++id; p.set(i, r); ws.send(JSON.stringify({ id: i, method: m, params: pr })); });
  ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && p.has(m.id)) { p.get(m.id)(m.result); p.delete(m.id); } };
  await new Promise((r) => (ws.onopen = r));
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1500, deviceScaleFactor: 1, mobile: false });
  const evalJs = async (expr) => (await send("Runtime.evaluate",
    { expression: expr, awaitPromise: true, returnByValue: true })).result?.value;
  const shot = async (file) => {
    const s = await send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(`${OUT}/${file}`, Buffer.from(s.data, "base64"));
    console.log(`${OUT}/${file}`);
  };

  // pick a project (C2.0d Evidence) then open the Cloud tab
  await send("Page.navigate", { url: "about:blank" }); await sleep(400);
  await send("Page.navigate", { url: "http://localhost:1420/" }); await sleep(3500);
  const picked = await evalJs(`
    (() => {
      const btns = [...document.querySelectorAll('button, [role=button], a, li, [class*=card], [class*=row]')];
      const hit = btns.find(b => (b.textContent||'').includes('C2.0d Evidence'));
      if (hit) { hit.click(); return 'clicked'; }
      return 'notfound:' + document.body.innerText.slice(0, 300);
    })()`);
  console.log("project pick:", picked);
  await sleep(2500);
  const tab = await evalJs(`
    (() => {
      const b = [...document.querySelectorAll('button, a, [role=tab]')]
        .find(x => (x.textContent||'').trim() === 'Cloud');
      if (b) { b.click(); return 'clicked'; }
      location.hash = '#project/cloud'; return 'hash';
    })()`);
  console.log("cloud tab:", tab);
  await sleep(3500);

  // scroll the assisted panel into view, select Sudowrite
  const sel = await evalJs(`
    (() => {
      const panel = document.querySelector('[data-testid=assisted-panel]');
      if (!panel) return 'no-panel:' + location.hash;
      panel.scrollIntoView();
      const s = document.getElementById('assist-provider');
      const opt = [...s.options].find(o => o.value === 'sudowrite');
      if (!opt) return 'no-sudowrite:' + [...s.options].map(o => o.value);
      s.value = 'sudowrite';
      s.dispatchEvent(new Event('change', { bubbles: true }));
      return 'selected';
    })()`);
  console.log("provider select:", sel);
  await sleep(1500);
  await shot("01-assisted-eligible.png");

  // type a prompt + pick a context entity if any, then screenshot before prepare
  await evalJs(`
    (() => {
      const t = document.getElementById('assist-prompt');
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(t, 'Write a short atmospheric opening paragraph for a neon-noir short film.');
      t.dispatchEvent(new Event('input', { bubbles: true }));
      const cb = document.querySelector('fieldset input[type=checkbox]');
      if (cb) cb.click();
      return 'typed';
    })()`);
  await sleep(800);
  await shot("02-assisted-share-preview.png");

  // click Prepare → the audited bridge.prepare runs → review card renders
  const prep = await evalJs(`
    (() => {
      const b = document.querySelector('[data-testid=assist-prepare]');
      if (!b || b.disabled) return 'no-btn';
      b.click(); return 'clicked';
    })()`);
  console.log("prepare:", prep);
  await sleep(2000);
  await shot("03-assisted-review.png");
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
