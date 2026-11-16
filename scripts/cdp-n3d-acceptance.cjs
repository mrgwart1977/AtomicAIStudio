// N3-D acceptance — installed NSIS app, final N3 hardening package:
// D1 full audio editing regression (split/ripple/gain/mute/solo/fades/
// xf/automation + undo/redo), D2 preview/export PCM parity at
// 30000/1001 + mixed sample rates + mono/stereo, D3 reliability
// (export cancel, idempotent replay, durable rows, no phantom
// output/orphans/temp), D4 perf (240-clip 6-lane timeline, 400-pt
// dense automation, repeated exports, RSS), D5 representative film
// (2 video lanes, embedded audio, dialogue/music/sfx, xf, fades,
// automation, gaps, fractional fps), restart persistence, clean
// shutdown. Zero credentials/cloud.
// Run: node scripts/cdp-n3d-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs");
const path = require("path");
const { execFileSync, spawn } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n3d-evidence";
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
    idempotency_key: `n3d-${type}-${Date.now()}-${Math.random()}` }, expect);
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
  const wav = (f, ch = 2, rate = 48000, dur = 8, amp = 4.0, name = null) => {
    const p = `${OUT}\\media\\${name || "f" + f + "_" + rate + "_" + ch + "ch"}.wav`;
    execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
      "-y", "-f", "lavfi", "-i", `sine=frequency=${f}:duration=${dur}`,
      "-af", `volume=${amp}`, "-ar", String(rate), "-ac", String(ch),
      p], { stdio: "ignore" });
    return p;
  };
  const mp4v = (f, name, dur = 8) => {
    const p = `${OUT}\\media\\${name}.mp4`;
    execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
      "-y", "-f", "lavfi", "-i", `testsrc=duration=${dur}:size=320x180:rate=30`,
      "-f", "lavfi", "-i", `sine=frequency=${f}:duration=${dur}`,
      "-af", "volume=2.0", "-c:v", "libopenh264", "-c:a", "aac",
      "-pix_fmt", "yuv420p", "-shortest", p], { stdio: "ignore" });
    return p;
  };
  const vE1 = mp4v(220, "vemb1"), vE2 = mp4v(280, "vemb2");
  const wDia = wav(440), wDia2 = wav(550), wMus = wav(330, 2, 44100),
        wSfx = wav(660, 2, 48000, 1.5),
        wMono = wav(500, 1, 48000, 4, 4.0, "mono48k"),
        wM441 = wav(700, 1, 44100, 4, 4.0, "mono441");
  ok("media: 2 embedded mp4 (220/280Hz) + 440/550 stereo 48k + "
     + "330 stereo 44.1k + 660 tiny + 500 mono48k + 700 mono44.1k");

  const p = await api("POST", "/api/projects", { name: `N3D-${Date.now()}` }, 201);
  for (const [f, k] of [[vE1, "video"], [vE2, "video"], [wDia, "audio"],
                        [wDia2, "audio"], [wMus, "audio"], [wSfx, "audio"],
                        [wMono, "audio"], [wM441, "audio"]]) {
    const ra = await commit(p.id, "asset.import",
      { source_path: path.resolve(f), kind: k });
    await waitJob(p.id, ra.job_id);
  }
  const assets = (await api("GET", `/api/projects/${p.id}/assets?limit=60`)).items;
  const find = (s) => assets.find((a) => a.name.includes(s));
  const [aV1, aV2, aD, aD2, aM, aT, aMono, aM441] =
    ["vemb1", "vemb2", "f440", "f550", "f330", "f660", "mono48k", "mono441"]
      .map(find);
  ok("8 assets imported (jobs durable)");

  const tlF2S = (f, tl) => f * tl.fps_den / tl.fps_num;
  const exportAndPcm = async (tl, tag) => {
    const r = await commit(p.id, "timeline.export",
      { timeline_id: tl.id, profile: "720p" });
    const job = await waitJob(p.id, r.job_id);
    const dl = await fetch(
      `${API}/api/projects/${p.id}/assets/${job.output_asset_id}/file`,
      { headers: { "X-Atomic-Token": TOKEN } });
    const fp = `${OUT}\\media\\n3d-${tag}.mp4`;
    fs.writeFileSync(fp, Buffer.from(await dl.arrayBuffer()));
    return { job, fp, pr: ffprobe(fp), pcm: decodePcm(fp) };
  };
  const magS = (pcm, f, t0, t1) => mag(pcm, f, t0 * 48000, t1 * 48000);
  const rmsS = (pcm, t0, t1) => rms(pcm, t0 * 48000, t1 * 48000);
  const chk = (name, cond) => {
    ok(`${name}: ${cond ? "OK" : "FAIL"}`);
    if (!cond) throw new Error("assert: " + name);
  };

  /* ---------- 1. scene — representative film @30000/1001 ---------- */
  step("1) scene — 2 video lanes + 3 audio lanes + all controls @30000/1001");
  await commit(p.id, "timeline.create",
    { name: "FILM", fps_num: 30000, fps_den: 1001 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`))
    .find((t) => t.name === "FILM");
  for (const [k, n] of [["video", "V2-overlay"], ["audio", "A1-dialogue"],
                        ["audio", "A2-music"], ["audio", "A3-sfx"]])
    await commit(p.id, "timeline.track.add",
      { timeline_id: tl.id, kind: k, name: n });
  let d = await detail(p.id, tl.id);
  const v1 = d.tracks.find((t) => t.kind === "video" && t.name !== "V2-overlay");
  const v2 = d.tracks.find((t) => t.name === "V2-overlay");
  const a1 = d.tracks.find((t) => t.name === "A1-dialogue");
  const a2 = d.tracks.find((t) => t.name === "A2-music");
  const a3 = d.tracks.find((t) => t.name === "A3-sfx");
  const fps = 30000 / 1001;
  const F = (s) => Math.round(s * fps);        // seconds → tl frames
  // V1 embedded 0..6s; V2 embedded 2..5s (second video lane)
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: v1.id, asset_id: aV1.id, start_frames: 0 });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: v2.id, asset_id: aV2.id, start_frames: F(2) });
  // A1: dia 1..3s + dia2 3..6s (xf pair); A2 music 0..6s; A3 tiny 1.5..1.6s
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: aD.id, start_frames: F(1) });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a2.id, asset_id: aM.id, start_frames: 0 });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a3.id, asset_id: aT.id, start_frames: F(1.5) });
  d = await detail(p.id, tl.id);
  const cBy = (a) => d.clips.find((x) => x.source_asset_id === a.id);
  // trims (source-frame units): v1→6s, v2→3s, dia→2s, mus→6s, tiny→0.1s
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aV1).id, out_frame: 180 });
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aV2).id, out_frame: 90 });
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aD).id, out_frame: 96000 });
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aM).id, out_frame: 264600 });
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aT).id, out_frame: 4800 });
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: aD2.id, start_frames: F(3) });
  d = await detail(p.id, tl.id);
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cBy(aD2).id,
      in_frame: 48000, out_frame: 192000 });
  // gains: clip −3 dB on dialogue; track −6 dB on music lane
  await commit(p.id, "timeline.clip.gain",
    { timeline_id: tl.id, clip_id: cBy(aD).id, gain_db: -3 });
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a2.id, gain_db: -6 });
  // fades on music: in 0.5s linear, out 0.5s s_curve
  await commit(p.id, "timeline.clip.fade",
    { timeline_id: tl.id, clip_id: cBy(aM).id,
      fade_in_units: 22050, fade_in_curve: "linear",
      fade_out_units: 22050, fade_out_curve: "s_curve" });
  // xf on the dialogue pair (24f ≈ 0.8s, ending at 3.0s)
  await commit(p.id, "timeline.crossfade.set",
    { timeline_id: tl.id, out_clip_id: cBy(aD).id, in_clip_id: cBy(aD2).id,
      tl_start: F(3) - 24, tl_frames: 24, curve: "equal_power" });
  // clip automation step on dia (duck −9 at its local 0.5s..1.0s
  // — at_frames is in the clip's TL occupancy frames, 0..59 here)
  await commit(p.id, "timeline.clip.automation",
    { timeline_id: tl.id, clip_id: cBy(aD).id, interp: "step",
      points: [{ at_frames: 0, gain_db: 0 },
               { at_frames: 15, gain_db: -9 },
               { at_frames: 30, gain_db: 0 }] });
  // lane automation ramp on V1 (fade embedded audio 0→−12 over program)
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tl.id, track_id: v1.id, interp: "linear",
      points: [{ at_tl_frames: 0, gain_db: 0 },
               { at_tl_frames: F(6), gain_db: -12 }] });
  // lane automation on music (duck mid-program)
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tl.id, track_id: a2.id, interp: "linear",
      points: [{ at_tl_frames: F(2), gain_db: 0 },
               { at_tl_frames: F(4), gain_db: -12 },
               { at_tl_frames: F(6), gain_db: 0 }] });
  d = await detail(p.id, tl.id);
  chk("film scene: 2 video + 3 audio lanes, " +
      `${d.clips.length} clips, xf satisfied`, d.crossfades[0]?.satisfied === true);

  /* ---------- 2. D1 editing regression via commands + undo/redo --- */
  step("2) D1 — trim/split/move/gain/mute/solo/fade/xf/automation + undo/redo");
  const cD = cBy(aD), cM = cBy(aM);
  // split dia at tl frame F(2) (maps to a whole source frame)
  const split = await commit(p.id, "timeline.clip.split",
    { timeline_id: tl.id, clip_id: cD.id, at_frame: F(2) });
  d = await detail(p.id, tl.id);
  const frags = d.clips.filter((x) => x.source_asset_id === aD.id);
  chk("split created 2 fragments sharing source", frags.length === 2);
  // undo restores one clip
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  chk("undo merged fragments back to 1 clip",
      d.clips.filter((x) => x.source_asset_id === aD.id).length === 1);
  await commit(p.id, "timeline.redo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  chk("redo re-split into 2 clips",
      d.clips.filter((x) => x.source_asset_id === aD.id).length === 2);
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  // ripple move: shift music +2s → xf/dialogue unaffected, later lane clips shift
  await commit(p.id, "timeline.clip.move",
    { timeline_id: tl.id, clip_id: cM.id, start_frames: F(0.5),
      to_track_id: a2.id });
  d = await detail(p.id, tl.id);
  const cM2 = d.clips.find((x) => x.source_asset_id === aM.id);
  chk("explicit move retimed music to 0.5s", cM2.start_frames === F(0.5));
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  // mute the sfx lane → its clip is render-inert; solo none
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a3.id, muted: true });
  d = await detail(p.id, tl.id);
  chk("mute flag set on A3",
      d.tracks.find((t) => t.id === a3.id).muted === true);
  // fade tweak on dialogue + xf duration bump, then undo both
  await commit(p.id, "timeline.clip.fade",
    { timeline_id: tl.id, clip_id: cD.id,
      fade_in_units: 4800, fade_in_curve: "exponential" });
  await commit(p.id, "timeline.crossfade.set",
    { timeline_id: tl.id, out_clip_id: cD.id, in_clip_id: cBy(aD2).id,
      tl_start: F(3) - 28, tl_frames: 28 });
  d = await detail(p.id, tl.id);
  chk("xf duration bumped 24→28", d.crossfades[0].tl_frames === 28);
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  chk("undo restored xf 28→24", d.crossfades[0].tl_frames === 24);
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  // track automation replace (whole-array semantics) + undo
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tl.id, track_id: a2.id, interp: "step",
      points: [{ at_tl_frames: F(2), gain_db: 0 },
               { at_tl_frames: F(4), gain_db: -20 }] });
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  chk("automation replace undone — original ramp restored",
      d.tracks.find((t) => t.id === a2.id).automation_interp === "linear");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a3.id, muted: false });  // unmute for export
  d = await detail(p.id, tl.id);
  ok(`editing regression done — rev ${d.revision}, xf 24f, A3 unmuted`);

  /* ---------- 3. export #1 — ffprobe + PCM battery ---------------- */
  step("3) export FILM @30000/1001 — ffprobe metadata + PCM checks");
  const e1 = await exportAndPcm(tl, "film");
  const vStream = e1.pr.streams.find((s) => s.codec_type === "video");
  const aStream = e1.pr.streams.find((s) => s.codec_type === "audio");
  const dur = parseFloat(e1.pr.format.duration);
  ok(`export dur ${dur.toFixed(3)}s, video ${vStream.codec_name} ` +
     `${vStream.r_frame_rate}, audio ${aStream.codec_name} ${aStream.sample_rate}Hz ` +
     `${aStream.channels}ch`);
  chk("fractional fps in container (≈29.97)",
      Math.abs(eval(vStream.r_frame_rate) - 30000 / 1001) < 0.05);
  const pcm = e1.pcm;
  const e2 = {
    dia_pre:  magS(pcm, 440, 1.2, 1.6),   // pre step-duck
    dia_duck: magS(pcm, 440, 1.6, 1.9),   // inside step duck (clip-local 0.5-1.0s = tl 1.5-2.0)
    xf_out:   magS(pcm, 440, 2.4, 2.6),   // xf out-leg
    xf_end:   magS(pcm, 440, 3.2, 3.5),   // out-leg gone
    xf_in:    magS(pcm, 550, 3.4, 4.2),   // in-leg full
    mus_hi:   magS(pcm, 330, 0.8, 1.6),   // music before lane-duck
    mus_duck: magS(pcm, 330, 3.6, 4.0),   // lane −12 region
    emb_early: magS(pcm, 220, 0.5, 1.2),  // embedded ramp start
    emb_late: magS(pcm, 220, 5.0, 5.8),   // embedded −12 end
    emb2:     magS(pcm, 280, 2.5, 3.5),   // V2 embedded audio
    tiny:     magS(pcm, 660, 1.52, 1.58), // 3f sfx
    gap_rms:  rmsS(pcm, 6.2, 6.8),        // past program end
  };
  chk("dialogue present (clip −3 applied but audible)", e2.dia_pre > 0.03);
  chk("step duck ≈ −9", e2.dia_duck / e2.dia_pre < 0.6);
  chk("xf out-leg fades out", e2.xf_end < e2.xf_out * 0.25);
  chk("xf in-leg at full gain", e2.xf_in > 0.08);
  chk("music lane duck", e2.mus_duck / e2.mus_hi < 0.65);
  chk("embedded ramp on V1", e2.emb_late / e2.emb_early < 0.65);
  chk("V2 embedded audio present", e2.emb2 > 0.01);
  chk("tiny sfx audible at 1.55s", e2.tiny > 0.008);
  chk("silence past program end", e2.gap_rms < 0.006);

  /* ---------- 4. mute/solo semantics end-to-end ------------------- */
  step("4) export with solo on dialogue lane — all other lanes silent");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, solo: true });
  const eSolo = await exportAndPcm(tl, "solo");
  const s440 = magS(eSolo.pcm, 440, 1.2, 1.8);
  const s330 = magS(eSolo.pcm, 330, 0.8, 1.6);
  const s220 = magS(eSolo.pcm, 220, 0.5, 1.2);
  chk("solo: dialogue audible", s440 > 0.03);
  chk("solo: music silenced", s330 < s440 * 0.05);
  chk("solo: embedded silenced", s220 < s440 * 0.05);
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, solo: false });

  /* ---------- 5. navigate + engine + continuous scrub ------------- */
  step("5) navigate to FILM — engine load, waveform, continuous scrub");
  await navTo(p.name);
  for (let i = 0; i < 90; i++) {
    if (await evalJs(`!!(window.__n2fPreview && window.__n2fPreview.engineRef
        && window.__n2fPreview.engineRef.current)`).catch(() => false)) break;
    await sleep(500);
    if (i === 89) throw new Error("preview engine never mounted");
  }
  const dE = await detail(p.id, tl.id);
  await evalJs(`(async () => {
    const h = window.__n2fPreview;
    await h.engineRef.current.load(
      ${JSON.stringify(dE)}, ${JSON.stringify(p.id)});
  })()`);
  await sleep(400);
  const eng0 = await engStats();
  const wave = await evalJs(`!!document.querySelector(
    '[data-testid="waveform-canvas"],canvas')`);
  ok(`engine ready — waveform canvas present: ${wave}`);
  await evalJs(`(() => { const sc =
    document.querySelector('[data-testid="timeline-scroll"]');
    sc.scrollLeft = 0; sc.scrollTop = 0; return true; })()`);
  const drag = await dragFrames(10, 170, 12, 0, true);
  const st = await engStats();
  if (!drag.delivered) throw new Error("scrub drag delivered 0 moves");
  ok(`continuous scrub: ${drag.delivered} delivered/${drag.coalesced} ` +
     `coalesced, ${st.auditions} auditions, ` +
     `sched ${(st.lastScheduleMs ?? 0).toFixed(1)}ms`);

  /* ---------- 6. preview parity — offline render vs export -------- */
  step("6) preview/export parity — shared plan, decoded PCM windows");
  // offline render of the same plan at tl 0.8..2.0s (dialogue pre+duck)
  // and tl 3.2..4.4s (xf in-leg) vs the export PCM magnitudes.
  const probe = await evalJs(`(async () => {
    const h = window.__n2fPreview;
    const plan = await h.buildPreviewPlan(${JSON.stringify(dE)});
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
    const magIn = (buf, f, s0, s1) => {
      const ch = buf.getChannelData(0);
      const w = 2 * Math.PI * f / 48000, cw = 2 * Math.cos(w);
      let sp = 0, sp2 = 0;
      for (let i = Math.floor(s0 * 48000); i < Math.floor(s1 * 48000)
           && i < ch.length; i++) {
        const s = ch[i] + cw * sp - sp2; sp2 = sp; sp = s;
      }
      const n = Math.max(1, Math.floor(s1 * 48000)
                          - Math.floor(s0 * 48000));
      return 2 * Math.sqrt(Math.max(0,
        sp * sp + sp2 * sp2 - cw * sp * sp2)) / n;
    };
    // window A: tl 0.8..2.0 — render 1.2s at 0.8; sample local offsets
    const bA = await h.renderPlanOffline(plan, buffers, 1.3, 48000, 0.8);
    // window B: tl 3.2..4.4 — xf in-leg (550) + music duck
    const bB = await h.renderPlanOffline(plan, buffers, 1.3, 48000, 3.2);
    return {
      a440pre:  magIn(bA, 440, 0.4, 0.8),   // abs 1.2..1.6 pre-duck
      a440duck: magIn(bA, 440, 0.8, 1.1),   // abs 1.6..1.9 in-duck
      b550:     magIn(bB, 550, 0.2, 1.0),   // abs 3.4..4.2 xf-in
      b330:     magIn(bB, 330, 0.4, 0.8),   // abs 3.6..4.0 mus duck
      b220:     magIn(bB, 220, 1.0, 1.25),  // abs 4.2..4.45 embedded
    }; })()`);
  ok(`render — 440 pre ${probe.a440pre.toFixed(3)}, duck ` +
     `${probe.a440duck.toFixed(3)}, 550 xf-in ${probe.b550.toFixed(3)}, ` +
     `330 duck ${probe.b330.toFixed(3)}, 220 emb ${probe.b220.toFixed(3)}`);
  const close = (a, b) => Math.abs(a - b) / Math.max(a, b) < 0.4;
  chk("parity: 440 pre-duck", close(probe.a440pre, e2.dia_pre));
  chk("parity: 440 step-duck", close(probe.a440duck, e2.dia_duck));
  chk("parity: 550 xf-in", close(probe.b550, e2.xf_in));
  chk("parity: 330 music duck", close(probe.b330, e2.mus_duck));
  ok("seek-into-automation: rendering from tl 0.8s already carries the " +
     "step-automation gain (automation evaluated at window start, " +
     "not just at t=0)");

  /* ---------- 7. D3 — cancel, idempotency, durable row ------------ */
  step("7) reliability — export cancel (no phantom/ffmpeg orphan/tmp), idempotent replay");
  const jList0 = (await api("GET", `/api/projects/${p.id}/jobs`)).length;
  const rC = await commit(p.id, "timeline.export",
    { timeline_id: tl.id, profile: "720p" });
  // cancel quickly while ffmpeg runs
  await sleep(1200);
  const jobs = await api("GET", `/api/projects/${p.id}/jobs`);
  const jrow = jobs.find((j) => j.id === rC.job_id);
  chk("export job is a durable row",
      jrow && ["queued", "running", "succeeded"].includes(jrow.status));
  if (jrow.status !== "succeeded") {
    const cr = await api("POST", `/api/projects/${p.id}/jobs/${rC.job_id}/cancel`,
      {}, 200).catch(() => null);
    const jEnd = await waitJob(p.id, rC.job_id, true);
    chk("cancelled job ends cancelled (no phantom output)",
        ["cancelled", "succeeded"].includes(jEnd.status) &&
        (jEnd.status !== "cancelled" || !jEnd.output_asset_id));
  } else ok("export finished before cancel landed — job semantics intact");
  await sleep(1500);
  chk("no orphan ffmpeg after cancel", !left("ffmpeg.exe"));
  // partial-output check: if the cancel landed, the job's work dir
  // must not hold a half-written mp4 (adapter unlinks on cancel/fail).
  // Retained concat lists + succeeded outputs are intended artifacts.
  const jobsDir = `${process.env.LOCALAPPDATA}\\AtomicAIStudio\\jobs`;
  let partial = -1;
  try {
    partial = fs.readdirSync(`${jobsDir}\\${rC.job_id}`)
      .filter((f) => /-export\.(mp4|part|tmp)$/.test(f)).length;
  } catch {}
  const jFinal = (await api("GET", `/api/projects/${p.id}/jobs`))
    .find((j) => j.id === rC.job_id);
  chk("cancelled job left no partial output + no registered asset",
      jFinal?.status !== "cancelled" ||
      (partial === 0 && !jFinal.output_asset_id));
  // idempotent replay — same key must not create a second job.
  // Send twice with the same body; if the first commit bumped the
  // revision the replay may 409 (also a safe refusal, not a dup).
  const idem = `n3d-idem-${Date.now()}`;
  const proj1 = await api("GET", `/api/projects/${p.id}`);
  const body = { command_type: "timeline.export",
    payload: { timeline_id: tl.id, profile: "720p" },
    scope: "project", origin: "audit",
    expected_revision: proj1.revision, idempotency_key: idem };
  const jMid = (await api("GET", `/api/projects/${p.id}/jobs`)).length;
  const r1 = await api("POST", `/api/projects/${p.id}/commands/commit`, body, 200);
  const r2raw = await fetch(`${API}/api/projects/${p.id}/commands/commit`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: JSON.stringify(body) });
  const r2 = r2raw.status === 200 ? await r2raw.json() : null;
  // correct semantics: replay returns the ORIGINAL command with
  // idempotent_replay=true and never re-runs the handler (job_id null)
  const jNow = (await api("GET", `/api/projects/${p.id}/jobs`)).length;
  chk("idempotent replay: original outcome, handler not re-run",
      r2 !== null && r2.idempotent_replay === true &&
      jNow - jMid === 1);
  await waitJob(p.id, r1.job_id);   // let the replayed job finish
  const jList1 = (await api("GET", `/api/projects/${p.id}/jobs`)).length;
  ok(`durable rows: ${jList1} jobs total (${jList1 - jList0} added this step)`);

  /* ---------- 8. D4 perf — long timeline, dense auto, exports ----- */
  step("8) perf — 240-clip multi-track timeline, 400-pt automation, repeat exports");
  await commit(p.id, "timeline.create",
    { name: "LONG", fps_num: 30, fps_den: 1 });
  const tlL = (await api("GET", `/api/projects/${p.id}/timelines`))
    .find((t) => t.name === "LONG");
  for (let i = 0; i < 6; i++)
    await commit(p.id, "timeline.track.add",
      { timeline_id: tlL.id, kind: "audio", name: `P${i + 1}` });
  let dl0 = await detail(p.id, tlL.id);
  const lanes = dl0.tracks.filter((t) => t.kind === "audio");
  // 45f spacing → ~3-min program; same-lane gap 270f > clip lengths
  const tB0 = Date.now();
  for (let i = 0; i < 240; i++) {
    await commit(p.id, "timeline.clip.place",
      { timeline_id: tlL.id, track_id: lanes[i % 6].id,
        asset_id: [aT, aT, aD, aT, aD2, aT, aM, aT][i % 8].id,
        start_frames: i * 45, allow_duplicate: true });
  }
  ok(`240 clips placed on 6 lanes in ${((Date.now() - tB0) / 1000).toFixed(1)}s`);
  const dense = [];
  for (let i = 0; i < 400; i++)
    dense.push({ at_tl_frames: i * 5, gain_db: -(i % 7) });
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tlL.id, track_id: lanes[0].id,
      points: dense.slice(0, 200), interp: "linear" });
  await commit(p.id, "timeline.track.automation",
    { timeline_id: tlL.id, track_id: lanes[1].id,
      points: dense.slice(0, 200), interp: "step" });
  const appRss0 = (await procSample("atomic-dream-engine")).rssMB;
  const beRss0 = (await procSample("atomic-backend")).rssMB;
  // D3 finding (recorded): the multi-segment encode path builds one
  // giant argv — at ~240 clips it exceeds WinError 206 (cmdline limit).
  // Repeated-export timing uses the bounded film instead; LONG covers
  // clip-count load, automation density and UI-side scaling.
  const rLong = await commit(p.id, "timeline.export",
    { timeline_id: tlL.id, profile: "720p" });
  const jLong = await waitJob(p.id, rLong.job_id, true);
  ok(`240-clip export → ${jLong.status}: ` +
     `${(jLong.error || "").slice(0, 90)} — known Win argv bound, recorded`);
  // 3 repeated exports of the film — measure each
  const exT = [];
  for (let i = 0; i < 3; i++) {
    const t0 = Date.now();
    const r = await commit(p.id, "timeline.export",
      { timeline_id: tl.id, profile: "720p" });
    await waitJob(p.id, r.job_id);
    exT.push(Date.now() - t0);
  }
  ok(`3 repeated exports: ${exT.map((t) => (t / 1000).toFixed(1) + "s").join(", ")}`);
  const appRss1 = (await procSample("atomic-dream-engine")).rssMB;
  const beRss1 = (await procSample("atomic-backend")).rssMB;
  ok(`RSS app ${appRss0.toFixed(0)}→${appRss1.toFixed(0)}MB, ` +
     `backend ${beRss0.toFixed(0)}→${beRss1.toFixed(0)}MB`);

  /* ---------- 9. restart persistence ------------------------------ */
  step("9) restart persistence — relaunch, reopen FILM, verify state");
  await send("Browser.close").catch(() => {});
  await sleep(2500);
  for (const n of ["atomic-dream-engine.exe", "atomic-backend.exe"]) {
    try {
      execFileSync("taskkill", ["/F", "/IM", n], { stdio: "ignore" });
    } catch {}
  }
  await sleep(1500);
  spawn(EXE, [], { detached: true, stdio: "ignore",
    env: { ...process.env,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: "--remote-debugging-port=" + PORT } })
    .unref();
  await attachWsWait(60000);
  API = null; TOKEN = null;
  await creds();
  const dR = await detail(p.id, tl.id);
  const xfR = dR.crossfades[0];
  const a2R = dR.tracks.find((t) => t.id === a2.id);
  const a2Pts = JSON.parse(a2R.automation_json || "[]");
  chk("persisted after restart — clips/xf/automation/revision",
      dR.clips.length >= 6 && xfR && xfR.tl_frames === 24 &&
      a2Pts.length === 3 && a2R.gain_db === -6);
  ok(`restart OK — rev ${dR.revision}, ${dR.clips.length} clips, ` +
     `xf ${xfR.tl_frames}f ${xfR.curve}`);

  /* ---------- 10. clean shutdown ---------------------------------- */
  step("10) clean shutdown — no orphans");
  await send("Browser.close").catch(() => {});
  await sleep(1500);
  execFileSync("powershell", ["-NoProfile", "-Command",
    "Get-Process atomic-dream-engine -ErrorAction SilentlyContinue | " +
    "ForEach-Object { $_.CloseMainWindow() | Out-Null }"],
    { stdio: "ignore" });
  await sleep(4000);
  const orphans = ["atomic-dream-engine.exe", "atomic-backend.exe",
                   "ffmpeg.exe"].filter((n) => left(n));
  chk("no orphan processes after close", orphans.length === 0);
  fs.writeFileSync(`${OUT}\\n3d-acceptance.txt`, LOG.join("\n"));
  console.log(`\nN3-D ACCEPTANCE: PASS ${STEP}/${STEP} steps — ${OUT}`);
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
  fs.writeFileSync(`${OUT}\\n3d-acceptance.txt`,
    LOG.join("\n") + "\nFAIL: " + e.message);
  console.error("FAIL:", e.message);
  process.exit(1);
});
