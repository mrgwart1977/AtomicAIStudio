// N2-E acceptance — real installed NSIS app: import audio, place clips,
// gains, fades, crossfade, automation, then EXPORT WITH MIXED AUDIO and
// verify the rendered output with ffprobe + PCM measurement. Includes
// detached-crossfade fail-closed, audio-only render, export-mode UI
// copy, restart persistence, and clean shutdown. Zero credentials/cloud.
const fs = require("fs");
const path = require("path");
const { execFileSync, spawn } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n2e-evidence";
const EXE = "C:/Users/gwart/AppData/Local/Atomic Dream Engine/atomic-dream-engine.exe";
const FFMPEG_BIN = path.resolve("backend/vendor/ffmpeg/bin");
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
const apiRaw = async (method, p, body) => {
  const r = await fetch(`${API}${p}`, {
    method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined });
  return r;
};
const commit = async (pid, type, payload) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "audit",
    expected_revision: proj.revision,
    idempotency_key: `n2e-${type}-${Date.now()}-${Math.random()}` });
};
const detail = (pid, tid) => api("GET", `/api/projects/${pid}/timelines/${tid}`);
const waitJob = async (pid, jobId, expectFail = false) => {
  for (let i = 0; i < 240; i++) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && j.status === "succeeded") return j;
    if (j && (j.status === "failed" || j.status === "cancelled")) {
      if (expectFail) return j;
      throw new Error("job failed: " + JSON.stringify(j).slice(0, 300));
    }
    await sleep(500);
  }
  throw new Error("job timeout");
};

/* ---- PCM helpers on decoded s16le stereo 48k ---- */
const decodePcm = (mp4, pcmPath) => {
  execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-v", "error", "-i", mp4, "-map", "0:a:0", "-f", "s16le",
    "-ac", "2", "-ar", "48000", pcmPath], { stdio: "ignore" });
  const buf = fs.readFileSync(pcmPath);
  const a = new Int16Array(buf.buffer, buf.byteOffset, buf.length / 2);
  return a;
};
const rms = (pcm, s0, s1) => {
  let sum = 0, n = 0;
  for (let i = 2 * s0; i < Math.min(2 * s1, pcm.length); i++) { const v = pcm[i] / 32768; sum += v * v; n++; }
  return n ? Math.sqrt(sum / n) : 0;
};
const mag = (pcm, freq, s0, s1, ch = 0) => {
  const w = 2 * Math.PI * freq / 48000, cw = 2 * Math.cos(w);
  let sp = 0, sp2 = 0;
  for (let i = s0; i < s1 && 2 * i + ch < pcm.length; i++) {
    const s_ = pcm[2 * i + ch] / 32768 + cw * sp - sp2;
    sp2 = sp; sp = s_;
  }
  const n = Math.max(1, s1 - s0);
  return 2 * Math.sqrt(Math.max(0, sp * sp + sp2 * sp2 - cw * sp * sp2)) / n;
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

async function attachWs() {
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
}

async function main() {
  await attachWs();
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

  fs.mkdirSync(`${OUT}\\media`, { recursive: true });
  const w440 = `${OUT}\\media\\tone440.wav`;
  const w880 = `${OUT}\\media\\tone880.wav`;
  // sine's default amplitude is 1/8 — scale to a healthy 0.5 so PCM
  // measurements have real headroom (levels verified below anyway).
  execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-f", "lavfi", "-i",
    "sine=frequency=440:duration=10", "-af", "volume=4.0",
    "-ar", "48000", "-ac", "2", w440], { stdio: "ignore" });
  execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-f", "lavfi", "-i",
    "sine=frequency=880:duration=10", "-af", "volume=4.0",
    "-ar", "48000", "-ac", "1", w880], { stdio: "ignore" });
  ok("wavs synthesized (440 stereo + 880 mono, amp ~0.5, 48k, 10s)");

  /* 1 — project, import both wavs, timeline + audio lane, clips */
  step("1) import audio + place two clips with handles");
  const p = await api("POST", "/api/projects", { name: `N2E-${Date.now()}` });
  for (const w of [w440, w880]) {
    const ra = await commit(p.id, "asset.import",
      { source_path: path.resolve(w), kind: "audio" });
    await waitJob(p.id, ra.job_id);
  }
  const assets = (await api("GET", `/api/projects/${p.id}/assets?limit=50`)).items;
  const a440 = assets.find((a) => a.name.includes("tone440"));
  const a880 = assets.find((a) => a.name.includes("tone880"));
  await commit(p.id, "timeline.create", { name: "MIX", fps_num: 24, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`)).find((t) => t.name === "MIX");
  await commit(p.id, "timeline.track.add", { timeline_id: tl.id, kind: "audio", name: "A1" });
  let d = await detail(p.id, tl.id);
  const a1 = d.tracks.find((t) => t.kind === "audio");
  // Clip A (440): [0,120f)=5s, tail handle 5s. Clip B (880): [120f,192f)=3s
  // with 1s head handle (in_frame 48000 of a 10s source).
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: a440.id, start_frames: 0 });
  d = await detail(p.id, tl.id);
  const clipA = d.clips[0];
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: clipA.id, out_frame: 240000 });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: a880.id, start_frames: 120 });
  d = await detail(p.id, tl.id);
  const clipB = d.clips.find((c) => c.id !== clipA.id);
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: clipB.id, in_frame: 48000, out_frame: 192000 });
  ok(`A 440Hz [0,120f) + B 880Hz [120f,192f) — handles both sides`);

  /* 2 — gain/fades/automation/crossfade committed */
  step("2) gains, fades, automation, crossfade committed");
  await commit(p.id, "timeline.clip.gain",
    { timeline_id: tl.id, clip_id: clipA.id, gain_db: -6.0 });
  await commit(p.id, "timeline.clip.fade",
    { timeline_id: tl.id, clip_id: clipA.id,
      fade_in_units: 12000, fade_in_curve: "equal_power" });
  await commit(p.id, "timeline.clip.automation",
    { timeline_id: tl.id, clip_id: clipB.id,
      points: [{ at_frames: 0, gain_db: -12 }, { at_frames: 71, gain_db: -12 }] });
  await commit(p.id, "timeline.crossfade.set",
    { timeline_id: tl.id, out_clip_id: clipA.id,
      in_clip_id: clipB.id, tl_start: 114, tl_frames: 12,
      curve: "equal_power" });
  d = await detail(p.id, tl.id);
  if (d.crossfades[0].satisfied !== true)
    throw new Error("xf not satisfied: " + JSON.stringify(d.crossfades));
  ok("clip −6dB, fade-in eqp, B automation −12dB flat, xf 114f+12f satisfied");

  /* 3 — export with mixed audio succeeds */
  step("3) timeline.export renders mixed audio — job succeeds");
  const jr = await commit(p.id, "timeline.export",
    { timeline_id: tl.id, profile: "720p" });
  const job = await waitJob(p.id, jr.job_id);
  const outA = (await api("GET", `/api/projects/${p.id}/assets?limit=50`))
    .items.find((a) => a.origin === "rendered");
  if (!outA) throw new Error("no rendered output asset");
  ok(`export succeeded → asset ${outA.id.slice(0, 8)}`);

  /* 4 — verify output: ffprobe + download + PCM measurement */
  step("4) output verified: aac 48k stereo + measured PCM");
  const dl = await fetch(`${API}/api/projects/${p.id}/assets/${outA.id}/file`,
    { headers: { "X-Atomic-Token": TOKEN } });
  if (!dl.ok) throw new Error("output download: " + dl.status);
  const mp4 = `${OUT}\\media\\n2e-export.mp4`;
  fs.writeFileSync(mp4, Buffer.from(await dl.arrayBuffer()));
  const probe = JSON.parse(execFileSync(`${FFMPEG_BIN}\\ffprobe.exe`, [
    "-v", "error", "-print_format", "json", "-show_streams", mp4]).toString());
  const aStream = probe.streams.find((s) => s.codec_type === "audio");
  if (!aStream || aStream.codec_name !== "aac" || +aStream.sample_rate !== 48000)
    throw new Error(`audio stream wrong: ${JSON.stringify(aStream).slice(0, 200)}`);
  const pcm = decodePcm(mp4, `${OUT}\\media\\n2e-export.pcm`);
  // Program ≈ 8 s (192f@24). Windows (48k samples): 440 only pre-seam,
  // 880 only post-window; both present during [114f,126f) xf window.
  const m440pre = mag(pcm, 440, 40000, 100 * 2000);
  const m880post = mag(pcm, 880, 140 * 2000, 180 * 2000);
  const m440win = mag(pcm, 440, 115 * 2000, 120 * 2000);
  const m880win = mag(pcm, 880, 121 * 2000, 125 * 2000);
  const winRms = rms(pcm, 115 * 2000, 124 * 2000);
  // A −6 dB vs B −12 dB → m440 : m880 ≈ 2 : 1.
  const levelRatio = m440pre / Math.max(1e-6, m880post);
  if (!(m440pre > 0.12 && m880post > 0.05 && winRms > 0.02))
    throw new Error(`pcm mix wrong: 440pre=${m440pre.toFixed(3)} 880post=${m880post.toFixed(3)} winRms=${winRms.toFixed(3)}`);
  if (!(levelRatio > 1.4 && levelRatio < 2.8))
    throw new Error(`gain ratio wrong: 440/880=${levelRatio.toFixed(3)}`);
  // fade-in: early RMS much lower than steady
  const fadeEarly = rms(pcm, 400, 9000), steady = rms(pcm, 40000, 80000);
  if (!(fadeEarly < steady * 0.6 && steady > 0.08))
    throw new Error(`fade-in not rendered: early=${fadeEarly.toFixed(3)} steady=${steady.toFixed(3)}`);
  ok(`440pre=${m440pre.toFixed(3)} 880post=${m880post.toFixed(3)} `
     + `win(440=${m440win.toFixed(3)},880=${m880win.toFixed(3)},rms=${winRms.toFixed(3)}) `
     + `fadeEarly=${fadeEarly.toFixed(3)}/${steady.toFixed(3)}`);

  /* 5 — detached crossfade fails the export closed */
  step("5) detached crossfade → export fails closed");
  await commit(p.id, "timeline.clip.move",
    { timeline_id: tl.id, clip_id: clipB.id, start_frames: 400 });
  d = await detail(p.id, tl.id);
  if (d.crossfades[0].satisfied !== false)
    throw new Error("move did not detach xf");
  const jr2 = await commit(p.id, "timeline.export",
    { timeline_id: tl.id, profile: "720p" });
  const j2 = await waitJob(p.id, jr2.job_id, true);
  if (j2.status !== "failed" || !/satisfied/i.test(j2.error || ""))
    throw new Error(`detached export not refused: ${JSON.stringify(j2).slice(0, 200)}`);
  ok(`refused: "${j2.error.slice(0, 80)}"`);

  /* 6 — export-mode UI copy: mixed audio + blocked reason */
  step("6) export UI reports audio mix + blocked reason");
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("MIX"));
    if (b) b.click(); return !!b; })()`);
  await sleep(2500);
  await mustClick("Export…", "button", 1200);
  const ui1 = await evalJs(`(() => ({
    panel: !!document.querySelector('[data-testid="export-panel"]'),
    blocked: (document.querySelector('[data-testid="export-blocked"]') || {}).textContent || "" }))()`);
  if (!ui1.panel || !/crossfade/i.test(ui1.blocked))
    throw new Error(`UI blocked reason missing: ${JSON.stringify(ui1)}`);
  ok(`panel shows blocked reason: "${ui1.blocked.slice(0, 70)}"`);
  // restore the window → blocked clears (reload so the UI refetches
  // the detail — the API-side commit doesn't push a UI refresh)
  await commit(p.id, "timeline.clip.move",
    { timeline_id: tl.id, clip_id: clipB.id, start_frames: 120 });
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("MIX"));
    if (b) b.click(); return !!b; })()`);
  await sleep(2500);
  await mustClick("Export…", "button", 1200);
  const ui2 = await evalJs(`(() => ({
    blocked: !!document.querySelector('[data-testid="export-blocked"]'),
    text: (document.querySelector('[data-testid="export-panel"]') || {}).textContent || "" }))()`);
  if (ui2.blocked || !/audio|mix/i.test(ui2.text))
    throw new Error(`UI audio copy missing: ${JSON.stringify(ui2).slice(0, 160)}`);
  ok("after re-attach: panel describes the audio mix, no blocked row");

  /* 7 — audio-only timeline renders (black canvas + mix) */
  step("7) audio-only timeline exports");
  await commit(p.id, "timeline.create", { name: "AUDIOONLY", fps_num: 24, fps_den: 1 });
  const tl2 = (await api("GET", `/api/projects/${p.id}/timelines`)).find((t) => t.name === "AUDIOONLY");
  await commit(p.id, "timeline.track.add", { timeline_id: tl2.id, kind: "audio", name: "A1" });
  let d2 = await detail(p.id, tl2.id);
  const a1b = d2.tracks.find((t) => t.kind === "audio");
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl2.id, track_id: a1b.id, asset_id: a440.id, start_frames: 0 });
  const jr3 = await commit(p.id, "timeline.export",
    { timeline_id: tl2.id, profile: "720p" });
  await waitJob(p.id, jr3.job_id);
  ok("audio-only export succeeded — black canvas + mix");

  /* 8 — restart persistence */
  step("8) restart persistence — timeline + xf + output asset survive");
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"]); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"]); } catch {}
  await sleep(3000);
  spawn(EXE, [], { detached: true, stdio: "ignore",
    env: { ...process.env,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:
        `--remote-debugging-port=${PORT}` } }).unref();
  let up = false;
  for (let i = 0; i < 45 && !up; i++) {
    await sleep(2000);
    try {
      const l = await fetch(`http://127.0.0.1:${PORT}/json`).then((r) => r.json());
      up = l.some((t) => t.type === "page");
    } catch {}
  }
  if (!up) throw new Error("relaunch: CDP never came up");
  await attachWs();
  await send("Network.enable");
  API = null; TOKEN = null;
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button,a")]
      .find(e => e.textContent.trim() === "Projects");
    if (b) b.click(); return !!b; })()`);
  await sleep(3500);
  if (!API || !TOKEN) { await send("Page.reload", { ignoreCache: true }); await sleep(6000); }
  if (!API || !TOKEN) throw new Error("relaunch: no backend creds");
  d = await detail(p.id, tl.id);
  const assets2 = (await api("GET", `/api/projects/${p.id}/assets?limit=50`)).items;
  if (d.crossfades.length !== 1 || d.crossfades[0].satisfied !== true
      || !assets2.some((a) => a.origin === "rendered"))
    throw new Error("restart lost xf/output state");
  ok("relaunched — xf satisfied + rendered asset intact");

  /* 9 — clean shutdown */
  step("9) shutdown - no orphan processes");
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"]); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"]); } catch {}
  await sleep(3000);
  const procs = execFileSync("tasklist").toString();
  const orphans = (procs.match(/atomic-(dream-engine|backend)\.exe/gi) || []).length;
  if (orphans > 0) throw new Error(`${orphans} orphan app processes`);
  ok("clean shutdown - zero orphan app processes");

  fs.writeFileSync(`${OUT}\\n2e-acceptance.log`, LOG.join("\n") + "\n");
  console.log(`\nPASS ${STEP}/9 - evidence in ${OUT}`);
}

main().catch((e) => {
  console.error("FAIL:", e.message || e);
  try { fs.writeFileSync(`${OUT}\\n2e-acceptance.log`, LOG.join("\n") + `\nFAIL ${e.message}\n`); } catch {}
  process.exit(1);
});
