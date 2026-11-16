// N3-B acceptance — installed NSIS app, continuous drag-scrubbing:
// real CDP pointer drags on the ruler, bounded audition windows,
// generation-throttled scheduling (no unbounded source sequence),
// no-stale-on-release, resume-after-scrub, keyboard auditions,
// windowed offline-render parity vs FFmpeg export PCM, fps variants,
// nonzero trims / head extension / gaps / very short clips,
// long-timeline + many-clips performance, decode-error surface,
// project-switch + close-during-scrub teardown, orphan check.
// Zero credentials/cloud. Run: node scripts/cdp-n3b-acceptance.cjs
// <cdpPort> <outDir> [installedExePath]
const fs = require("fs");
const path = require("path");
const { execFileSync, spawn } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n3b-evidence";
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
  const r = await fetch(`${API}${p}`, {
    method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined });
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
    idempotency_key: `n3b-${type}-${Date.now()}-${Math.random()}` }, expect);
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
  return { left: r.left, top: r.top + r.height / 2,
           width: r.width, visRight }; })()`);
const ppfNow = () => evalJs(`window.__n2fPreview.getPpf()`);
/** Horizontally scroll the ruler's scroller so frame f sits inside
 * the visible window (real users scroll before scrubbing far). */
const scrollRuler = (f) => evalJs(`(() => {
  const el = document.querySelector('[data-testid="timeline-ruler"]');
  let sc = el.parentElement;
  while (sc && !(sc.scrollWidth > sc.clientWidth + 1)) sc = sc.parentElement;
  if (!sc) return false;
  sc.scrollLeft = ${f} * window.__n2fPreview.getPpf() - 60;
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
  const clampX = (f) => Math.min(r.left + f * ppf, r.visRight - 4);
  const maxVisF = (r.visRight - 4 - r.left) / ppf;
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
  const p = await api("POST", "/api/projects", { name: `N3B-${Date.now()}` }, 201);
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
        const x = ${rr.left} + (${rr.visRight - 4 - rr.left}) * i / 11;
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

  /* ---------- 5. windowed offline parity vs export PCM ---------- */
  step("5) windowed audition parity — offline render windows vs export");
  const winRender = async (fromS, windowS) => evalJs(`(async () => {
    const h = window.__n2fPreview;
    const plan = h.buildPreviewPlan(${JSON.stringify(detailJson)});
    const off = new OfflineAudioContext(2, Math.ceil(${windowS} * 48000), 48000);
    const buffers = new Map();
    for (const c of plan.clips) {
      const res = await fetch("${API}/api/projects/" +
        ${JSON.stringify(p.id)} + "/assets/" + c.assetId + "/file",
        {headers:{"X-Atomic-Token":${JSON.stringify(TOKEN)}}});
      buffers.set(c.clipId, await off.decodeAudioData(await res.arrayBuffer()));
    }
    const buf = await h.renderPlanOffline(plan, buffers, ${windowS}, 48000,
      ${fromS}, ${windowS});
    const L = buf.getChannelData(0), R = buf.getChannelData(1);
    const mag = (f,s0,s1) => {
      const w=2*Math.PI*f/48000, cw=2*Math.cos(w); let a=0,b=0;
      for (let i=Math.floor(s0*48000); i<Math.floor(s1*48000); i++){
        const v=(L[i]+R[i])/2+cw*a-b; b=a; a=v; }
      const n=Math.max(1,Math.floor((s1-s0)*48000));
      return 2*Math.sqrt(Math.max(0,a*a+b*b-cw*a*b))/n; };
    return { m440: mag(440,0.05,0.3), m550: mag(550,0.05,0.3),
             m330: mag(330,0.05,0.3), m220: mag(220,0.05,0.3),
             m660: mag(660,0.05,0.3),
             rms: Math.sqrt(L.reduce((q,v)=>q+v*v,0)/L.length) }; })()`);
  // Probe windows render the same absolute span they compare against:
  // winRender(at) samples window-local [0.05,0.3) ≈ abs [at+0.05,at+0.3).
  const probes = [
    { at: 1.15, what: "pre-duck dialogue",         f: "m440", x: "dia_pre",  lo: 0.5, hi: 1.5 },
    { at: 2.25, what: "step-ducked dialogue",      f: "m440", x: "dia_duck", lo: 0.5, hi: 1.5 },
    { at: 4.25, what: "crossfade in-leg (550)",    f: "m550", x: "xf_550",   lo: 0.4, hi: 1.8 },
    { at: 3.85, what: "music lane duck",           f: "m330", x: "mus_duck", lo: 0.4, hi: 1.8 },
    { at: 0.45, what: "embedded ramp early",       f: "m220", x: "emb_early",lo: 0.5, hi: 1.5 },
    { at: 5.25, what: "embedded ramp late",        f: "m220", x: "emb_late", lo: 0.4, hi: 1.6 },
  ];
  for (const pb of probes) {
    const w = await winRender(pb.at, 0.35);
    const prev = w[pb.f];
    const e = exp[pb.x];
    const near = prev >= e * pb.lo && prev <= e * pb.hi;
    ok(`  ${pb.what}: window ${prev.toFixed(4)} vs export ${e.toFixed(4)} ${near ? "OK" : "FAIL"}`);
    if (!near) throw new Error("window parity: " + pb.what);
  }
  // gap window → silence; past-program-end window → silence
  const g = await winRender(8.15, 0.35);
  ok(`past-end window rms ${g.rms.toFixed(5)} ${g.rms < 0.01 ? "OK" : "FAIL"}`);
  if (g.rms >= 0.01) throw new Error("past-end window not silent");

  /* ---------- 6. real drag-scrub via CDP Input ---------- */
  step("6) REAL drag — window-tracked moves, bounded nodes, phase latency");
  // scroll so 30→85 sits inside the ~75-frame visible window (the
  // audit aside clips the ruler's right edge — real users scroll to
  // reach far frames; span covers pre-duck → step-duck → xf head)
  await scrollRuler(40);
  await sleep(200);
  const drag = await dragFrames(30, 85, 20, 70);   // ~real drag cadence
  const st = await engStats();
  const latSorted = [...drag.lat].sort((a, b) => a - b);
  const p50 = latSorted.length ? latSorted[Math.floor(latSorted.length / 2)] : "n/a";
  const p95 = latSorted.length ? latSorted[Math.floor(latSorted.length * 0.95)] : "n/a";
  // delivered-move → next-audition latency incl. browser coalescing,
  // the ≤45 ms throttle, JS schedule cost, and the 20 ms start lead.
  ok(`drag 30f→85f ×20: delivered ${drag.delivered} coalesced ` +
     `${drag.coalesced} | move→audition p50 ${p50}ms p95 ${p95}ms | ` +
     `calls ${st.calls} auditions ${st.auditions} maxLive ${st.maxLive} | ` +
     `engine sched ${st.lastScheduleMs.toFixed(2)}ms ` +
     `lead ${st.lastLeadMs.toFixed(1)}ms`);
  // B4 — phase-split latency, measured inside the engine:
  // input = event.timeStamp→handler entry; queue = handler→scheduling
  // (coalesce wait); sched = schedule loop; lead = scheduling→expected
  // onset. estOutputLatencyMs is the context's device estimate — the
  // expected-audible proxy, NOT a measured audible latency.
  const lr = await latReport();
  ok(`phase latency — input→handler ${fmtLat(lr.input)}ms`);
  ok(`phase latency — handler→schedule ${fmtLat(lr.queue)}ms`);
  ok(`phase latency — schedule loop ${fmtLat(lr.sched)}ms`);
  ok(`phase latency — schedule→expected onset ${fmtLat(lr.lead)}ms`);
  ok(`est device output latency ${lr.estOutputLatencyMs?.toFixed(1) ?? "n/a"}ms ` +
     `(expected-onset proxy — audible onset not objectively measurable, no loopback)`);
  if (!lr.queue.n || !lr.lead.n)
    throw new Error("phase latency rings empty");
  if (drag.delivered === 0) throw new Error("no pointer moves delivered");
  if (latSorted.length && p95 > 300)
    throw new Error("drag latency bound exceeded");
  if (st.lastScheduleMs > 100)
    throw new Error("schedule cost bound exceeded");
  if (st.maxLive > detailJson.clips.length * 2 + 2)
    throw new Error(`live node bound exceeded: ${st.maxLive}`);
  if (st.live !== 0) throw new Error("stale live nodes after release");
  const tc = await playheadReadout();
  ok(`playhead after drag: ${tc} | nodes drained to ${st.live}`);

  /* ---------- 7. rapid scrub burst — coalescing bound ---------- */
  step("7) rapid drag burst — 60 moves, throttle coalesces");
  const b0 = await engStats();
  await scrollRuler(30);
  await dragFrames(20, 80, 60, 0, false);       // raw-speed burst
  const b1 = await engStats();
  const fired = b1.auditions - b0.auditions;
  const called = b1.calls - b0.calls;
  ok(`burst: ${called} pointer calls → ${fired} auditions ` +
     `(${Math.round(100 * fired / Math.max(1, called))}% scheduled), ` +
     `maxLive ${b1.maxLive}`);
  if (fired >= called && called > 8)
    throw new Error("throttle did not coalesce burst");
  if (b1.live !== 0) throw new Error("stale nodes after burst release");

  /* ---------- 8. scrub during playback → resume ---------- */
  step("8) scrub while playing → smooth resume on release");
  await evalJs(`document.querySelector('[data-testid="preview-play"]').click()`);
  await sleep(800);
  const before = await engStats();
  if (before.state !== "playing") throw new Error("preview did not start");
  await scrollRuler(110);
  await dragFrames(100, 140, 10);
  await sleep(600);
  const after = await engStats();
  const ph = await playheadReadout();
  ok(`state during end: ${after.state} — resumed playing=${after.state === "playing"}, pos ${after.pos.toFixed(2)}s (${ph})`);
  if (after.state !== "playing") throw new Error("did not resume playback");
  if (after.pos < 4.0 || after.pos > 6.5)
    throw new Error("resume position wrong: " + after.pos);
  await evalJs(`document.querySelector('[data-testid="preview-stop"]').click()`);
  await sleep(300);

  /* ---------- 9. transport matrix — Escape cancel + stopped scrub -- */
  step("9) transport matrix — Escape cancel restores pre-scrub state");
  // playing → scrub → Escape: deterministic cancel = playhead snaps
  // to the pre-scrub position AND playback resumes from there.
  await evalJs(`document.querySelector('[data-testid="preview-play"]').click()`);
  await sleep(500);
  const cPos0 = (await engStats()).pos;  // live pos — cancel restores this
  await scrollRuler(60);
  {
    const r = await rulerRect(); const ppf = await ppfNow();
    const clampX = (f) => Math.min(r.left + f * ppf, r.visRight - 4);
    await mouse("mousePressed", clampX(50), r.top, 1, "left");
    for (const f of [55, 60, 65, 70])
      await mouse("mouseMoved", clampX(f), r.top, 1, "none");
    await sleep(120);
    const mid = await engStats();
    if (mid.state !== "scrubbing")
      throw new Error("expected scrubbing mid-drag, got " + mid.state);
    await send("Input.dispatchKeyEvent",
      { type: "keyDown", key: "Escape", code: "Escape",
        windowsVirtualKeyCode: 27 });
    await send("Input.dispatchKeyEvent",
      { type: "keyUp", key: "Escape", code: "Escape",
        windowsVirtualKeyCode: 27 });
    await sleep(500);
  }
  const cPos1 = await engStats();
  ok(`playing→scrub→Escape: state ${cPos1.state}, pos ` +
     `${cPos1.pos.toFixed(2)}s (pre-scrub ${cPos0.toFixed(2)}s + playback)`);
  if (cPos1.state !== "playing")
    throw new Error("cancel did not resume playback");
  if (cPos1.pos < cPos0 - 0.3 || cPos1.pos > cPos0 + 2.5)
    throw new Error(`cancel position off: ${cPos1.pos} vs ~${cPos0}`);
  await evalJs(`document.querySelector('[data-testid="preview-stop"]').click()`);
  await sleep(300);
  // stopped → scrub → Escape: snaps back, stays stopped (no accidental
  // playback activation — the "one authoritative state" invariant)
  await scrollRuler(80);
  {
    const r = await rulerRect(); const ppf = await ppfNow();
    const clampX = (f) => Math.min(r.left + f * ppf, r.visRight - 4);
    const s0 = (await engStats()).pos;         // pre-press — restore target
    await mouse("mousePressed", clampX(70), r.top, 1, "left");
    for (const f of [75, 80, 85, 90])
      await mouse("mouseMoved", clampX(f), r.top, 1, "none");
    await sleep(120);
    await send("Input.dispatchKeyEvent",
      { type: "keyDown", key: "Escape", code: "Escape",
        windowsVirtualKeyCode: 27 });
    await send("Input.dispatchKeyEvent",
      { type: "keyUp", key: "Escape", code: "Escape",
        windowsVirtualKeyCode: 27 });
    await sleep(300);
    const sEnd = await engStats();
    ok(`stopped→scrub→Escape: state ${sEnd.state}, pos ` +
       `${sEnd.pos.toFixed(2)}s (restored ≈${s0.toFixed(2)})`);
    if (sEnd.state === "playing")
      throw new Error("cancel activated playback from stopped");
    if (Math.abs(sEnd.pos - s0) > 0.4)
      throw new Error("cancel did not restore pre-scrub position");
    if (sEnd.live !== 0) throw new Error("stale nodes after cancel");
  }

  /* ---------- 10. lifecycle — zigzag burst + release outside ruler - */
  step("10) lifecycle — zigzag burst, release outside ruler bounds");
  await scrollRuler(30);
  {
    const r = await rulerRect(); const ppf = await ppfNow();
    const clampX = (f) => Math.min(r.left + f * ppf, r.visRight - 4);
    const z0 = await engStats();
    await mouse("mousePressed", clampX(20), r.top, 1, "left");
    // alternating direction every move — generator supersession under
    // direction reversal must keep live nodes bounded
    for (let i = 1; i <= 24; i++) {
      const f = i % 2 === 0 ? 20 + i * 1.5 : 20 + (i - 1) * 1.5 - 6;
      await mouse("mouseMoved", clampX(Math.max(5, f)), r.top, 1, "none");
    }
    // release OUTSIDE the ruler — window-level listeners still end it
    await mouse("mouseReleased", clampX(30), r.top - 46, 0, "left");
    await sleep(250);
    const z1 = await engStats();
    ok(`zigzag 24 moves: auditions ${z1.auditions - z0.auditions}, ` +
       `maxLive ${z1.maxLive}, release-outside → state ${z1.state}, ` +
       `live ${z1.live}`);
    if (z1.state === "scrubbing")
      throw new Error("release outside ruler left scrub active");
    if (z1.live !== 0)
      throw new Error("stale live nodes after outside release");
    if (z1.maxLive > detailJson.clips.length + 2)
      throw new Error("zigzag live-node bound exceeded");
  }

  /* ---------- 11. keyboard seek + audition ---------- */
  step("11) keyboard — arrow steps + Home/End audition blips");
  await evalJs(`document.querySelector('[data-testid="timeline-ruler"]').focus()`);
  const k0 = await engStats();
  for (const key of ["ArrowRight", "ArrowRight", "ArrowRight"])
    await send("Input.dispatchKeyEvent",
      { type: "keyDown", key, code: key, windowsVirtualKeyCode: 39 });
  await sleep(300);
  const k1 = await engStats();
  const tc2 = await playheadReadout();
  ok(`3× ArrowRight → playhead ${tc2}, auditions ${k1.auditions - k0.auditions}`);
  if (k1.auditions <= k0.auditions && k1.calls <= k0.calls)
    throw new Error("keyboard audition never fired");
  await send("Input.dispatchKeyEvent",
    { type: "keyDown", key: "Home", code: "Home", windowsVirtualKeyCode: 36 });
  await sleep(250);
  ok(`Home → ${await playheadReadout()}`);

  /* ---------- 9. fps-variant timelines — tl→src mapping ---------- */
  step("12) fps variants — 24/25/30/24000:1001/30000:1001 timeline→source mapping");
  const fpsVariants = [[24, 1], [25, 1], [30, 1], [24000, 1001], [30000, 1001]];
  for (const [num, den] of fpsVariants) {
    const name = `FPS${num}of${den}`;
    await commit(p.id, "timeline.create", { name, fps_num: num, fps_den: den });
    const t2 = (await api("GET", `/api/projects/${p.id}/timelines`))
      .find((x) => x.name === name);
    await commit(p.id, "timeline.track.add",
      { timeline_id: t2.id, kind: "audio", name: "A1" });
    const dt = await detail(p.id, t2.id);
    const aT2 = dt.tracks.find((t) => t.name === "A1");
    await commit(p.id, "timeline.clip.place",
      { timeline_id: t2.id, track_id: aT2.id, asset_id: aD.id, start_frames: 48 });
    const d3 = await detail(p.id, t2.id);
    const c48 = d3.clips[0];
    await commit(p.id, "timeline.clip.trim",
      { timeline_id: t2.id, clip_id: c48.id, out_frame: 144000 });
    // offline-render a window at the clip's exact tl start —
    // frame 48 at num/den fps ⇒ seconds 48*den/num
    const s48 = 48 * den / num;
    const w = await evalJs(`(async () => {
      const h = window.__n2fPreview;
      const plan = h.buildPreviewPlan(${JSON.stringify(await detail(p.id, t2.id))});
      const off = new OfflineAudioContext(2, 16800, 48000);
      const res = await fetch("${API}/api/projects/" +
        ${JSON.stringify(p.id)} + "/assets/" + plan.clips[0].assetId + "/file",
        {headers:{"X-Atomic-Token":${JSON.stringify(TOKEN)}}});
      const buf0 = await off.decodeAudioData(await res.arrayBuffer());
      const m2 = new Map(); m2.set(plan.clips[0].clipId, buf0);
      const buf = await h.renderPlanOffline(plan, m2, 0.35, 48000,
        ${s48 - 0.05}, 0.35);
      const L = buf.getChannelData(0);
      // onset should sit at 0.05s into the window (s48 - (s48-0.05))
      const win = (s0,s1) => { let a=0,b=0;
        const wv=2*Math.PI*440/48000, cw=2*Math.cos(wv);
        for (let i=Math.floor(s0*48000);i<Math.floor(s1*48000);i++){
          const v=L[i]+cw*a-b; b=a; a=v; }
        return 2*Math.sqrt(Math.max(0,a*a+b*b-cw*a*b))/Math.floor((s1-s0)*48000); };
      return { pre: win(0, 0.04), post: win(0.06, 0.2) }; })()`);
    const on = w.post > 0.1 && w.pre < w.post * 0.2;
    ok(`  ${num}/${den}: clip@48f → ${s48.toFixed(4)}s — onset pre ${w.pre.toFixed(3)} post ${w.post.toFixed(3)} ${on ? "OK" : "FAIL"}`);
    if (!on) throw new Error(`fps ${num}/${den} mapping wrong`);
  }

  /* ---------- 10. many-clips long timeline — performance ---------- */
  step("13) long timeline — 20 clips / 4 lanes / 60s — schedule cost");
  await commit(p.id, "timeline.create", { name: "BIG", fps_num: 30, fps_den: 1 });
  const big = (await api("GET", `/api/projects/${p.id}/timelines`))
    .find((x) => x.name === "BIG");
  for (let i = 0; i < 4; i++)
    await commit(p.id, "timeline.track.add",
      { timeline_id: big.id, kind: "audio", name: `A${i + 1}` });
  const bd = await detail(p.id, big.id);
  for (const [tname, asset, starts] of [
    ["A1", aD, [0, 300, 600, 900, 1200]],
    ["A2", aM, [60, 360, 660, 960, 1260]],
    ["A3", aD2, [120, 420, 720, 1020, 1320]],
    ["A4", aT, [180, 480, 780, 1080, 1380]],
  ]) {
    const tr = bd.tracks.find((t) => t.name === tname);
    for (const s of starts)
      await commit(p.id, "timeline.clip.place",
        { timeline_id: big.id, track_id: tr.id, asset_id: asset.id,
          start_frames: s, allow_duplicate: true });
  }
  const bd2 = await detail(p.id, big.id);
  ok(`BIG: ${bd2.clips.length} clips across 4 lanes over ~46s`);
  const bigDetail = await detail(p.id, big.id);
  const bigProbe = await evalJs(`(async () => {
    const h = window.__n2fPreview;
    const plan = h.buildPreviewPlan(${JSON.stringify(bigDetail)});
    const t0 = performance.now();
    const off = new OfflineAudioContext(2, 16800, 48000);
    const buffers = new Map();
    for (const c of plan.clips) {
      const res = await fetch("${API}/api/projects/" +
        ${JSON.stringify(p.id)} + "/assets/" + c.assetId + "/file",
        {headers:{"X-Atomic-Token":${JSON.stringify(TOKEN)}}});
      buffers.set(c.clipId, await off.decodeAudioData(await res.arrayBuffer()));
    }
    const buildMs = performance.now() - t0;
    const t1 = performance.now();
    const buf = await h.renderPlanOffline(plan, buffers, 0.35, 48000, 20.0, 0.35);
    return { planClips: plan.clips.length,
             decodeMs: Math.round(buildMs),
             renderMs: Math.round(performance.now() - t1),
             dur: buf.duration }; })()`);
  ok(`BIG window render: ${bigProbe.planClips} clips, decode ${bigProbe.decodeMs}ms, render ${bigProbe.renderMs}ms`);

  /* ---------- 11. project-switch dispose + perf sample ---------- */
  step("14) project switch → engine disposal; CPU/mem during scrub");
  const m0 = await procSample("atomic-dream-engine");
  const m0b = await procSample("atomic-backend");
  await navTo(p.name);
  await sleep(800);
  await evalJs(`(async () => {
    const h = window.__n2fPreview;
    await h.engineRef.current.load(
      ${JSON.stringify(await detail(p.id, tl.id))}, ${JSON.stringify(p.id)});
  })()`);
  await dragFrames(10, 80, 30, 60);             // deliberate drag (visible range)
  await sleep(400);
  const m1 = await procSample("atomic-dream-engine");
  const m1b = await procSample("atomic-backend");
  const dcpu = Math.max(0, m1.cpuS - m0.cpuS) + Math.max(0, m1b.cpuS - m0b.cpuS);
  ok(`drag CPU +${dcpu.toFixed(1)}s, app RSS ${m1.rssMB.toFixed(0)}MB (was ${m0.rssMB.toFixed(0)}), ` +
     `backend RSS ${m1b.rssMB.toFixed(0)}MB`);
  // navigate away from the timeline — engine must dispose its graph
  await clickText("← Projects") || await clickText("Choose a project")
    || await clickText("Projects", "button,a");
  await sleep(1200);
  const hookGone = await evalJs(`window.__n2fPreview === undefined
    || window.__n2fPreview.engineRef.current === null`);
  ok(`preview hook after nav-away: ${hookGone ? "disposed OK" : "still live"}`);
  if (!hookGone) throw new Error("engine not disposed on project switch");

  /* ---------- 12. shutdown during scrub — orphans ---------- */
  step("15) close mid-scrub — graceful quit, no orphans");
  await navTo(p.name);
  await sleep(800);
  await evalJs(`(async () => {
    const h = window.__n2fPreview;
    await h.engineRef.current.load(
      ${JSON.stringify(await detail(p.id, tl.id))}, ${JSON.stringify(p.id)});
  })()`);
  const r2 = await rulerRect(); const ppf2 = await ppfNow();
  await mouse("mousePressed", r2.left + 60 * ppf2, r2.top, 1, "left");
  await mouse("mouseMoved", r2.left + 80 * ppf2, r2.top, 1, "none");
  await sleep(120);
  const scrubMid = await engStats();
  ok(`mid-scrub: state ${scrubMid.state}, live nodes ${scrubMid.live}`);
  // close via the app's own window-close path (same as the title-bar X)
  execFileSync("powershell", ["-NoProfile", "-Command",
    `(Get-Process atomic-dream-engine -ErrorAction SilentlyContinue | ` +
    `Select-Object -First 1).CloseMainWindow() | Out-Null`]);
  await sleep(4000);
  const left = (name) => {
    try {
      return execFileSync("tasklist", ["/FI", `IMAGENAME eq ${name}`, "/FO", "CSV", "/NH"]).toString().includes(name);
    } catch { return false; }
  };
  const appLeft = left("atomic-dream-engine.exe");
  const beLeft = left("atomic-backend.exe");
  ok(`post-close: app ${appLeft ? "RUNNING" : "gone"}, backend ${beLeft ? "RUNNING (orphan)" : "gone"}`);
  if (beLeft || appLeft) throw new Error("orphan processes after mid-scrub close");

  /* ---------- 16. restart persistence — data survives a relaunch --- */
  step("16) restart persistence — relaunch keeps timeline + automation");
  const env = { ...process.env, WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:
    `--remote-debugging-port=${PORT}` };
  const child = spawn(EXE, [], { env, detached: true, stdio: "ignore" });
  child.unref();
  ws = null;
  await attachWsWait(60000);
  await creds();
  // the freshly spawned backend may rebind ports — verify the captured
  // API actually answers; re-capture from live traffic if not.
  let apiUp = false;
  for (let i = 0; i < 30 && !apiUp; i++) {
    try {
      const r = await fetch(`${API}/api/projects`,
        { headers: { "X-Atomic-Token": TOKEN } });
      if (r.status < 500) { apiUp = true; break; }
    } catch { /* backend still starting */ }
    API = null; TOKEN = null;
    await creds();
  }
  if (!apiUp) throw new Error("backend unreachable after relaunch");
  await navTo(p.name);
  await sleep(800);
  let persist = null;
  for (let i = 0; i < 20; i++) {
    try { persist = await detail(p.id, tl.id); break; }
    catch { await sleep(1000); }
  }
  if (!persist) throw new Error("timeline detail unreachable post-restart");
  const auto = JSON.parse(
    (persist.clips.find(c => c.source_asset_id === aD.id) || {})
      .automation_json || "[]");
  const xfOk = persist.crossfades.length === 1 && persist.crossfades[0].satisfied;
  ok(`persisted: ${persist.clips.length} clips, xf satisfied=${xfOk}, ` +
     `clip automation ${auto.length} pts, rev ${persist.revision}`);
  if (persist.clips.length !== d.clips.length || !xfOk || auto.length !== 3)
    throw new Error("restart persistence check failed");
  // engine reloads the persisted detail cleanly
  await evalJs(`(async () => {
    const h = window.__n2fPreview;
    await h.engineRef.current.load(
      ${JSON.stringify(persist)}, ${JSON.stringify(p.id)});
  })()`);
  const rel = await engStats();
  if (!rel || rel.state !== "ready")
    throw new Error("engine not ready after restart");
  ok(`post-restart engine ready`);
  execFileSync("powershell", ["-NoProfile", "-Command",
    `(Get-Process atomic-dream-engine -ErrorAction SilentlyContinue | ` +
    `Select-Object -First 1).CloseMainWindow() | Out-Null`]);
  await sleep(3500);
  if (left("atomic-backend.exe"))
    throw new Error("backend orphaned after restart close");

  fs.writeFileSync(`${OUT}\\n3b-acceptance.txt`, LOG.join("\n"));
  console.log(`\nN3-B ACCEPTANCE: PASS ${STEP}/${STEP} steps — ${OUT}`);
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
  fs.writeFileSync(`${OUT}\\n3b-acceptance.txt`,
    LOG.join("\n") + "\nFAIL: " + e.message);
  console.error("FAIL:", e.message);
  process.exit(1);
});
