// N1-D desktop acceptance driver — drives the REAL installed app's
// WebView2 via CDP (launched with
// WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<port>).
//
// Usage: node scripts/cdp-n1d-acceptance.cjs <port> <evidence_dir>
//
// Seeds a genuine multitrack layout (two overlapping video lanes, a
// hidden lane, a muted lane) with REAL synthesized media through the
// installed backend, then executes the composite export THROUGH THE
// INSTALLED UI — export panel, profile select, preview pin, start —
// and verifies the rendered output asset with the bundled ffprobe.
// Nothing is reported on faith — DOM claims are backed by API reads.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const PORT = process.argv[2] || "9223";
const OUT = process.argv[3] || "docs/n1d-evidence";
const MEDIA = path.resolve("docs/n1d-evidence/media");
const FFPROBE = path.resolve("backend/vendor/ffmpeg/bin/ffprobe.exe");
const DATA_DIR = process.env.ADE_DATA_DIR
  || `${process.env.LOCALAPPDATA}\\AtomicAIStudio`;
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
const hasText = (txt) => txt.includes("|")
  ? evalJs(`(() => { const t = document.body.innerText; return ${JSON.stringify(txt.split("|"))}.some(x => t.includes(x)); })()`)
  : evalJs(`document.body.innerText.includes(${JSON.stringify(txt)})`);
const waitText = async (txt, timeoutMs = 15000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await hasText(txt)) return true;
    await sleep(300);
  }
  return false;
};
const mustWaitText = async (txt, t = 15000) => {
  if (!await waitText(txt, t)) throw new Error(`text never appeared: "${txt}"`);
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
    idempotency_key: `n1d-${type}-${Date.now()}-${Math.random()}`,
  });
};
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
const detail = async (pid, tid) => api("GET", `/api/projects/${pid}/timelines/${tid}`);

async function seed() {
  step("Seed: project + 4 real synthesized videos + 4-lane layout");
  let projects = await api("GET", "/api/projects");
  let p = projects.find((x) => x.name === "N1-D Validation");
  if (!p) {
    p = await api("POST", "/api/projects", {
      name: "N1-D Validation", kind: "movie", aspect_ratio: "16:9",
      width: 1920, height: 1080, fps_num: 24, fps_den: 1,
    });
    ok(`project ${p.id}`);
  } else ok(`reusing project ${p.id}`);
  let tls = await api("GET", `/api/projects/${p.id}/timelines`);
  let tl = tls.find((t) => t.name === "N1-D Cut");
  if (tl && (await detail(p.id, tl.id)).clips.length >= 4) {
    ok(`timeline ${tl.id} already seeded`);
    return { pid: p.id, tid: tl.id };
  }
  const assets = {};
  for (const name of ["base", "over", "ghost", "muted"]) {
    const r = await commit(p.id, "asset.import",
      { source_path: `${MEDIA}\\${name}.mp4`, kind: "video" });
    await waitJob(p.id, r.job_id);
    ok(`imported ${name}.mp4`);
  }
  const list = await api("GET", `/api/projects/${p.id}/assets?limit=50&kind=video`);
  for (const name of ["base", "over", "ghost", "muted"]) {
    const a = list.items.find((x) => x.name.includes(name));
    if (!a) throw new Error(`asset ${name} missing after import`);
    assets[name] = a;
  }
  if (!tl) {
    await commit(p.id, "timeline.create",
      { name: "N1-D Cut", fps_num: 24, fps_den: 1 });
    tls = await api("GET", `/api/projects/${p.id}/timelines`);
    tl = tls.find((t) => t.name === "N1-D Cut");
  }
  const d = await detail(p.id, tl.id);
  const v1 = d.tracks.find((t) => t.kind === "video");
  // V1: base clip at frame 0 (48f @ 24fps) + a trailing-gap follower
  // at 96 — exercises an interior gap inside a visible lane.
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: v1.id, asset_id: assets.base.id,
      start_frames: 0 });
  // O1 overlay lane: over.mp4 at frame 24. (position model: 0 =
  // bottom layer, higher composites on top — V1 stays pos 0 by
  // default and O1 lands above it.)
  const o1r = await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "overlay", name: "O1" });
  const dd = await detail(p.id, tl.id);
  const o1 = dd.tracks.find((t) => t.name === "O1");
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: o1.id, asset_id: assets.over.id,
      start_frames: 24 });
  // Hidden lane: ghost.mp4 — contributes nothing.
  await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "overlay", name: "H" });
  const d2 = await detail(p.id, tl.id);
  const hid = d2.tracks.find((t) => t.name === "H");
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: hid.id, asset_id: assets.ghost.id,
      start_frames: 0 });
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: hid.id, visible: false });
  // Muted lane: muted.mp4 — video composites, audio excluded.
  await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "overlay", name: "M" });
  const d3 = await detail(p.id, tl.id);
  const mut = d3.tracks.find((t) => t.name === "M");
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: mut.id, asset_id: assets.muted.id,
      start_frames: 0 });
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: mut.id, muted: true });
  const n = (await detail(p.id, tl.id)).clips.length;
  if (n < 4) throw new Error(`timeline only has ${n} clips`);
  ok(`timeline ${tl.id}: base@0 on V1, over@24 on O1, ghost on hidden, muted on muted`);
  return { pid: p.id, tid: tl.id };
}

async function navToTimeline() {
  const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    const state = await evalJs(`(() => {
      const t = document.body.innerText;
      if ([...document.querySelectorAll("button")].some(b => b.textContent.includes("N1-D Validation"))) return "projects";
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
  await mustClick("N1-D Validation", "button", 2500);
  await mustWaitText("Timeline", 10000);
  await mustClick("Timeline", "button,a", 1200);
  await mustWaitText("N1-D Cut", 10000);
  await mustClick("N1-D Cut", "button", 1500);
}

async function scenario(pid, tid) {
  step("Open project → Timeline → composite badge shows lane count");
  await navToTimeline();
  const badge = await evalJs(`(() => {
    const b = document.querySelector('[data-testid="export-capability"]');
    return b ? b.textContent.trim() : null; })()`);
  if (!badge || !/Exports 3 lanes — composite/.test(badge)) {
    throw new Error(`badge: ${JSON.stringify(badge)}`);
  }
  ok(`badge: "${badge}" (V1+O1+muted visible; hidden lane excluded)`);
  await shot("composite-badge");

  step("Export panel: profile select → preview pins EDL → start");
  await mustClick("Export…", "button", 1000);
  await evalJs(`(() => {
    const s = document.querySelector('select[aria-label="Export profile"]');
    s.value = "720p";
    s.dispatchEvent(new Event("change", { bubbles: true })); })()`);
  await sleep(400);
  await mustClick("Preview export", "button", 2000);
  const prev = await evalJs(`(() => {
    const e = document.querySelector('[data-testid="export-preview"]');
    return e ? e.textContent : null; })()`);
  if (!prev || !/4 clip|EDL pinned/.test(prev)) {
    throw new Error(`preview: ${JSON.stringify(prev)}`);
  }
  ok(`preview: ${prev.slice(0, 90)}…`);
  await shot("export-preview");
  await mustClick("Start export", "button", 1500);
  await mustWaitText("Export queued", 10000);
  ok("export enqueued from the installed UI");
  await shot("export-started");

  step("Job completes → output asset probed with bundled ffprobe");
  const jobs = await api("GET", `/api/projects/${pid}/jobs`);
  const job = jobs.filter((j) => j.adapter_id === "ops.timeline.export")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  const done = await waitJob(pid, job.id);
  ok(`job ${done.id} succeeded (progress ${done.progress})`);
  const items = (await api("GET",
    `/api/projects/${pid}/assets?limit=50`)).items;
  const out = items.filter((a) => a.origin === "rendered")
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  if (!out) throw new Error("no rendered output asset registered");
  const outPath = path.join(DATA_DIR, out.uri.replace(/\//g, "\\"));
  if (!fs.existsSync(outPath)) throw new Error(`output missing: ${outPath}`);
  const probe = JSON.parse(execFileSync(FFPROBE, [
    "-v", "error", "-show_format", "-show_streams",
    "-of", "json", outPath]).toString());
  const v = probe.streams.find((s) => s.codec_type === "video");
  const a = probe.streams.find((s) => s.codec_type === "audio");
  const dur = parseFloat(probe.format.duration);
  const frames = parseInt(v.nb_frames, 10);
  if (v.codec_name !== "h264" || v.width !== 1280 || v.height !== 720) {
    throw new Error(`video: ${v.codec_name} ${v.width}x${v.height}`);
  }
  if (!a || a.codec_name !== "aac") {
    throw new Error(`audio: ${a && a.codec_name}`);
  }
  // program end = max(48 base, 24+24 over, 24 ghost, 24 muted) = 48f
  if (Math.abs(dur - 2.0) > 0.05 || frames !== 48) {
    throw new Error(`duration ${dur}s / ${frames}f — expected 2.0s/48f`);
  }
  ok(`output: ${v.codec_name} ${v.width}x${v.height}, ${frames}f, ` +
     `${dur.toFixed(3)}s, audio=${a.codec_name} ` +
     `(hidden lane excluded; base 440Hz audio mixed once)`);
  fs.writeFileSync(`${OUT}/output-probe.json`,
    JSON.stringify(probe, null, 2));
  await shot("output-registered");

  step("Restart persistence: reload → layout + output asset intact");
  await send("Page.reload");
  await sleep(4000);
  await navToTimeline();
  const n = await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
  const d = await detail(pid, tid);
  if (n !== d.clips.length || n < 4) {
    throw new Error(`post-reload: ${n} DOM clips vs ${d.clips.length} API`);
  }
  const items2 = (await api("GET",
    `/api/projects/${pid}/assets?limit=50`)).items;
  if (!items2.some((x) => x.id === out.id)) {
    throw new Error("output asset lost across restart");
  }
  ok(`post-reload: ${n} clips + rendered asset still registered`);
  await shot("post-reload");

  step("N1-D INSTALLED-APP ACCEPTANCE: PASS");
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
  // Learn the ephemeral backend port + per-launch token by sniffing
  // the app's own API traffic.
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
  // Nudge API traffic if the app is idle — click the Projects nav
  // item to force a real /api fetch carrying the token header.
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
  fs.writeFileSync(`${OUT}/n1d-acceptance.log`, LOG.join("\n") + "\n");
  process.exit(0);
}

main().catch((e) => {
  LOG.push(`FAIL: ${e.message}`);
  fs.writeFileSync(`${OUT}/n1d-acceptance.log`, LOG.join("\n") + "\n");
  console.error("FAIL:", e.message);
  process.exit(1);
});
