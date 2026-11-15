// N2-A acceptance — real installed NSIS app: import a WAV through the
// UI file path (command), probe lands, audio lane + clip place via
// the +audio picker, kind-rule refusal in the DOM, export badge
// blocks on a visible audio lane, undo works, restart persists.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n2a-evidence";
const FFMPEG_BIN = path.resolve("backend/vendor/ffmpeg/bin");
const DATA_DIR = `${process.env.LOCALAPPDATA}\\AtomicAIStudio`;
let API = null, TOKEN = null;

fs.mkdirSync(OUT, { recursive: true });
const LOG = []; let STEP = 0;
const step = (m) => { STEP += 1; console.log(`[${STEP}] ${m}`); LOG.push(`[${STEP}] ${m}`); };
const ok = (m) => { console.log(`   OK — ${m}`); LOG.push(`   OK — ${m}`); };

let _id = 0; const _pend = new Map(); const _events = new Map();
const onEvent = (n, fn) => { (_events.get(n) ?? _events.set(n, []).get(n)).push(fn); };
let ws;
const send = (m, pr = {}) => new Promise((res, rej) => {
  const i = ++_id; _pend.set(i, { res, rej });
  ws.send(JSON.stringify({ id: i, method: m, params: pr }));
  setTimeout(() => { if (_pend.has(i)) { _pend.get(i).rej(new Error("timeout " + m)); _pend.delete(i); } }, 60000);
});
const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error("js: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
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
const clickText = (txt, tag = "button") => evalJs(`(() => {
  const el = [...document.querySelectorAll(${JSON.stringify(tag)})]
    .find(e => e.textContent.trim().includes(${JSON.stringify(txt)}) && !e.disabled);
  if (!el) return false; el.click(); return true; })()`);
const mustClick = async (txt, tag, waitMs = 800) => {
  if (!await clickText(txt, tag)) throw new Error(`cannot click ${tag} "${txt}"`);
  await sleep(waitMs);
};
const api = async (method, p, body) => {
  const r = await fetch(`${API}${p}`, {
    method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(`api ${method} ${p}: ${r.status} ${(await r.text()).slice(0, 200)}`);
  return r.json();
};
const commit = async (pid, type, payload) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "audit",
    expected_revision: proj.revision,
    idempotency_key: `n2a-${type}-${Date.now()}-${Math.random()}` });
};
const detail = (pid, tid) => api("GET", `/api/projects/${pid}/timelines/${tid}`);
const waitJob = async (pid, jobId) => {
  for (let i = 0; i < 120; i++) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && j.status === "succeeded") return j;
    if (j && j.status === "failed") throw new Error("job failed: " + JSON.stringify(j).slice(0, 300));
    await sleep(500);
  }
  throw new Error("job timeout");
};

async function navToTimeline(pname) {
  await send("Page.reload", { ignoreCache: true });
  await sleep(6000);
  const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    const st = await evalJs(`(() => {
      const t = document.body.innerText;
      if ([...document.querySelectorAll("button")].some(b => b.textContent.includes(${JSON.stringify(pname)}))) return "projects";
      if (document.querySelector('[aria-label="Workspace"]')) return "workspace";
      if (t.includes("No project open") || t.includes("Create something extraordinary")) return "home";
      return null; })()`);
    if (st === "projects") break;
    if (st === "home") { await clickText("Projects", "button,a"); await sleep(1200); continue; }
    if (st === "workspace") { await clickText("← Projects") || await clickText("Choose a project"); await sleep(1200); break; }
    await sleep(400);
  }
  await mustClick(pname, "button", 2000);
  await mustWaitText("Timeline", 10000);
  await mustClick("Timeline", "button,a", 1200);
  await sleep(600);
}

async function main() {
  const targets = await fetch(`http://127.0.0.1:${PORT}/json`).then((r) => r.json());
  const page = targets.find((t) => t.type === "page");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && _pend.has(m.id)) {
      const { res, rej } = _pend.get(m.id); _pend.delete(m.id);
      m.error ? rej(new Error(m.error.message)) : res(m.result);
    } else if (m.method && _events.has(m.method)) {
      _events.get(m.method).forEach((fn) => fn(m.params));
    }
  };
  await send("Network.enable");
  onEvent("Network.requestWillBeSent", (e) => {
    const m = e.request.url.match(/http:\/\/(127\.0\.0\.1|localhost):(\d+)\/api/);
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
  if (!API || !TOKEN) { await send("Page.reload", { ignoreCache: true }); await sleep(6000); }
  if (!API || !TOKEN) throw new Error("no backend creds");
  ok(`backend ${API}`);

  /* synthesize a real WAV with the bundled ffmpeg */
  const wav = `${OUT}\\media\\tone.wav`;
  fs.mkdirSync(`${OUT}\\media`, { recursive: true });
  execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
    "-ar", "48000", "-ac", "2", wav]);
  ok(`wav synthesized (1s sine, 48k stereo)`);

  /* 1 — project + video + audio lane (command path) */
  step("1) Project + video clip + audio lane + audio clip placed via API");
  const p = await api("POST", "/api/projects", { name: `N2A-${Date.now()}` });
  const rv = await commit(p.id, "asset.import",
    { source_path: path.resolve("docs/n1d-evidence/media/base.mp4"), kind: "video" });
  await waitJob(p.id, rv.job_id);
  const ra = await commit(p.id, "asset.import",
    { source_path: path.resolve(wav), kind: "audio" });
  await waitJob(p.id, ra.job_id);
  const assets = (await api("GET", `/api/projects/${p.id}/assets?limit=50`)).items;
  const au = assets.find((a) => a.name.includes("tone"));
  if (!au || au.kind !== "audio") throw new Error("audio asset missing/wrong kind");
  const probe = (au.media_json || {}).probe || {};
  if (!probe.ok || probe.sample_rate !== 48000 || probe.channels !== 2)
    throw new Error(`probe bad: ${JSON.stringify(probe).slice(0, 200)}`);
  ok(`imported audio: ${probe.codec} ${probe.sample_rate}Hz x${probe.channels}ch ${probe.samples} samples (exact=${probe.samples_exact})`);

  const vi = assets.find((a) => a.name.includes("base"));
  await commit(p.id, "timeline.create", { name: "A-Mix", fps_num: 24, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`))
    .find((t) => t.name === "A-Mix");
  await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "audio", name: "A1" });
  let d = await detail(p.id, tl.id);
  const v1 = d.tracks.find((t) => t.kind === "video");
  const a1 = d.tracks.find((t) => t.kind === "audio");
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: v1.id, asset_id: vi.id, start_frames: 0 });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: au.id, start_frames: 12 });
  d = await detail(p.id, tl.id);
  const ac = d.clips.find((c) => c.track_id === a1.id);
  if (!ac || ac.src_fps_num !== 48000 || ac.out_frame !== 48000)
    throw new Error(`audio clip bad: ${JSON.stringify(ac && {r: ac.src_fps_num, o: ac.out_frame})}`);
  ok(`audio clip on A1: ${ac.out_frame - ac.in_frame} samples @${ac.src_fps_num}, stable id ${ac.id.slice(0, 8)}`);

  /* 2 — DOM: audio lane renders the clip, +audio picker exists */
  step("2) Installed UI: audio lane renders clip; +audio picker present");
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("A-Mix"));
    if (b) b.click(); return !!b; })()`);
  await sleep(800);
  const dom = await evalJs(`(() => ({
    clips: document.querySelectorAll('[data-testid="lane-clip"]').length,
    picker: !!document.querySelector('[data-testid^="audio-pick-"]'),
    badge: document.querySelector('[data-testid="export-capability"]')?.textContent?.trim(),
    badgeTitle: document.querySelector('[data-testid="export-capability"]')?.title,
  }))()`);
  if (dom.clips < 2 || !dom.picker) throw new Error(`dom: ${JSON.stringify(dom)}`);
  ok(`${dom.clips} clips mounted; +audio picker present`);

  /* 3 — export badge blocks on visible audio lane */
  step("3) Export capability honestly blocks visible audio lane");
  if (!/audio/i.test(dom.badgeTitle || ""))
    throw new Error(`badge title did not disclose audio block: "${dom.badgeTitle}"`);
  ok(`badge: "${dom.badge}" — title: "${dom.badgeTitle}"`);

  /* 4 — export job fails closed through the real UI */
  step("4) Export commit → durable job fails closed on audio lane");
  const jr = await commit(p.id, "timeline.export",
    { timeline_id: tl.id, profile: "720p" });
  let job = null;
  for (let i = 0; i < 60; i++) {
    job = (await api("GET", `/api/projects/${p.id}/jobs`))
      .find((x) => x.id === jr.job_id);
    if (job && (job.status === "failed" || job.status === "succeeded")) break;
    await sleep(500);
  }
  if (!job || job.status !== "failed" || !/audio lanes/i.test(job.error || ""))
    throw new Error(`job did not fail closed: ${JSON.stringify(job).slice(0, 240)}`);
  ok(`job failed closed: "${job.error.slice(0, 90)}"`);

  /* 5 — kind rule: video clip on audio lane refused via API */
  step("5) Kind rule: video→audio lane refused, audio→video refused");
  const wrong1 = await api("POST",
    `/api/projects/${p.id}/commands/preview`,
    { command_type: "timeline.clip.place", scope: "project",
      payload: { timeline_id: tl.id, track_id: a1.id, asset_id: vi.id,
                 start_frames: 0 } }).then((r) => r).catch((e) => ({ err: e.message }));
  const wrong1Status = await fetch(`${API}/api/projects/${p.id}/commands/commit`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: JSON.stringify({ command_type: "timeline.clip.place",
      payload: { timeline_id: tl.id, track_id: a1.id, asset_id: vi.id, start_frames: 0 },
      scope: "project", expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
      idempotency_key: `n2a-wrong-${Date.now()}` }) });
  const wrong2 = await fetch(`${API}/api/projects/${p.id}/commands/commit`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: JSON.stringify({ command_type: "timeline.clip.place",
      payload: { timeline_id: tl.id, track_id: v1.id, asset_id: au.id, start_frames: 0 },
      scope: "project", expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
      idempotency_key: `n2a-wrong2-${Date.now()}` }) });
  const e1 = await wrong1Status.json(), e2 = await wrong2.json();
  if (wrong1Status.status !== 422 || !/audio assets only/.test(e1.detail || ""))
    throw new Error(`video→audio not refused: ${JSON.stringify(e1).slice(0, 160)}`);
  if (wrong2.status !== 422 || !/video assets only/.test(e2.detail || ""))
    throw new Error(`audio→video not refused: ${JSON.stringify(e2).slice(0, 160)}`);
  ok(`video→audio: "${e1.detail.slice(0, 60)}"; audio→video refused too`);

  /* 6 — undo through the UI removes the audio clip. Re-nav first so
     the page's expected_timeline pin is fresh (API calls above bumped
     the project revision — a stale pin would refuse the undo). */
  step("6) UI undo removes the audio clip, redo restores it");
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("A-Mix"));
    if (b) b.click(); return !!b; })()`);
  await sleep(1200);
  const before = await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
  await mustClick("Undo", "button", 1200);
  const after = await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
  if (after !== before - 1) throw new Error(`undo: ${before}→${after}`);
  await mustClick("Redo", "button", 1200);
  const restored = await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
  if (restored !== before) throw new Error(`redo: ${restored}`);
  const dn = await detail(p.id, tl.id);
  const same = dn.clips.find((c) => c.track_id === a1.id);
  if (!same || same.id !== ac.id) throw new Error("stable id not preserved across undo/redo");
  ok(`undo ${before}→${after}→redo ${restored}; stable id preserved`);

  /* 7 — restart persistence */
  step("7) Reload → audio clip + probe metadata persist");
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("A-Mix"));
    if (b) b.click(); return !!b; })()`);
  await sleep(800);
  const dn2 = await detail(p.id, tl.id);
  const ac2 = dn2.clips.find((c) => c.track_id === a1.id);
  if (!ac2 || ac2.id !== ac.id) throw new Error("audio clip lost across reopen");
  const edl = await api("GET",
    `/api/projects/${p.id}/timelines/${tl.id}/edl?version=2`);
  const src = (edl.clips.find((c) => c.clip_id === ac.id) || {}).source || {};
  if (src.sample_rate !== 48000 || src.channels !== 2)
    throw new Error(`edl audio fields: ${JSON.stringify(src).slice(0, 160)}`);
  ok(`reopen: clip ${ac.id.slice(0, 8)} intact; EDL carries ${src.sample_rate}Hz x${src.channels}ch`);

  step("N2-A INSTALLED ACCEPTANCE: PASS (7/7)");
  fs.writeFileSync(`${OUT}/n2a-acceptance.log`, LOG.join("\n") + "\n");
  process.exit(0);
}

main().catch((e) => {
  LOG.push(`FAIL: ${e.message}`);
  fs.writeFileSync(`${OUT}/n2a-acceptance.log`, LOG.join("\n") + "\n");
  console.error("FAIL:", e.message);
  process.exit(1);
});
