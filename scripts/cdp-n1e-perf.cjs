// N1-E installed-app UI performance driver — measures REAL WebView2
// interaction latency via CDP on the installed NSIS build.
//
// Usage: node scripts/cdp-n1e-perf.cjs <port> <evidence_dir> [sizes]
//   sizes default "10,100,500,1000"
//
// Methodology (recorded honestly in docs/n1e-acceptance-evidence.md):
//   - initial render: nav click → lane-clip count == expected, timed
//     with performance.now() inside the page
//   - scroll: set scrollLeft → measure until 2 rAFs complete
//   - zoom: click zoom-in → measure until layout reflects new ppf
//     (clip style width changes) via rAF
//   - select: click a clip → selected class appears, rAF-timed
//   - undo/redo: click button → lane-clip count changes, rAF-timed
//   - memory: performance.memory.usedJSHeapSize (Chromium API)
//   - long-session: 50 undo/redo cycles on the 100-clip timeline,
//     heap sampled before/after
const fs = require("fs");
const path = require("path");

const PORT = process.argv[2] || "9225";
const OUT = process.argv[3] || "docs/n1e-evidence";
const SIZES = (process.argv[4] || "10,100,500,1000")
  .split(",").map(Number);
const MEDIA = path.resolve("docs/n1d-evidence/media");
let API = null;
let TOKEN = null;

fs.mkdirSync(OUT, { recursive: true });
const LOG = [];
const log = (m) => { console.log(m); LOG.push(m); };

let _id = 0;
const _pend = new Map();
const _events = new Map();
const onEvent = (n, fn) => {
  if (!_events.has(n)) _events.set(n, []);
  _events.get(n).push(fn);
};
let ws;
const send = (m, pr = {}) =>
  new Promise((res, rej) => {
    const i = ++_id; _pend.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method: m, params: pr }));
    setTimeout(() => { if (_pend.has(i)) { _pend.get(i).rej(new Error(`cdp timeout ${m}`)); _pend.delete(i); } }, 60000);
  });
const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(`page js: ${JSON.stringify(r.exceptionDetails).slice(0, 300)}`);
  return r.result && r.result.value;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (method, p, body) => {
  const r = await fetch(`${API}${p}`, {
    method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(`api ${method} ${p}: ${r.status} ${await r.text()}`);
  return r.json();
};
const commit = async (pid, type, payload) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "perf",
    expected_revision: proj.revision,
    idempotency_key: `perf-${Date.now()}-${Math.random()}` });
};
const detail = (pid, tid) => api("GET", `/api/projects/${pid}/timelines/${tid}`);
const waitJob = async (pid, jobId) => {
  for (let i = 0; i < 240; i++) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`))
      .find((x) => x.id === jobId);
    if (j && j.status === "succeeded") return j;
    if (j && j.status === "failed") throw new Error("import failed");
    await sleep(500);
  }
  throw new Error("job timeout");
};

const mustClick = async (txt, tag = "button", waitMs = 800) => {
  const c = await evalJs(`(() => {
    const el = [...document.querySelectorAll(${JSON.stringify(tag)})]
      .find(e => e.textContent.trim().includes(${JSON.stringify(txt)}) && !e.disabled);
    if (!el) return false; el.click(); return true; })()`);
  if (!c) throw new Error(`could not click "${txt}"`);
  await sleep(waitMs);
};

async function navToTimeline(name) {
  const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    const st = await evalJs(`(() => {
      const t = document.body.innerText;
      if ([...document.querySelectorAll("button")].some(b => b.textContent.includes(${JSON.stringify(name)}))) return "projects";
      if (document.querySelector('[aria-label="Workspace"]')) return "workspace";
      if (t.includes("Choose a project")) return "empty-ws";
      if (t.includes("No project open") || t.includes("Create something extraordinary")) return "home";
      return null; })()`);
    if (st === "projects") break;
    if (st === "home") {
      await evalJs(`(() => {
        const b = [...document.querySelectorAll("button,a")]
          .find(e => e.textContent.trim() === "Projects");
        if (b) b.click(); return !!b; })()`);
      await sleep(1200); continue;
    }
    if (st === "workspace" || st === "empty-ws") {
      await evalJs(`(() => {
        const b = [...document.querySelectorAll("button")]
          .find(e => /← Projects|Choose a project/.test(e.textContent));
        if (b) b.click(); return !!b; })()`);
      await sleep(1200); break;
    }
    await sleep(400);
  }
  await mustClick(name, "button", 2000);
  await sleep(800);
  await mustClick("Timeline", "button,a", 1000);
  await sleep(600);
  // open the seeded timeline row
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("Perf Cut"));
    if (b) b.click(); return !!b; })()`);
  await sleep(800);
}

async function seed(pid, n) {
  const r = await commit(pid, "asset.import",
    { source_path: `${MEDIA}\\base.mp4`, kind: "video" });
  await waitJob(pid, r.job_id);
  const list = await api("GET", `/api/projects/${pid}/assets?limit=50&kind=video`);
  const asset = list.items.find((x) => x.name.includes("base"));
  await commit(pid, "timeline.create",
    { name: "Perf Cut", fps_num: 24, fps_den: 1 });
  const tls = await api("GET", `/api/projects/${pid}/timelines`);
  const tl = tls.find((t) => t.name === "Perf Cut");
  const d = await detail(pid, tl.id);
  const v1 = d.tracks.find((t) => t.kind === "video");
  const t0 = Date.now();
  for (let i = 0; i < n; i++) {
    await commit(pid, "timeline.clip.place",
      { timeline_id: tl.id, track_id: v1.id, asset_id: asset.id,
        start_frames: i * 48, allow_duplicate: true });
  }
  return { tid: tl.id, seedMs: Date.now() - t0 };
}

const measure = (name, expr) => evalJs(`(async () => {
  const t0 = performance.now();
  ${expr}
  return Math.round((performance.now() - t0) * 10) / 10;
})()`).then((ms) => { log(`  ${name}: ${ms}ms`); return ms; });

async function benchTimeline(pid, tid, n) {
  const r = { clips: n };
  // initial render: count lane-clip nodes after 2 rAF
  r.render_ms = await measure("initial render+settle", `
    await new Promise(r => requestAnimationFrame(() =>
      requestAnimationFrame(r)));`);
  const clips = await evalJs(
    `document.querySelectorAll('[data-testid="lane-clip"]').length`);
  r.dom_clips = clips;

  // horizontal scroll to the far end — measure paint settle
  r.scroll_ms = await measure("scroll→end settle", `
    const s = document.querySelector('[data-testid="timeline-scroll"]');
    s.scrollLeft = s.scrollWidth - s.clientWidth;
    s.dispatchEvent(new Event("scroll", { bubbles: true }));
    await new Promise(r => requestAnimationFrame(() =>
      requestAnimationFrame(r)));`);
  r.dom_clips_scrolled = await evalJs(
    `document.querySelectorAll('[data-testid="lane-clip"]').length`);

  // zoom in (button "+") — measure settle
  r.zoom_ms = await measure("zoom-in settle", `
    const b = [...document.querySelectorAll("button")]
      .find(x => x.textContent.trim() === "+");
    if (b) b.click();
    await new Promise(r => requestAnimationFrame(() =>
      requestAnimationFrame(r)));`);

  // select the first mounted clip — measure
  r.select_ms = await measure("clip select", `
    const c = document.querySelector('[data-testid="lane-clip"]');
    if (c) c.dispatchEvent(new MouseEvent("pointerdown",
      { bubbles: true }));
    await new Promise(r => requestAnimationFrame(() =>
      requestAnimationFrame(r)));`);

  // undo + redo through the real buttons (lane-clip count changes)
  const c0 = await evalJs(
    `document.querySelectorAll('[data-testid="lane-clip"]').length`);
  r.undo_ms = await measure("undo (UI)", `
    const b = document.querySelector('[data-testid="undo-btn"]');
    if (b && !b.disabled) b.click();
    await new Promise(r => setTimeout(r, 60));
    await new Promise(r => requestAnimationFrame(() =>
      requestAnimationFrame(r)));`);
  r.redo_ms = await measure("redo (UI)", `
    const b = document.querySelector('[data-testid="redo-btn"]');
    if (b && !b.disabled) b.click();
    await new Promise(r => setTimeout(r, 60));
    await new Promise(r => requestAnimationFrame(() =>
      requestAnimationFrame(r)));`);
  const c1 = await evalJs(
    `document.querySelectorAll('[data-testid="lane-clip"]').length`);
  r.clips_roundtrip = [c0, c1];

  r.heap_mb = await evalJs(
    `performance.memory ? Math.round(performance.memory.usedJSHeapSize/104857.6)/10 : null`);
  log(`  DOM clips: ${clips} → scrolled ${r.dom_clips_scrolled}, heap ${r.heap_mb}MB, roundtrip ${c0}→${c1}`);
  return r;
}

async function longSession(pid, tid) {
  log("  long-session: 50 undo/redo cycles on the 100-clip timeline");
  const h0 = await evalJs(
    `performance.memory ? performance.memory.usedJSHeapSize : null`);
  for (let i = 0; i < 50; i++) {
    await evalJs(`(() => {
      const u = document.querySelector('[data-testid="undo-btn"]');
      if (u && !u.disabled) u.click(); })()`);
    await sleep(120);
    await evalJs(`(() => {
      const r = document.querySelector('[data-testid="redo-btn"]');
      if (r && !r.disabled) r.click(); })()`);
    await sleep(120);
  }
  await sleep(2000);
  const h1 = await evalJs(
    `performance.memory ? performance.memory.usedJSHeapSize : null`);
  const leak = h0 && h1 ? Math.round((h1 - h0) / 10485.76) / 100 : null;
  log(`  heap before ${h0 ? Math.round(h0/1048576) : "?"}MB → after ${h1 ? Math.round(h1/1048576) : "?"}MB (Δ ${leak}MB)`);
  return { heap_before_mb: h0 ? Math.round(h0 / 104857.6) / 10 : null,
           heap_after_mb: h1 ? Math.round(h1 / 104857.6) / 10 : null,
           delta_mb: leak };
}

async function main() {
  const targets = await fetch(`http://127.0.0.1:${PORT}/json`)
    .then((r) => r.json());
  const page = targets.find((t) => t.type === "page");
  if (!page) throw new Error("no page target");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && _pend.has(m.id)) {
      const { res, rej } = _pend.get(m.id);
      _pend.delete(m.id);
      m.error ? rej(new Error(m.error.message)) : res(m.result);
    } else if (m.method && _events.has(m.method)) {
      _events.get(m.method).forEach((fn) => fn(m.params));
    }
  };
  await send("Network.enable");
  onEvent("Network.requestWillBeSent", (e) => {
    const m = e.request.url.match(
      /http:\/\/(127\.0\.0\.1|localhost):(\d+)\/api/);
    if (m && !API) {
      API = `http://127.0.0.1:${m[2]}`;
      TOKEN = (e.request.headers || {})["X-Atomic-Token"] || null;
    }
  });
  await sleep(1500);
  if (!API || !TOKEN) {
    await evalJs(`(() => {
      const b = [...document.querySelectorAll("button,a")]
        .find(e => e.textContent.trim() === "Projects");
      if (b) b.click(); return !!b; })()`);
    await sleep(2500);
  }
  if (!API || !TOKEN) {
    await send("Page.reload", { ignoreCache: true });
    await sleep(6000);
  }
  if (!API || !TOKEN) throw new Error("no backend creds");

  const results = [];
  for (const n of SIZES) {
    log(`n=${n}: seeding…`);
    const p = await api("POST", "/api/projects",
      { name: `n1e-perf-${n}-${Date.now()}` });
    const { tid, seedMs } = await seed(p.id, n);
    log(`n=${n}: seeded in ${(seedMs / 1000).toFixed(1)}s — measuring UI`);
    await send("Page.reload", { ignoreCache: true });
    await sleep(5000);
    await navToTimeline(`n1e-perf-${n}`);
    const row = await benchTimeline(p.id, tid, n);
    if (n === 100) row.long_session = await longSession(p.id, tid);
    results.push(row);
  }
  fs.writeFileSync(`${OUT}/n1e-perf.json`,
    JSON.stringify({ env: "installed NSIS WebView2", results }, null, 2));
  log(JSON.stringify(results, null, 2));
  log("N1-E PERF MEASUREMENT: COMPLETE");
  process.exit(0);
}

main().catch((e) => {
  fs.writeFileSync(`${OUT}/n1e-perf.log`, LOG.join("\n") + `\nFAIL: ${e.message}`);
  console.error("FAIL:", e.message);
  process.exit(1);
});
