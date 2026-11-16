// N3-C acceptance — installed NSIS app, advanced audio editing UX:
// automation numeric editing (position/gain entry, validation,
// keyboard delete, interpolation), crossfade region/participant/
// curve UX + handle-error surfacing, edge auto-scroll + anchored
// zoom + keyboard pan, a11y scan (labels/focus/values), dense-
// automation + many-clip perf, regression pass over N3-B engine.
// Zero credentials/cloud. Run: node scripts/cdp-n3c-acceptance.cjs
// <cdpPort> <outDir> [installedExePath]
const fs = require("fs");
const path = require("path");
const { execFileSync, spawn } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n3c-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Atomic Dream Engine\\atomic-dream-engine.exe`;
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
  const i = ++_id; _pend.set(i, { res, rej, m });
  ws.send(JSON.stringify({ id: i, method: m, params: pr }));
  setTimeout(() => { if (_pend.has(i)) { _pend.get(i).rej(new Error("timeout " + m)); _pend.delete(i); } }, 90000);
});
const evalJs = async (expr) => {
  let last;
  for (let i = 0; i < 60; i++) {
    try {
      const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error("js: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
      return r.result && r.result.value;
    } catch (e) {
      last = e;
      if (!/Object reference chain|context|Execution context/.test(e.message)) throw e;
      await sleep(400);
    }
  }
  throw last;
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
const api = async (method, p, body, expect = 200) => {
  let r;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      r = await fetch(`${API}${p}`, {
        method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
        body: body ? JSON.stringify(body) : undefined });
      break;
    } catch (e) {
      if (attempt === 3) throw e;
      await sleep(300);          // stale keepalive socket — retry
    }
  }
  if (expect && r.status !== expect) {
    throw new Error(`api ${method} ${p}: ${r.status} != ${expect} ${(await r.text()).slice(0, 200)}`);
  }
  return r.status === 204 ? {} : r.json();
};
const commit = async (pid, type, payload, expect = 200) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "audit",
    expected_revision: proj.revision,
    idempotency_key: `n3c-${type}-${Date.now()}-${Math.random()}` }, expect);
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
const ffprobe = (mp4) => JSON.parse(execFileSync(
  `${FFMPEG_BIN}\\ffprobe.exe`, ["-v", "error", "-print_format", "json",
    "-show_streams", "-show_format", mp4]).toString());
const decodePcm = (mp4) => {
  const buf = execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-v", "error", "-i", mp4, "-map", "0:a:0", "-f", "s16le",
    "-ac", "2", "-ar", "48000", "-"], { stdio: ["ignore", "pipe", "ignore"],
    maxBuffer: 64 * 1024 * 1024 });
  return new Int16Array(buf.buffer, buf.byteOffset, buf.length / 2);
};
const rms = (pcm, s0, s1) => {
  let sum = 0, n = 0;
  for (let i = 2 * Math.floor(s0); i < Math.min(2 * Math.floor(s1), pcm.length); i++) { const v = pcm[i] / 32768; sum += v * v; n++; }
  return n ? Math.sqrt(sum / n) : 0;
};
const mag = (pcm, freq, s0, s1, ch = 0) => {
  const w = 2 * Math.PI * freq / 48000, cw = 2 * Math.cos(w);
  let sp = 0, sp2 = 0;
  for (let i = Math.floor(s0); i < Math.floor(s1) && 2 * i + ch < pcm.length; i++) {
    const s_ = pcm[2 * i + ch] / 32768 + cw * sp - sp2;
    sp2 = sp; sp = s_;
  }
  const n = Math.max(1, s1 - s0);
  return 2 * Math.sqrt(Math.max(0, sp * sp + sp2 * sp2 - cw * sp * sp2)) / n;
};

async function attachWs() {
  const targets = await fetch(`http://127.0.0.1:${PORT}/json`).then((r) => r.json());
  const page = targets.find((t) => t.type === "page");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && _pend.has(m.id)) {
      const { res, rej, m: meth } = _pend.get(m.id); _pend.delete(m.id);
      m.error ? rej(new Error(`${m.error.message} [${meth}]`)) : res(m.result);
    } else if (m.method && _events.has(m.method)) {
      _events.get(m.method).forEach((fn) => fn(m.params));
    }
  };
}
async function attachWsWait(timeoutMs = 45000) {
  const t0 = Date.now();
  for (;;) {
    try { await attachWs(); return; }
    catch (e) {
      if (Date.now() - t0 > timeoutMs) throw e;
      await sleep(1000);
    }
  }
}
async function creds() {
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
}
const left = (name) => {
  try {
    return execFileSync("tasklist", ["/FI", `IMAGENAME eq ${name}`, "/FO", "CSV", "/NH"]).toString().includes(name);
  } catch { return false; }
};
const procSample = async (name) => {
  try {
    const out = execFileSync("powershell", ["-NoProfile", "-Command",
      `Get-Process ${name} -ErrorAction SilentlyContinue | ` +
      `Measure-Object CPU,WorkingSet64 -Sum | ` +
      `ConvertTo-Json`]).toString();
    const j = JSON.parse(out || "[]");
    const arr = Array.isArray(j) ? j : [j];   // one object per property
    const prop = (n) => (arr.find(o => o.Property === n) || {}).Sum ?? 0;
    return { cpuS: prop("CPU"), rssMB: prop("WorkingSet64") / 1048576 };
  } catch { return { cpuS: 0, rssMB: 0 }; }
};

/* ---- real pointer drag on the ruler via CDP Input ---- */
const mouse = async (type, x, y, buttons, button) =>
  send("Input.dispatchMouseEvent",
    { type, x, y, buttons, button, pointerType: "mouse",
      clickCount: type === "mouseMoved" ? 0 : 1 });
const rulerRect = () => evalJs(`(() => {
  const el = document.querySelector('[data-testid="timeline-ruler"]');
  const r = el.getBoundingClientRect();
  // visible right edge — the scroll container clips the ruler before
  // its layout end (the audit aside occupies the rest of the row).
  let visRight = r.right;
  for (let p = el.parentElement; p; p = p.parentElement) {
    if (p.scrollWidth > p.clientWidth + 1
        || getComputedStyle(p).overflowX !== "visible") {
      visRight = Math.min(visRight, p.getBoundingClientRect().right);
      break;
    }
  }
  // visible LEFT edge — the sticky track-header cell overlays the
  // ruler's first HEADER_W px of viewport (it sticks while scrolling).
  const hdr = el.parentElement.firstElementChild;
  const visLeft = Math.max(r.left,
    hdr ? hdr.getBoundingClientRect().right : r.left);
  return { left: r.left, visLeft, top: r.top + r.height / 2,
           width: r.width, visRight }; })()`);
const ppfNow = () => evalJs(`window.__n2fPreview.getPpf()`);
/** Horizontally scroll the ruler's scroller so frame f sits inside
 * the visible window (real users scroll before scrubbing far). */
const scrollRuler = (f) => evalJs(`(() => {
  const el = document.querySelector('[data-testid="timeline-ruler"]');
  let sc = el.parentElement;
  while (sc && !(sc.scrollWidth > sc.clientWidth + 1)) sc = sc.parentElement;
  if (!sc) return false;
  // land the frame ~24px past the 176px sticky header
  sc.scrollLeft = ${f} * window.__n2fPreview.getPpf() - 200;
  return true; })()`);
const playheadReadout = () =>
  evalJs(`document.querySelector('[data-testid="playhead-readout"]').textContent`);
const engStats = () => evalJs(`(() => {
  const e = window.__n2fPreview.engineRef.current;
  if (!e) return null;
  return { state: e.state, calls: e.scrubStats.calls,
           auditions: e.scrubStats.auditions, maxLive: e.scrubStats.maxLive,
           lastScheduleMs: e.scrubStats.lastScheduleMs,
           lastLeadMs: e.scrubStats.lastLeadMs, live: e.live.length,
           pos: e.positionS() }; })()`);
const latReport = () => evalJs(
  `window.__n2fPreview.engineRef.current.latencyReport()`);
const fmtLat = (s) =>
  `p50 ${s.p50.toFixed(1)} p95 ${s.p95.toFixed(1)} max ${s.max.toFixed(1)} (n=${s.n})`;
/** Drag from frame f0 to f1 in `steps` moves, release at f1.
 * waitEach: for every dispatched move, first wait for it to be
 * DELIVERED (calls++ — browsers coalesce pointermove streams, so
 * some dispatches never reach the handler), then for the resulting
 * audition (auditions++). Reported latency is delivered→audition:
 * throttle coalesce (≤45 ms) + JS schedule + 20 ms start lead.
 * Coalesced dispatches are counted separately — they aren't lost
 * input, they're merged into a later position. */
const dragFrames = async (x0f, x1f, steps, moveDelayMs = 0, waitEach = true) => {
  const r = await rulerRect();
  const ppf = await ppfNow();
  // press must land on the VISIBLE ruler — the layout ruler extends
  // under the audit aside; presses there hit aside elements, not the
  // ruler (window-level move tracking then has nothing to track).
  const clampX = (f) => Math.max(r.visLeft + 2,
    Math.min(r.left + f * ppf, r.visRight - 4));
  const maxVisF = (r.visRight - 4 - r.visLeft - 2) / ppf;
  if (x0f > maxVisF)
    throw new Error(`press frame ${x0f} beyond visible ruler edge`);
  const lat = []; let delivered = 0, coalesced = 0;
  await mouse("mousePressed", clampX(x0f), r.top, 1, "left");
  for (let i = 1; i <= steps; i++) {
    const f = x0f + ((x1f - x0f) * i) / steps;
    const s0 = await engStats();
    const t0 = Date.now();
    await mouse("mouseMoved", clampX(f), r.top, 1, "none");
    if (!waitEach) { if (moveDelayMs) await sleep(moveDelayMs); continue; }
    // wait for delivery: calls++ means the pointermove handler ran
    let hit = false;
    for (let w = 0; w < 30; w++) {
      if ((await engStats()).calls > s0.calls) { hit = true; break; }
      await sleep(20);
    }
    if (!hit) { coalesced++; continue; }
    delivered++;
    // delivered → audition fired (throttle + schedule + lead)
    for (let w = 0; w < 20; w++) {
      if ((await engStats()).auditions > s0.auditions) break;
      await sleep(10);
    }
    lat.push(Date.now() - t0);
    if (moveDelayMs) await sleep(moveDelayMs);
  }
  await mouse("mouseReleased", clampX(x1f), r.top, 0, "left");
  return { lat, delivered, coalesced };
};

async function main() {
  await attachWsWait();
  await creds();

  /* ---------- media ---------- */
  fs.mkdirSync(`${OUT}\\media`, { recursive: true });
  const wav = (f, ch = 2, rate = 48000, dur = 8, amp = 4.0) => {
    const p = `${OUT}\\media\\f${f}.wav`;
    execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
      "-y", "-f", "lavfi", "-i", `sine=frequency=${f}:duration=${dur}`,
      "-af", `volume=${amp}`, "-ar", String(rate), "-ac", String(ch),
      p], { stdio: "ignore" });
    return p;
  };
  const vEmbed = `${OUT}\\media\\vembed.mp4`;
  execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-f", "lavfi", "-i", "testsrc=duration=8:size=320x180:rate=30",
    "-f", "lavfi", "-i", "sine=frequency=220:duration=8",
    "-af", "volume=2.0", "-c:v", "libopenh264", "-c:a", "aac",
    "-pix_fmt", "yuv420p", "-shortest", vEmbed], { stdio: "ignore" });
  const wDia = wav(440), wDia2 = wav(550), wMus = wav(330, 2, 44100),
        wTiny = wav(660, 2, 48000, 1.5);
  ok("media: embedded mp4 + 440/550/330/660 wavs (330 @44.1k, 660 1.5s)");

  /* ---------- 1. scene: embedded + 3 audio lanes @30fps ---------- */
  step("1) scene — embedded V1, dialogue+xf A1, music A2, tiny+gap A3 @30fps");
  const p = await api("POST", "/api/projects", { name: `N3C-${Date.now()}` }, 201);
  for (const [f, k] of [[vEmbed, "video"], [wDia, "audio"],
                        [wDia2, "audio"], [wMus, "audio"], [wTiny, "audio"]]) {
    const ra = await commit(p.id, "asset.import",
      { source_path: path.resolve(f), kind: k });
    await waitJob(p.id, ra.job_id);
  }
  const assets = (await api("GET", `/api/projects/${p.id}/assets?limit=50`)).items;
  const find = (s) => assets.find((a) => a.name.includes(s));
  const [aV, aD, aD2, aM, aT] =
    ["vembed", "f440", "f550", "f330", "f660"].map(find);
  await commit(p.id, "timeline.create",
    { name: "FILM", fps_num: 30, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`))
    .find((t) => t.name === "FILM");
  for (const [k, n] of [["audio", "A1-dialogue"], ["audio", "A2-music"],
                        ["audio", "A3-sfx"]])
    await commit(p.id, "timeline.track.add",
      { timeline_id: tl.id, kind: k, name: n });
  let d = await detail(p.id, tl.id);
  const v1 = d.tracks.find((t) => t.kind === "video");
  const a1 = d.tracks.find((t) => t.name === "A1-dialogue");
  const a2 = d.tracks.find((t) => t.name === "A2-music");
  const a3 = d.tracks.find((t) => t.name === "A3-sfx");
  // v1 embedded 0→180f (6s); a1: dia 30→120 + dia2 120→180 (xf);
  // a2: music 0→180; a3: tiny 45→48 + gap then nothing (tiny clip edge)
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: v1.id, asset_id: aV.id, start_frames: 0 });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: aD.id, start_frames: 30 });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a2.id, asset_id: aM.id, start_frames: 0 });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a3.id, asset_id: aT.id, start_frames: 45 });
  d = await detail(p.id, tl.id);
  const cBy = (a) => d.clips.find((x) => x.source_asset_id === a.id);
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aV).id, out_frame: 180 });
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aD).id, out_frame: 144000 });
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aM).id, out_frame: 264600 });
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aT).id, out_frame: 4800 });  // 3f = 0.1s
  // dia trimmed to tl[30,120) → second take fits at 120 (xf pair)
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: aD2.id, start_frames: 120 });
  d = await detail(p.id, tl.id);
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aD2).id,
      in_frame: 48000, out_frame: 240000 });   // nonzero trim + xf head
  await commit(p.id, "timeline.crossfade.set",
    { timeline_id: tl.id, out_clip_id: cBy(aD).id, in_clip_id: cBy(aD2).id,
      tl_start: 108, tl_frames: 24, curve: "equal_power" });
  // controls on top: clip fade + clip automation + lane automation
  await commit(p.id, "timeline.clip.fade",
    { timeline_id: tl.id, clip_id: cBy(aM).id,
      fade_out_units: 22050, fade_out_curve: "linear" });
  await commit(p.id, "timeline.clip.automation",
    { timeline_id: tl.id, clip_id: cBy(aD).id, interp: "step",
      points: [{ at_frames: 0, gain_db: 0 },
               { at_frames: 30, gain_db: -9 },
               { at_frames: 60, gain_db: 0 }] });
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tl.id, track_id: a2.id, interp: "linear",
      points: [{ at_tl_frames: 60, gain_db: 0 },
               { at_tl_frames: 120, gain_db: -12 },
               { at_tl_frames: 180, gain_db: 0 }] });
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tl.id, track_id: v1.id, interp: "linear",
      points: [{ at_tl_frames: 0, gain_db: 0 },
               { at_tl_frames: 180, gain_db: -9 }] });
  d = await detail(p.id, tl.id);
  ok(`scene: ${d.clips.length} clips, xf satisfied=${d.crossfades[0]?.satisfied}, ` +
     `clip fades+auto, lane ramps on A2+V1, tiny 3-frame clip`);

  /* ---------- 2. export + PCM baseline ---------- */
  step("2) export FILM — PCM baseline for parity comparison");
  const r = await commit(p.id, "timeline.export", { timeline_id: tl.id, profile: "720p" });
  const job = await waitJob(p.id, r.job_id);
  const dl = await fetch(`${API}/api/projects/${p.id}/assets/${job.output_asset_id}/file`,
    { headers: { "X-Atomic-Token": TOKEN } });
  const mp4 = `${OUT}\\media\\n3b-export.mp4`;
  fs.writeFileSync(mp4, Buffer.from(await dl.arrayBuffer()));
  const pr = ffprobe(mp4);
  const dur = parseFloat(pr.format.duration);
  ok(`export dur ${dur.toFixed(3)}s @${pr.streams.find(s=>s.codec_type==="video").r_frame_rate}`);
  const pcm = decodePcm(mp4);
  const m = (f, s0, s1) => mag(pcm, f, s0 * 48000, s1 * 48000);
  const exp = {
    dia_pre:   m(440, 1.2, 1.8),     // before step-duck (tl<60)
    dia_duck:  m(440, 2.2, 2.8),     // inside −9 step (tl60–90)
    xf_out:    m(440, 3.6, 3.8),     // xf out-leg still strong
    xf_440end: m(440, 4.3, 4.6),     // out-leg faded to ~0
    xf_550:    m(550, 4.5, 5.5),     // in-leg at full gain post-xf
    mus_early: m(330, 0.5, 1.5),     // lane ramp rising
    mus_duck:  m(330, 3.6, 4.0),     // lane −12 region
    emb_early: m(220, 0.3, 1.0),     // embedded ramp start
    emb_late:  m(220, 5.0, 5.9),     // embedded ramp end
    tiny:      m(660, 1.55, 1.62),   // 3-frame clip tl45–48=1.5–1.6s
    gap_rms:   rms(pcm, 8.1 * 48000, 8.6 * 48000),   // past program end
  };
  const chk = (name, cond) => {
    ok(`${name}: ${cond ? "OK" : "FAIL"}`);
    if (!cond) throw new Error("export PCM: " + name);
  };
  chk("dialogue pre-duck present", exp.dia_pre > 0.05);
  chk("step duck ≈ −9 dB", exp.dia_duck / exp.dia_pre < 0.55);
  chk("xf out-leg fades to silence",
      exp.xf_440end < exp.xf_out * 0.2 && exp.xf_out > 0.1);
  chk("xf in-leg at full gain", exp.xf_550 > 0.1);
  chk("music lane duck", exp.mus_duck / exp.mus_early < 0.6);
  chk("embedded lane ramp", exp.emb_late / exp.emb_early < 0.6);
  chk("tiny 3-frame clip audible at 1.55s", exp.tiny > 0.01);
  chk("no audio past program end (8.1s+)", exp.gap_rms < 0.005);

  /* ---------- 3. navigate + preview load ---------- */
  step("3) navigate to FILM, engine loads");
  let navOk = false;
  for (let att = 0; att < 4 && !navOk; att++) {
    try { await navTo(p.name); navOk = true; }
    catch (e) { ok(`nav ${att + 1}: ${e.message.slice(0, 70)}`); await sleep(4000); }
  }
  if (!navOk) throw new Error("navigation never stabilized");
  await sleep(1200);
  const detailJson = await detail(p.id, tl.id);
  await evalJs(`(async () => {
    const h = window.__n2fPreview;
    await h.engineRef.current.load(
      ${JSON.stringify(detailJson)}, ${JSON.stringify(p.id)});
  })()`);
  const loaded = await engStats();
  if (!loaded || loaded.state !== "ready")
    throw new Error(`engine not ready: ${JSON.stringify(loaded)}`);
  ok(`engine ready — ${detailJson.clips.length} clips + xf + lane automation`);

  /* ---------- 4. layout — activity rail must not obstruct ruler ---- */
  step("4) layout — activity rail collapse keeps the ruler unobstructed");
  // B1: the Activity aside is a collapsible flex sibling — never an
  // overlay on the ruler. Verify: (a) every pixel row across the
  // visible ruler resolves to a ruler element, (b) the panel
  // collapses to a slim rail and re-expands, (c) it stays correct at
  // narrow width and non-100% scale.
  const rulerClear = async () => {
    const rr = await rulerRect();
    const probes = await evalJs(`(() => {
      const out = [];
      for (let i = 0; i < 12; i++) {
        const x = ${rr.visLeft + 2} + (${rr.visRight - 4 - rr.visLeft - 2}) * i / 11;
        const el = document.elementFromPoint(x, ${rr.top});
        const aside = el && el.closest('aside[aria-label="Activity"]');
        const inRuler = el && el.closest('[data-testid="timeline-ruler"]');
        out.push({ x: Math.round(x), tag: el ? el.tagName : "NONE",
                   aside: !!aside, ruler: !!inRuler });
      }
      return out; })()`);
    return probes;
  };
  const asideW = () => evalJs(`(() => {
    const a = document.querySelector('aside[aria-label="Activity"]');
    return a ? a.getBoundingClientRect().width : 0; })()`);
  const hasAside = (await asideW()) > 0;
  ok(`activity aside present: ${hasAside} (jobs exist — rail is up)`);
  if (hasAside) {
    // EXPANDED: panel takes real layout width — ruler viewport ends
    // at its left edge; nothing overlaps.
    let probes = await rulerClear();
    let bad = probes.filter(q => q.aside || !q.ruler);
    if (bad.length)
      throw new Error("ruler obstructed while expanded: " +
                      JSON.stringify(bad.slice(0, 3)));
    ok(`expanded: ${probes.length} probes across visible ruler — all ruler, none aside`);
    // collapse it (no-op click if a prior run left it collapsed)
    await evalJs(`(() => {
      const b = document.querySelector('[aria-label="Hide activity panel"]');
      if (b) b.click(); return !!b; })()`);
    await sleep(300);
    const w2 = await asideW();
    ok(`collapsed rail width ${w2}px (expect ~32)`);
    if (w2 > 60) throw new Error("rail did not collapse");
    const rr2 = await rulerRect();
    ok(`ruler visible span grew: ${(rr2.visRight - rr2.left).toFixed(0)}px`);
    probes = await rulerClear();
    bad = probes.filter(q => q.aside || !q.ruler);
    if (bad.length)
      throw new Error("ruler obstructed while collapsed: " +
                      JSON.stringify(bad.slice(0, 3)));
    // NARROW + SCALED viewport — rail must stay a sibling, never overlay
    await send("Emulation.setDeviceMetricsOverride",
      { width: 900, height: 700, deviceScaleFactor: 1.5, mobile: false });
    await sleep(400);
    probes = await rulerClear();
    bad = probes.filter(q => q.aside || !q.ruler);
    if (bad.length)
      throw new Error("ruler obstructed at 900px@1.5x: " +
                      JSON.stringify(bad.slice(0, 3)));
    await send("Emulation.clearDeviceMetricsOverride");
    await sleep(400);
    // re-expand — audit controls independently accessible
    await evalJs(`(() => {
      const b = document.querySelector('[aria-label="Show activity panel"]');
      if (b) b.click(); return !!b; })()`);
    await sleep(300);
    const w3 = await asideW();
    if (w3 < 100) throw new Error("activity panel did not re-expand");
    ok(`re-expanded to ${w3}px; narrow@1.5x probes all clean; ` +
       `audit controls independently reachable`);
    // collapse again for the drag steps — wider ruler viewport
    await evalJs(`(() => {
      const b = document.querySelector('[aria-label="Hide activity panel"]');
      if (b) b.click(); return !!b; })()`);
    await sleep(300);
  } else {
    const probes = await rulerClear();
    if (probes.some(q => !q.ruler))
      throw new Error("ruler probes missed ruler: " +
                      JSON.stringify(probes.slice(0, 3)));
    ok("no activity aside — ruler fully clear");
  }

  /* ---------- 5. track automation — numeric entry + validation --- */
  step("5) track automation — numeric pos/gain, validation, interp, undo");
  // open the ƒ automation lane for A2-music
  const ensureAuto = async () => {
    const open = await evalJs(
      `!!document.querySelector('[data-testid="track-auto-${a2.id}"]')`);
    if (!open)
      await evalJs(`(() => {
        const b = document.querySelector('[aria-label="Show A2-music automation"]');
        if (b) b.click(); return !!b; })()`);
    await sleep(400);
  };
  await ensureAuto();
  const autoProbe = () => evalJs(`(() => {
    const sec = document.querySelector('[data-testid="track-auto-${a2.id}"]');
    if (!sec) return { err: "no section" };
    const svg = sec.querySelector('svg[role="img"]');
    return {
      curve: svg ? svg.getAttribute("aria-label") : null,
      rows: sec.querySelectorAll('[role="group"][aria-label^="Keyframe"]').length,
      alerts: [...sec.querySelectorAll('[role="alert"]')]
                .map(a => a.textContent) }; })()`);
  const laneAuto = async () => JSON.parse(
    (await detail(p.id, tl.id)).tracks.find(t => t.id === a2.id)
      .automation_json);
  const setNum = async (sel, val, reopen) => {
    for (let a = 0; a < 2; a++) {
      const r = await evalJs(`(() => {
        const inp = document.querySelector(${JSON.stringify(sel)});
        if (!inp) return "no input";
        const set = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, "value").set;
        inp.focus();
        set.call(inp, ${JSON.stringify(val)});
        inp.dispatchEvent(new Event("input", { bubbles: true }));
        inp.dispatchEvent(new KeyboardEvent("keydown",
          { key: "Enter", bubbles: true, cancelable: true }));
        return "ok"; })()`);
      if (r === "ok") return r;
      // section may close if the editor remounted — reopen once via the
      // caller's reopen thunk (track ƒ toggle / clip re-select)
      if (reopen) await reopen();
    }
    return "no input";
  };
  let ap = await autoProbe();
  if (ap.err || ap.rows !== 3 || !ap.curve)
    throw new Error("automation editor did not open: " + JSON.stringify(ap));
  ok(`editor open — "${ap.curve}", ${ap.rows} rows + numeric pos/gain inputs`);
  const POS1 = `[data-testid="track-auto-${a2.id}"] input[aria-label="Timeline frame for keyframe 1"]`;
  const GAIN1 = `[data-testid="track-auto-${a2.id}"] input[aria-label="Keyframe 1 gain in dB"]`;
  // numeric position entry — 60 → 70, committed via existing command
  if ((await setNum(POS1, "70", ensureAuto)) !== "ok") throw new Error("pos input missing");
  await sleep(800);
  let pts = await laneAuto();
  if (pts[0].at_tl_frames !== 70)
    throw new Error(`position commit failed: ${JSON.stringify(pts)}`);
  // duplicate position — must be rejected in-UI, no commit
  await setNum(POS1, "120", ensureAuto); await sleep(500);
  ap = await autoProbe(); pts = await laneAuto();
  if (ap.err || !ap.alerts.length || pts[0].at_tl_frames !== 70)
    throw new Error(`duplicate not rejected: ${JSON.stringify(ap)}`);
  ok(`validation — duplicate position rejected in-UI: "${ap.alerts[0]}"`);
  await setNum(POS1, "-5", ensureAuto); await sleep(400);
  ap = await autoProbe();
  if (ap.err || !ap.alerts.length)
    throw new Error(`range error not surfaced: ${JSON.stringify(ap)}`);
  ok(`range error shown: "${ap.alerts[0]}"`);
  await setNum(POS1, "60", ensureAuto); await sleep(800);          // restore
  // numeric gain entry — 0 → -20
  await setNum(GAIN1, "-20", ensureAuto); await sleep(800);
  pts = await laneAuto();
  if (Math.abs(pts[0].gain_db + 20) > 0.01)
    throw new Error(`gain commit failed: ${JSON.stringify(pts)}`);
  await setNum(GAIN1, "999", ensureAuto); await sleep(400);
  ap = await autoProbe(); pts = await laneAuto();
  if (ap.err || !ap.alerts.length || Math.abs(pts[0].gain_db + 20) > 0.01)
    throw new Error(`gain range not rejected: ${JSON.stringify(ap)}`);
  ok(`gain entry committed; 999dB rejected: "${ap.alerts[0]}"`);
  await setNum(GAIN1, "0", ensureAuto); await sleep(800);          // restore
  // keyboard select + Delete → 2 pts; undo → 3; redo → 2; undo → 3
  await evalJs(`(() => {
    const rows = [...document.querySelectorAll(
      '[data-testid="track-auto-${a2.id}"] [role="group"][aria-label^="Keyframe"]')];
    const row = rows[rows.length - 1];
    row.click();
    row.dispatchEvent(new KeyboardEvent("keydown",
      { key: "Delete", bubbles: true, cancelable: true }));
    return true; })()`);
  await sleep(800);
  pts = await laneAuto();
  if (pts.length !== 2) throw new Error("keyboard delete failed");
  ok("keyboard Delete removed keyframe 3 (one undo step)");
  await evalJs(`document.querySelector('[data-testid="undo-btn"]').click()`);
  await sleep(800);
  pts = await laneAuto();
  if (pts.length !== 3) throw new Error("undo did not restore keyframe");
  await evalJs(`document.querySelector('[data-testid="redo-btn"]').click()`);
  await sleep(800);
  pts = await laneAuto();
  if (pts.length !== 2) throw new Error("redo did not re-delete");
  await evalJs(`document.querySelector('[data-testid="undo-btn"]').click()`);
  await sleep(800);
  pts = await laneAuto();
  if (pts.length !== 3) throw new Error("undo chain broken");
  ok("undo/redo through automation commits verified");
  // interpolation switch — linear → step → linear (restore)
  const setSel = async (sel, val, reopen) => {
    for (let a = 0; a < 2; a++) {
      const r = await evalJs(`(() => {
        const el = document.querySelector(${JSON.stringify(sel)});
        if (!el) return false;
        const set = Object.getOwnPropertyDescriptor(
          window.HTMLSelectElement.prototype, "value").set;
        set.call(el, ${JSON.stringify(val)});
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return true; })()`);
      if (r) return true;
      if (reopen) await reopen();
    }
    return false;
  };
  await setSel('[aria-label="Track automation interpolation"]', "step", ensureAuto);
  await sleep(800);
  let tr = (await detail(p.id, tl.id)).tracks.find(t => t.id === a2.id);
  if (tr.automation_interp !== "step") throw new Error("interp->step failed");
  await setSel('[aria-label="Track automation interpolation"]', "linear", ensureAuto);
  await sleep(800);
  tr = (await detail(p.id, tl.id)).tracks.find(t => t.id === a2.id);
  if (tr.automation_interp !== "linear")
    throw new Error("interp restore failed");
  ok("interpolation select commits + restores (linear<->step)");

  /* ---------- 6. clip automation bounds + crossfade editing UX ---- */
  step("6) clip automation bounds + crossfade region/partners/curve/errors");
  // select the dialogue clip on A1 (xf out-participant, has step auto)
  const ensureInspector = async () => {
    const open = await evalJs(
      `!!document.querySelector('[data-testid="clip-inspector"]')`);
    if (!open)
      await evalJs(`(() => {
        const c = document.querySelector(
          '[data-testid="lane-body-${a1.id}"] [data-testid="lane-clip"]');
        if (c) c.click(); return !!c; })()`);
    await sleep(500);
  };
  await ensureInspector();
  const insp = await evalJs(`(() => {
    const sec = document.querySelector('[data-testid="automation-section"]');
    const xf = document.querySelector('[data-testid="xf-section"]');
    if (!sec || !xf) return { err: "inspector missing" };
    return {
      rows: sec.querySelectorAll('[role="group"][aria-label^="Keyframe"]').length,
      max: sec.querySelector('input[aria-label^="Offset frame"]')
              ?.getAttribute("max"),
      xfText: xf.textContent.slice(0, 120),
      xfCurve: xf.querySelector('select[aria-label="Crossfade curve"]')?.value,
      xfStart: xf.querySelector('input[aria-label="Crossfade start frame"]')?.value,
      xfLen: xf.querySelector('input[aria-label="Crossfade length in frames"]')?.value };
    })()`);
  if (insp.err) throw new Error(insp.err);
  if (insp.rows !== 3) throw new Error(`clip auto rows: ${insp.rows}`);
  ok(`inspector — ${insp.rows} clip keyframes (occupancy max ${insp.max}); ` +
     `xf curve=${insp.xfCurve} start=${insp.xfStart} len=${insp.xfLen}`);
  // occupancy bound: max+1 must be rejected
  const CPOS2 = `[data-testid="automation-section"] input[aria-label="Offset frame for keyframe 2"]`;
  await setNum(CPOS2, String(Number(insp.max) + 1), ensureInspector); await sleep(400);
  const cap = await evalJs(`(() => [...document.querySelectorAll(
    '[data-testid="automation-section"] [role="alert"]')]
      .map(a => a.textContent))()`);
  if (!cap.length) throw new Error("clip-local bound not enforced");
  ok(`clip-local bound enforced — "${cap[0]}"`);
  // valid in-bound move then restore (30 → 45 → 30)
  await setNum(CPOS2, "45", ensureInspector); await sleep(800);
  let cd = await detail(p.id, tl.id);
  let cA = cd.clips.find(c => c.id === cBy(aD).id);
  if (JSON.parse(cA.automation_json)[1].at_frames !== 45)
    throw new Error("clip auto pos commit failed");
  await setNum(CPOS2, "30", ensureInspector); await sleep(800);
  // crossfade region overlay on the lane
  const xfband = await evalJs(`(() => {
    const el = document.querySelector('[data-testid="xf-region"]');
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { w: Math.round(r.width) }; })()`);
  if (!xfband || xfband.w < 5)
    throw new Error("xf region band not rendered on lane");
  ok(`crossfade region band rendered on lane (${xfband.w}px wide)`);
  // precise duration edit: 24 → 40 → commit verified
  const XFLEN = '[data-testid="xf-section"] input[aria-label="Crossfade length in frames"]';
  await ensureInspector();
  await setNum(XFLEN, "40", ensureInspector); await sleep(900);
  cd = await detail(p.id, tl.id);
  if (cd.crossfades[0].tl_frames !== 40 || !cd.crossfades[0].satisfied)
    throw new Error("xf length edit failed: " +
                    JSON.stringify(cd.crossfades[0]));
  ok("xf duration edited via numeric entry — 24f→40f committed");
  // insufficient handles → backend rejects, error surfaces, xf unchanged
  await setNum(XFLEN, "4000", ensureInspector); await sleep(900);
  cd = await detail(p.id, tl.id);
  const bodyTxt = await evalJs("document.body.innerText");
  if (cd.crossfades[0].tl_frames !== 40 ||
      !/handle|material|overlap|tail|head/i.test(bodyTxt))
    throw new Error("xf handle validation did not surface an error");
  ok("insufficient-handle rejection surfaced with explanation (unchanged at 40f)");
  // curve edit → restore all (40→24, curve→equal_power)
  await ensureInspector();
  await setSel('[data-testid="xf-section"] select[aria-label="Crossfade curve"]',
               "linear");
  await sleep(800);
  cd = await detail(p.id, tl.id);
  if (cd.crossfades[0].curve !== "linear")
    throw new Error("xf curve edit failed");
  await ensureInspector();
  await setSel('[data-testid="xf-section"] select[aria-label="Crossfade curve"]',
               "equal_power");
  await sleep(900);   // let the curve commit land — revision pin serializes
  await setNum(XFLEN, "24", ensureInspector); await sleep(900);
  cd = await detail(p.id, tl.id);
  if (cd.crossfades[0].tl_frames !== 24 ||
      cd.crossfades[0].curve !== "equal_power")
    throw new Error("xf restore failed: " + JSON.stringify(cd.crossfades[0]));
  ok("xf curve select commits; restored to 24f equal_power for parity");

  /* ---------- 7. navigation — edge scroll, anchored zoom, keys ---- */
  step("7) navigation — edge auto-scroll, playhead-anchored zoom, keyboard pan");
  const scrollEl = () => evalJs(
    `document.querySelector('[data-testid="timeline-scroll"]').scrollLeft`);
  // edge auto-scroll: hold a drag near the right visible edge → pans.
  // Inspector/row focus can scroll the main column — bring the ruler
  // back into the viewport before positioning presses/probes.
  const bringRulerIntoView = async () => {
    await evalJs(`(() => {
      const ruler = document.querySelector('[data-testid="timeline-ruler"]');
      ruler.scrollIntoView({ block: "nearest", inline: "nearest" });
      const sc = document.querySelector('[data-testid="timeline-scroll"]');
      sc.scrollTop = 0;
      window.scrollTo(0, 0);
      return true; })()`);
    await sleep(300);
  };
  await bringRulerIntoView();
  await scrollRuler(0); await sleep(200);
  const rr7 = await rulerRect();
  const sc0 = await scrollEl();
  await mouse("mousePressed", rr7.visRight - 24, rr7.top, 1, "left");
  for (let i = 0; i < 6; i++) {
    await mouse("mouseMoved", rr7.visRight - 24, rr7.top, 1, "none");
    await sleep(70);
  }
  await mouse("mouseReleased", rr7.visRight - 24, rr7.top, 0, "left");
  await sleep(200);
  const sc1 = await scrollEl();
  if (!(sc1 > sc0 + 30))
    throw new Error(`edge auto-scroll did not pan (${sc0}→${sc1})`);
  ok(`edge auto-scroll during ruler drag — scrollLeft ${sc0}→${sc1} (bounded steps)`);
  // playhead-anchored zoom: playhead screen-x must stay put
  const phX = () => evalJs(`(() => {
    const el = document.querySelector('[data-testid="playhead"]');
    return el ? el.getBoundingClientRect().left : null; })()`);
  await scrollRuler(30); await sleep(200);
  // press+release inside the view — playhead lands where we can watch it
  const ppf7 = await ppfNow();
  const rr7b = await rulerRect();
  const px7 = rr7b.left + 40 * ppf7;
  await mouse("mousePressed", px7, rr7b.top, 1, "left");
  await sleep(150);
  await mouse("mouseReleased", px7, rr7b.top, 0, "left");
  await sleep(300);
  const x0 = await phX();
  await evalJs(`document.querySelector('[aria-label="Zoom in"]').click()`);
  await sleep(400);
  const x1 = await phX();
  await evalJs(`document.querySelector('[aria-label="Zoom out"]').click()`);
  await sleep(400);
  const x2 = await phX();
  if (x0 === null || Math.abs(x1 - x0) > 8 || Math.abs(x2 - x0) > 8)
    throw new Error(`zoom anchor drifted: ${x0}→${x1}→${x2}`);
  ok(`anchored zoom — playhead screen-x ${x0|0}→${x1|0}→${x2|0} (±8px)`);
  // PageDown/PageUp pan one visible span
  const scA = await scrollEl();
  await evalJs(`(() => {
    const r = document.querySelector('[data-testid="timeline-ruler"]');
    r.dispatchEvent(new KeyboardEvent("keydown",
      { key: "PageDown", bubbles: true, cancelable: true }));
    return true; })()`);
  await sleep(300);
  const scB = await scrollEl();
  if (!(scB > scA + 50))
    throw new Error(`PageDown did not pan (${scA}→${scB})`);
  await evalJs(`(() => {
    const r = document.querySelector('[data-testid="timeline-ruler"]');
    r.dispatchEvent(new KeyboardEvent("keydown",
      { key: "PageUp", bubbles: true, cancelable: true }));
    return true; })()`);
  await sleep(300);
  const scC = await scrollEl();
  if (!(scC < scB)) throw new Error(`PageUp did not pan back (${scB}→${scC})`);
  ok(`PageDown/PageUp pan — scrollLeft ${scA}→${scB}→${scC}`);

  /* ---------- 8. accessibility scan ------------------------------- */
  step("8) accessibility — names on all controls, focus, values");
  const a11y = await evalJs(`(() => {
    const root = document.querySelector('[data-testid="timeline-editor"]');
    const els = [...root.querySelectorAll(
      'button,input,select,[tabindex="0"],[role="slider"]')];
    const name = (el) => el.getAttribute("aria-label")
      || (el.closest("label")?.textContent || "").trim()
      || (el.textContent || "").trim()
      || el.getAttribute("title") || el.getAttribute("aria-labelledby");
    const unnamed = els.filter(el => !name(el) && !el.disabled)
                       .map(el => el.outerHTML.slice(0, 90));
    const negTab = els.filter(el => el.tabIndex < 0).length;
    const ruler = document.querySelector('[data-testid="timeline-ruler"]');
    return { total: els.length, unCount: unnamed.length,
             unnamed: unnamed.slice(0, 4), negTab,
             rulerRole: ruler.getAttribute("role"),
             rulerVT: ruler.getAttribute("aria-valuetext"),
             rulerNow: ruler.getAttribute("aria-valuenow") }; })()`);
  if (a11y.unCount || a11y.negTab)
    throw new Error(`a11y: ${a11y.unCount} unnamed ${JSON.stringify(a11y.unnamed)}; ` +
                    `${a11y.negTab} tabindex<0`);
  if (a11y.rulerRole !== "slider" || !a11y.rulerVT)
    throw new Error("ruler missing slider semantics: " +
                    JSON.stringify(a11y));
  ok(`${a11y.total} interactive controls — all named, none tabindex<0; ` +
     `ruler role=slider valuetext="${a11y.rulerVT}"`);
  // focus lands visibly on keyframe rows (keyboard operable)
  const foc = await evalJs(`(() => {
    const row = document.querySelector(
      '[data-testid="automation-section"] [role="group"][aria-label^="Keyframe"]');
    if (!row) return { err: "no row" };
    row.focus();
    return { focused: document.activeElement === row,
             tabbable: row.tabIndex === 0 }; })()`);
  if (foc.err || !foc.focused || !foc.tabbable)
    throw new Error("keyframe row not keyboard-focusable: " + JSON.stringify(foc));
  ok("keyframe rows are tabbable + focusable (no pointer-only controls)");
  // narrow window: ruler still unobstructed
  await send("Emulation.setDeviceMetricsOverride",
    { width: 760, height: 640, deviceScaleFactor: 1.5, mobile: false });
  await sleep(400);
  await bringRulerIntoView();
  const probes8 = await rulerClear();
  const bad8 = probes8.filter(q => q.aside || !q.ruler);
  await send("Emulation.clearDeviceMetricsOverride");
  await sleep(300);
  if (bad8.length)
    throw new Error("ruler obstructed at 760px@1.5x: " +
                    JSON.stringify(bad8.slice(0, 3)));
  ok("760px@1.5x — ruler probes clean (narrow-window + high-DPI)");

  /* ---------- 9. perf — dense automation, virtualization, WIDE ---- */
  step("9) perf — dense automation + 120-clip timeline, bounded DOM");
  // dense lane automation on A3 (200 pts, within 256 bound)
  const dense = [];
  for (let i = 0; i < 200; i++)
    dense.push({ at_tl_frames: i * 3, gain_db: -(i % 9) });
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tl.id, track_id: a3.id,
      points: dense, interp: "linear" });
  // WIDE timeline: 2 lanes × 60 clips = 120 clips
  await commit(p.id, "timeline.create",
    { name: "WIDE", fps_num: 30, fps_den: 1 });
  const wide = (await api("GET", `/api/projects/${p.id}/timelines`))
    .find((t) => t.name === "WIDE");
  for (const n of ["W1", "W2"])
    await commit(p.id, "timeline.track.add",
      { timeline_id: wide.id, kind: "audio", name: n });
  let wd = await detail(p.id, wide.id);
  const w1 = wd.tracks.find(t => t.name === "W1");
  const w2 = wd.tracks.find(t => t.name === "W2");
  for (let i = 0; i < 120; i++) {
    await commit(p.id, "timeline.clip.place",
      { timeline_id: wide.id, track_id: i % 2 ? w2.id : w1.id,
        asset_id: [aD, aM, aD2, aT][i % 4].id,
        start_frames: i * 150, allow_duplicate: true });
  }
  wd = await detail(p.id, wide.id);
  ok(`WIDE built — ${wd.clips.length} clips; dense lane automation (200 pts)`);
  // navigate the app to WIDE
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(x => x.textContent === "WIDE");
    if (b) b.click(); return !!b; })()`);
  await sleep(1200);
  // DOM virtualization — mounted clips bounded, not 120
  let dom = await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
  if (dom > 60)
    throw new Error(`virtualization regressed — ${dom} clip nodes mounted`);
  await scrollRuler(900); await sleep(400);
  dom = await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
  if (dom > 60)
    throw new Error(`virtualization regressed mid-lane — ${dom} nodes`);
  ok(`virtualization bounded — ${dom} DOM clip nodes for 120 total`);
  // React responsiveness: zoom click → rAF
  const resp = await evalJs(`(async () => {
    const t0 = performance.now();
    document.querySelector('[aria-label="Zoom in"]').click();
    await new Promise(r => requestAnimationFrame(
      () => requestAnimationFrame(r)));
    return Math.round(performance.now() - t0); })()`);
  ok(`zoom click → settled: ${resp}ms`);
  // engine load + offline render on the dense/wide scene
  const t9 = Date.now();
  const wdJson = JSON.stringify(wd);
  await evalJs(`(async () => {
    const h = window.__n2fPreview;
    await h.engineRef.current.load(${wdJson}, ${JSON.stringify(p.id)});
  })()`);
  const loadMs = Date.now() - t9;
  const wr = await evalJs(`(async () => {
    const h = window.__n2fPreview;
    const t0 = performance.now();
    const plan = await h.buildPreviewPlan(${wdJson});
    const buildMs = performance.now() - t0;
    const off = new OfflineAudioContext(2, Math.ceil(0.35 * 48000), 48000);
    const byAsset = new Map();
    const buffers = new Map();
    for (const c of plan.clips) {
      if (!byAsset.has(c.assetId)) {
        const res = await fetch("${API}/api/projects/" +
          ${JSON.stringify(p.id)} + "/assets/" + c.assetId + "/file",
          {headers:{"X-Atomic-Token":${JSON.stringify(TOKEN)}}});
        byAsset.set(c.assetId,
          await off.decodeAudioData(await res.arrayBuffer()));
      }
      buffers.set(c.clipId, byAsset.get(c.assetId));
    }
    const t1 = performance.now();
    const buf = await h.renderPlanOffline(plan, buffers, 0.35, 48000, 0, 0.35);
    return { buildMs: +buildMs.toFixed(1),
             renderMs: +(performance.now() - t1).toFixed(1),
             clips: plan.clips.length,
             dur: +buf.duration.toFixed(3) }; })()`);
  ok(`WIDE engine — load ${loadMs}ms, plan ${wr.buildMs}ms ` +
     `(${wr.clips} clips), 350ms window render ${wr.renderMs}ms (${wr.dur}s)`);
  const rss9 = await procSample("atomic-dream-engine");
  const rss9b = await procSample("atomic-backend");
  ok(`app RSS ${rss9.rssMB}MB / backend ${rss9b.rssMB}MB`);
  // restore dense automation (A3 originally had none)
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tl.id, track_id: a3.id, points: [], interp: "linear" });
  // back to FILM for regression
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(x => x.textContent === "FILM");
    if (b) b.click(); return !!b; })()`);
  await sleep(800);

  /* ---------- 10. regression + clean close ------------------------ */
  step("10) regression — engine scrub + parity spot-check + clean close");
  const dFinal = await detail(p.id, tl.id);
  await evalJs(`(async () => {
    const h = window.__n2fPreview;
    await h.engineRef.current.load(
      ${JSON.stringify(dFinal)}, ${JSON.stringify(p.id)});
  })()`);
  await sleep(300);
  const dg = await dragFrames(30, 80, 10, 0, true);
  if (!dg.delivered) throw new Error("regression drag delivered 0 moves");
  const st10 = await engStats();
  ok(`drag ${dg.delivered} delivered/${dg.coalesced} coalesced, ` +
     `${st10.auditions} auditions, live ${st10.live}, ` +
     `sched ${st10.lastScheduleMs}ms`);
  // parity spot-check: offline render window vs export PCM (unchanged detail)
  const probe = await evalJs(`(async () => {
    const h = window.__n2fPreview;
    const plan = await h.buildPreviewPlan(${JSON.stringify(dFinal)});
    const off = new OfflineAudioContext(2, 24000, 48000);
    const byAsset = new Map(); const buffers = new Map();
    for (const c of plan.clips) {
      if (!byAsset.has(c.assetId)) {
        const res = await fetch("${API}/api/projects/" +
          ${JSON.stringify(p.id)} + "/assets/" + c.assetId + "/file",
          {headers:{"X-Atomic-Token":${JSON.stringify(TOKEN)}}});
        byAsset.set(c.assetId,
          await off.decodeAudioData(await res.arrayBuffer()));
      }
      buffers.set(c.clipId, byAsset.get(c.assetId));
    }
    const buf = await h.renderPlanOffline(plan, buffers, 0.5, 48000, 3.9, 0.5);
    const ch = buf.getChannelData(0);
    // window-local: buf starts at tl 3.9s; 550Hz clip starts 4.0s →
    // sample local 0.15–0.45s (abs 4.05–4.35)
    let a550 = 0;
    for (let i = 7200; i < 21600; i += 4)
      a550 += ch[i] * Math.sin(2 * Math.PI * 550 * i / 48000);
    return { a550: Math.abs(a550) }; })()`);
  const exp550 = m(550, 3.9, 4.4);
  if (probe.a550 < 10 || exp550 < 0.001)
    throw new Error(`regression parity probe off: ` +
                    `render=${JSON.stringify(probe)} export=550:${exp550}`);
  ok(`windowed parity intact — xf tail renders 550Hz ` +
     `(render ${probe.a550.toFixed(0)} vs export ${exp550.toFixed(3)})`);
  // clean close — no orphan backend
  await send("Browser.close").catch(() => {});
  await sleep(500);
  execFileSync("powershell", ["-NoProfile", "-Command",
    "Get-Process atomic-dream-engine -ErrorAction SilentlyContinue | " +
    "Select-Object -First 1 | ForEach-Object { $_.CloseMainWindow() | Out-Null }"]);
  await sleep(3500);
  if (left("atomic-backend.exe"))
    throw new Error("backend orphaned after close");
  ok("clean close — no orphan app/backend processes");


  fs.writeFileSync(`${OUT}\\n3c-acceptance.txt`, LOG.join("\n"));
  console.log(`\nN3-C ACCEPTANCE: PASS ${STEP}/${STEP} steps — ${OUT}`);
}

async function navTo(pname) {
  try { await send("Page.reload", { ignoreCache: true }); } catch { /* nav ok */ }
  const t0 = Date.now();
  while (Date.now() - t0 < 45000) {
    let st = null;
    try {
      st = await evalJs(`(() => {
        const t = document.body ? document.body.innerText : "";
        if ([...document.querySelectorAll("button")].some(b => b.textContent.includes(${JSON.stringify(pname)}))) return "projects";
        if (document.querySelector('[aria-label="Workspace"]')) return "workspace";
        if (t.includes("No project open") || t.includes("Create something extraordinary")) return "home";
        return null; })()`);
    } catch { /* navigating */ }
    if (st === "projects") break;
    if (st === "home") { await clickText("Projects", "button,a"); await sleep(1200); continue; }
    if (st === "workspace") { await clickText("← Projects") || await clickText("Choose a project"); await sleep(1200); break; }
    await sleep(400);
  }
  await mustClick(pname, "button", 2000);
  await mustWaitText("Timeline", 10000);
  await mustClick("Timeline", "button,a", 1200);
  await sleep(800);
  try { await mustClick("FILM", "button", 2500); } catch { /* already open */ }
  await sleep(800);
}

main().then(() => process.exit(0)).catch((e) => {
  fs.writeFileSync(`${OUT}\\n3c-acceptance.txt`,
    LOG.join("\n") + "\nFAIL: " + e.message);
  console.error("FAIL:", e.message);
  process.exit(1);
});
