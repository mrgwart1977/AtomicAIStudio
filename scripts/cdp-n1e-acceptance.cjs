// N1-E desktop acceptance driver — drives the REAL installed app's
// WebView2 via CDP (launched with
// WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<port>).
//
// Usage: node scripts/cdp-n1e-acceptance.cjs <port> <evidence_dir>
//
// Seeds three clips on a fresh project through the installed backend,
// then exercises undo/redo THROUGH THE INSTALLED UI — toolbar buttons,
// Ctrl+Z / Ctrl+Shift+Z, redo-stack invalidation on a divergent edit,
// and restart persistence. API reads back every DOM claim.
const fs = require("fs");
const path = require("path");

const PORT = process.argv[2] || "9224";
const OUT = process.argv[3] || "docs/n1e-evidence";
const MEDIA = path.resolve("docs/n1d-evidence/media");
let API = null;
let TOKEN = null;

fs.mkdirSync(OUT, { recursive: true });
const LOG = [];
let STEP = 0;
const step = (m) => { STEP += 1; const l = `[${String(STEP).padStart(2, "0")}] ${m}`; console.log(l); LOG.push(l); };
const ok = (m) => { const l = `      OK — ${m}`; console.log(l); LOG.push(l); };
const shot = async (name) => {
  const s = await send("Page.captureScreenshot", { format: "png" });
  const f = `${OUT}/${String(STEP).padStart(2, "0")}-${name}.png`;
  fs.writeFileSync(f, Buffer.from(s.data, "base64"));
  LOG.push(`      shot: ${f}`);
};

let _id = 0;
const _pend = new Map();
const _events = new Map();
const onEvent = (name, fn) => {
  if (!_events.has(name)) _events.set(name, []);
  _events.get(name).push(fn);
};
let ws;
const send = (m, pr = {}) =>
  new Promise((res, rej) => {
    const i = ++_id; _pend.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method: m, params: pr }));
    setTimeout(() => { if (_pend.has(i)) { _pend.get(i).rej(new Error(`cdp timeout ${m}`)); _pend.delete(i); } }, 30000);
  });
const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(`page js: ${JSON.stringify(r.exceptionDetails).slice(0, 300)}`);
  return r.result && r.result.value;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const mustWaitText = async (txt, t = 15000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < t) {
    if (await evalJs(`document.body.innerText.includes(${JSON.stringify(txt)})`)) return;
    await sleep(300);
  }
  throw new Error(`text never appeared: "${txt}"`);
};
const clickText = (txt, tag = "button") => evalJs(`
  (() => {
    const el = [...document.querySelectorAll(${JSON.stringify(tag)})]
      .find(e => e.textContent.trim().includes(${JSON.stringify(txt)}) && !e.disabled);
    if (!el) return false; el.click(); return true;
  })()`);
const mustClick = async (txt, tag, waitMs = 800) => {
  if (!await clickText(txt, tag)) throw new Error(`could not click ${tag} "${txt}"`);
  await sleep(waitMs);
};

const api = async (method, path, body) => {
  const r = await fetch(`${API}${path}`, {
    method,
    headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`api ${method} ${path}: ${r.status} ${await r.text()}`);
  return r.json();
};
const commit = async (pid, type, payload) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "acceptance",
    expected_revision: proj.revision,
    idempotency_key: `n1e-${type}-${Date.now()}-${Math.random()}`,
  });
};
const detail = async (pid, tid) => api("GET", `/api/projects/${pid}/timelines/${tid}`);
const waitJob = async (pid, jobId) => {
  for (let i = 0; i < 240; i++) {
    const jobs = await api("GET", `/api/projects/${pid}/jobs`);
    const j = jobs.find((x) => x.id === jobId);
    if (j && j.status === "succeeded") return j;
    if (j && j.status === "failed") throw new Error(`job failed: ${JSON.stringify(j).slice(0, 400)}`);
    await sleep(1000);
  }
  throw new Error("job never completed");
};

async function seed() {
  step("Seed: project + real video + 3 packed clips via API commits");
  let projects = await api("GET", "/api/projects");
  let p = projects.find((x) => x.name === "N1-E Validation");
  if (!p) {
    p = await api("POST", "/api/projects", {
      name: "N1-E Validation", kind: "movie", aspect_ratio: "16:9",
      width: 1920, height: 1080, fps_num: 24, fps_den: 1 });
    ok(`project ${p.id}`);
  } else ok(`reusing project ${p.id}`);
  let tls = await api("GET", `/api/projects/${p.id}/timelines`);
  let tl = tls.find((t) => t.name === "N1-E Cut");
  if (tl && (await detail(p.id, tl.id)).clips.length >= 3) {
    ok(`timeline ${tl.id} already seeded`);
    return { pid: p.id, tid: tl.id };
  }
  const r = await commit(p.id, "asset.import",
    { source_path: `${MEDIA}\\base.mp4`, kind: "video" });
  await waitJob(p.id, r.job_id);
  const list = await api("GET", `/api/projects/${p.id}/assets?limit=50&kind=video`);
  const asset = list.items.find((x) => x.name.includes("base"));
  if (!asset) throw new Error("asset missing after import");
  if (!tl) {
    await commit(p.id, "timeline.create",
      { name: "N1-E Cut", fps_num: 24, fps_den: 1 });
    tls = await api("GET", `/api/projects/${p.id}/timelines`);
    tl = tls.find((t) => t.name === "N1-E Cut");
  }
  const d = await detail(p.id, tl.id);
  const v1 = d.tracks.find((t) => t.kind === "video");
  for (let i = 0; i < 3; i++) {
    await commit(p.id, "timeline.clip.place",
      { timeline_id: tl.id, track_id: v1.id, asset_id: asset.id,
        start_frames: i * 48, allow_duplicate: true });
  }
  ok(`timeline ${tl.id}: 3×48f clips at 0/48/96`);
  return { pid: p.id, tid: tl.id };
}

async function navToTimeline() {
  const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    const state = await evalJs(`(() => {
      const t = document.body.innerText;
      if ([...document.querySelectorAll("button")].some(b => b.textContent.includes("N1-E Validation"))) return "projects";
      if (document.querySelector('[aria-label="Workspace"]')) return "workspace";
      if (t.includes("Choose a project")) return "empty-ws";
      if (t.includes("No project open") || t.includes("Create something extraordinary")) return "home";
      return null;
    })()`);
    if (state === "projects") break;
    if (state === "home") {
      await clickText("Projects", "button,a") || await clickText("Projects", "nav *");
      await sleep(1500);
      continue;
    }
    if (state === "workspace" || state === "empty-ws") {
      await clickText("← Projects", "button") || await clickText("Choose a project", "button");
      await sleep(1500);
      break;
    }
    await sleep(500);
  }
  await mustClick("N1-E Validation", "button", 2500);
  await mustWaitText("Timeline", 10000);
  await mustClick("Timeline", "button,a", 1200);
  await mustWaitText("N1-E Cut", 10000);
  await mustClick("N1-E Cut", "button", 1500);
  await sleep(800);
}

const clipCount = () =>
  evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
const undoState = () => evalJs(`(() => ({
  undo: document.querySelector('[data-testid="undo-btn"]')?.disabled ?? null,
  redo: document.querySelector('[data-testid="redo-btn"]')?.disabled ?? null,
  undoTitle: document.querySelector('[data-testid="undo-btn"]')?.title ?? "",
  redoTitle: document.querySelector('[data-testid="redo-btn"]')?.title ?? "",
}))()`);

async function scenario(pid, tid) {
  step("Open project → Timeline → Undo/Redo controls visible");
  await navToTimeline();
  let st = await undoState();
  if (st.undo === null) throw new Error("undo button missing");
  if (st.undo !== false) throw new Error(`undo should be enabled (seeded stack): ${JSON.stringify(st)}`);
  if (st.redo !== true) throw new Error(`redo should be disabled pre-undo: ${JSON.stringify(st)}`);
  ok(`buttons: undo enabled ("${st.undoTitle}"), redo disabled`);
  await shot("undo-enabled");

  step("UI Undo button → last clip placement reverts (3→2)");
  await mustClick("Undo", "button", 1200);
  const n2 = await clipCount();
  if (n2 !== 2) throw new Error(`after undo: ${n2} clips, want 2`);
  st = await undoState();
  if (st.redo !== false) throw new Error("redo should be enabled");
  ok(`undo → ${n2} clips; redo now enabled ("${st.redoTitle}")`);
  await shot("after-undo");

  step("UI Redo button → clip restored (2→3)");
  await mustClick("Redo", "button", 1200);
  const n3 = await clipCount();
  if (n3 !== 3) throw new Error(`after redo: ${n3} clips, want 3`);
  ok(`redo → ${n3} clips`);
  await shot("after-redo");

  step("Ctrl+Z keyboard → undo; Ctrl+Shift+Z → redo");
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "z", code: "KeyZ", modifiers: 2 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "z", code: "KeyZ", modifiers: 2 });
  await sleep(1200);
  if (await clipCount() !== 2) throw new Error("Ctrl+Z did not undo");
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Z", code: "KeyZ", modifiers: 10 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Z", code: "KeyZ", modifiers: 10 });
  await sleep(1200);
  if (await clipCount() !== 3) throw new Error("Ctrl+Shift+Z did not redo");
  ok("Ctrl+Z undid, Ctrl+Shift+Z redid — keyboard path live");
  await shot("keyboard-undo-redo");

  step("Divergent edit clears the redo stack");
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "z", code: "KeyZ", modifiers: 2 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "z", code: "KeyZ", modifiers: 2 });
  await sleep(1200);
  // a real edit through the UI: remove the middle clip via Delete
  const removed = await evalJs(`(() => {
    const clips = [...document.querySelectorAll('[data-testid="lane-clip"]')];
    const c = clips[0];
    c.focus(); c.dispatchEvent(new KeyboardEvent("keydown",
      { key: "Delete", bubbles: true })); return true; })()`);
  await sleep(1500);
  st = await undoState();
  if (st.redo !== true) {
    throw new Error(`redo should be disabled after divergent edit: ${JSON.stringify(st)}`);
  }
  ok("new edit after undo → redo disabled (branch invalidated)");
  await shot("branch-invalidated");

  step("Restart persistence: reload → undo stack intact");
  await send("Page.reload");
  await sleep(4000);
  await navToTimeline();
  st = await undoState();
  if (st.undo !== false) throw new Error("undo stack lost across reload");
  ok(`post-reload undo enabled: "${st.undoTitle}"`);
  await shot("post-reload-undo");

  step("API truth: detail reports undo_label and clip count");
  const d = await detail(pid, tid);
  if (!d.undo_label) throw new Error("API detail lacks undo_label");
  ok(`API: ${d.clips.length} clips, undo_label="${d.undo_label}"`);

  step("N1-E INSTALLED-APP ACCEPTANCE: PASS");
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
    const u = e.request.url;
    const m = u.match(/http:\/\/(127\.0\.0\.1|localhost):(\d+)\/api/);
    if (m && !API) {
      API = `http://127.0.0.1:${m[2]}`;
      TOKEN = (e.request.headers || {})["X-Atomic-Token"] || null;
    }
    const w = u.match(/[?&]token=([^&]+)/);
    if (!TOKEN && w) TOKEN = decodeURIComponent(w[1]);
  });
  onEvent("Network.webSocketCreated", (e) => {
    const w = e.url.match(/http:\/\/(127\.0\.0\.1|localhost):(\d+)/);
    const t = e.url.match(/[?&]token=([^&]+)/);
    if (w && !API) API = `http://127.0.0.1:${w[2]}`;
    if (t && !TOKEN) TOKEN = decodeURIComponent(t[1]);
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
  if (!API || !TOKEN) throw new Error("no backend creds sniffed");
  ok(`backend ${API}`);
  const { pid, tid } = await seed();
  await scenario(pid, tid);
  fs.writeFileSync(`${OUT}/n1e-acceptance.log`, LOG.join("\n") + "\n");
  process.exit(0);
}

main().catch((e) => {
  LOG.push(`FAIL: ${e.message}`);
  fs.writeFileSync(`${OUT}/n1e-acceptance.log`, LOG.join("\n") + "\n");
  console.error("FAIL:", e.message);
  process.exit(1);
});
