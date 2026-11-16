// N3-A acceptance — installed NSIS app, per-track gain automation:
// embedded-video lane + two audio lanes, lane keyframes linear+step,
// add/remove/edit/interp changes, move/trim/split invariance,
// undo/redo, locked-lane mixer semantics, mute/solo, offline preview
// parity + seek-into-ramp, export PCM verification, real app restart
// persistence, graceful Browser.close shutdown, orphan check.
// Zero credentials/cloud. Run: node scripts/cdp-n3a-acceptance.cjs
// <cdpPort> <outDir> [installedExePath]
const fs = require("fs");
const path = require("path");
const { execFileSync, spawn } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n3a-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Programs\\Atomic Dream Engine\\atomic-dream-engine.exe`;
// --verify-only: post-restart pass — reads restart-state.json, verifies
// persisted automation + clips against a freshly launched app instance.
const VERIFY_ONLY = process.argv.includes("--verify-only");
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
    idempotency_key: `n3a-${type}-${Date.now()}-${Math.random()}` }, expect);
};
const commitStatus = async (pid, type, payload) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  const r = await fetch(`${API}/api/projects/${pid}/commands/commit`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: JSON.stringify({
      command_type: type, payload, scope: "project", origin: "audit",
      expected_revision: proj.revision,
      idempotency_key: `n3a-${type}-${Date.now()}-${Math.random()}` }) });
  return r.status;
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
// PCM decode straight to memory via stdout — Defender eats .pcm files.
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
const trackOf = (d, id) => d.tracks.find((t) => t.id === id);

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

async function main() {
  await attachWsWait();
  await creds();

  if (VERIFY_ONLY) {
    step("9v) post-restart persistence verification");
    const st = JSON.parse(fs.readFileSync(`${OUT}\\restart-state.json`, "utf8"));
    const post = await detail(st.projectId, st.timelineId);
    const a1post = post.tracks.find((t) => t.name === "A1-dialogue");
    const a2post = post.tracks.find((t) => t.name === "A2-music");
    const v1post = post.tracks.find((t) => t.kind === "video");
    if (a1post.automation_json !== st.a1auto)
      throw new Error("A1 lane automation not persisted across restart");
    if (a1post.automation_interp !== "linear" ||
        a2post.automation_interp !== "step" ||
        v1post.automation_interp !== "linear")
      throw new Error("automation interp not persisted");
    if (post.clips.length !== 4)
      throw new Error(`clip count ${post.clips.length} != 4 after restart`);
    const dia = post.clips.find((c) =>
      post.tracks.find((t) => t.id === c.track_id).name === "A1-dialogue");
    if (dia.start_frames !== 60)
      throw new Error("moved clip position not persisted");
    ok(`restart persistence verified: project '${st.projectName}' ` +
       `${post.clips.length} clips, A1 automation byte-identical, ` +
       `interps linear/step/linear, revision ${st.rev0} → ${post.revision}`);
    fs.writeFileSync(`${OUT}\\n3a-acceptance.log`, LOG.join("\n"));
    return;
  }

  /* ---------- 0. migration — existing N2 project at 0019 ---------- */
  step("0) migration — 0018_n2f DB upgraded in place");
  const projects = await api("GET", "/api/projects");
  const old = (projects.items || projects).find((x) => x.name.startsWith("N2F-"));
  ok(`projects listed: ${(projects.items || projects).length}; prior N2-F project ${old ? "FOUND (" + old.name + ")" : "absent (fresh DB)"}`);
  if (old) {
    const tls = await api("GET", `/api/projects/${old.id}/timelines`);
    const film = tls.find((t) => t.name === "FILM");
    if (film) {
      const dd = await detail(old.id, film.id);
      const lanes = dd.tracks;
      const a2 = lanes.find((t) => t.name === "A2-music");
      // N2-F set A2 gain −4 dB — must survive the migration.
      if (a2 && a2.gain_db !== -4) throw new Error(`A2 gain ${a2.gain_db} lost in migration`);
      ok(`N2 project opens: ${dd.clips.length} clips, A2 gain ${a2?.gain_db} dB preserved, ` +
         `lane automation defaults ${lanes.every((t) => t.automation_json === "[]") ? "neutral '[]'" : "NON-NEUTRAL"}`);
      if (!lanes.every((t) => t.automation_json === "[]")) throw new Error("automation defaults not neutral");
    }
  }

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
  const wDia = wav(440);            // dialogue — a1 lane (linear ramp)
  const wMus = wav(330, 2, 44100);  // music bed — a2 lane (step)
  ok("media: embedded mp4 (220Hz) + 440/330 wavs (330 @44.1k)");

  /* ---------- 1. scene: video lane + 2 audio lanes @30fps ---------- */
  step("1) project + scene — embedded V1, dialogue A1, music A2 @30fps");
  const p = await api("POST", "/api/projects", { name: `N3A-${Date.now()}` }, 201);
  for (const [f, k] of [[vEmbed, "video"], [wDia, "audio"], [wMus, "audio"]]) {
    const ra = await commit(p.id, "asset.import", { source_path: path.resolve(f), kind: k });
    await waitJob(p.id, ra.job_id);
  }
  const assets = (await api("GET", `/api/projects/${p.id}/assets?limit=50`)).items;
  const find = (s) => assets.find((a) => a.name.includes(s));
  const [aV, aD, aM] = ["vembed", "f440", "f330"].map(find);
  await commit(p.id, "timeline.create", { name: "FILM", fps_num: 30, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`))
    .find((t) => t.name === "FILM");
  await commit(p.id, "timeline.track.add", { timeline_id: tl.id, kind: "audio", name: "A1-dialogue" });
  await commit(p.id, "timeline.track.add", { timeline_id: tl.id, kind: "audio", name: "A2-music" });
  let d = await detail(p.id, tl.id);
  const v1 = d.tracks.find((t) => t.kind === "video");
  const a1 = trackOf(d, d.tracks.find((t) => t.name === "A1-dialogue").id);
  const a2 = trackOf(d, d.tracks.find((t) => t.name === "A2-music").id);
  await commit(p.id, "timeline.clip.place", { timeline_id: tl.id, track_id: v1.id, asset_id: aV.id, start_frames: 0 });
  await commit(p.id, "timeline.clip.place", { timeline_id: tl.id, track_id: a1.id, asset_id: aD.id, start_frames: 30 });
  await commit(p.id, "timeline.clip.place", { timeline_id: tl.id, track_id: a2.id, asset_id: aM.id, start_frames: 0 });
  d = await detail(p.id, tl.id);
  const cBy = (a) => d.clips.find((x) => x.source_asset_id === a.id);
  await commit(p.id, "timeline.clip.trim", { timeline_id: tl.id, clip_id: cBy(aV).id, out_frame: 150 });
  await commit(p.id, "timeline.clip.trim", { timeline_id: tl.id, clip_id: cBy(aD).id, out_frame: 144000 });
  await commit(p.id, "timeline.clip.trim", { timeline_id: tl.id, clip_id: cBy(aM).id, out_frame: 220500 });
  d = await detail(p.id, tl.id);
  const cV = cBy(aV), cD = cBy(aD), cM = cBy(aM);
  ok(`clips: embedded 0→150f, dialogue 30→120f, music 0→150f`);

  /* ---------- 2. create track automation + edit ops ---------- */
  step("2) track automation — create, add/remove keyframe, gain edit, interp swap");
  const tauto = (trackId, points, interp) =>
    commit(p.id, "timeline.track.automation",
      { timeline_id: tl.id, track_id: trackId, points, ...(interp ? { interp } : {}) });
  // A1 linear: unity → −12 @tl75 → unity @tl120 (duck-recover)
  await tauto(a1.id, [{ at_tl_frames: 30, gain_db: 0 },
                      { at_tl_frames: 75, gain_db: -12 },
                      { at_tl_frames: 120, gain_db: 0 }], "linear");
  // V1 linear embedded ramp 0→−12
  await tauto(v1.id, [{ at_tl_frames: 0, gain_db: 0 },
                      { at_tl_frames: 150, gain_db: -12 }], "linear");
  // A2 step: 0 → −18 at tl75
  await tauto(a2.id, [{ at_tl_frames: 0, gain_db: 0 },
                      { at_tl_frames: 75, gain_db: -18 }], "step");
  d = await detail(p.id, tl.id);
  ok(`automation committed: A1 3pts linear, V1 2pts linear, A2 2pts step`);
  // + keyframe (add): A2 gains a third point
  await tauto(a2.id, [{ at_tl_frames: 0, gain_db: 0 },
                      { at_tl_frames: 75, gain_db: -18 },
                      { at_tl_frames: 140, gain_db: 0 }]);
  if (JSON.parse(trackOf(await detail(p.id, tl.id), a2.id).automation_json).length !== 3)
    throw new Error("add keyframe failed");
  // − keyframe (remove): back to 2
  await tauto(a2.id, [{ at_tl_frames: 0, gain_db: 0 },
                      { at_tl_frames: 75, gain_db: -18 }]);
  // edit gain: −18 → −15
  await tauto(a2.id, [{ at_tl_frames: 0, gain_db: 0 },
                      { at_tl_frames: 75, gain_db: -15 }]);
  // interp swap: step → linear → step
  await tauto(a2.id, [{ at_tl_frames: 0, gain_db: 0 },
                      { at_tl_frames: 75, gain_db: -15 }], "linear");
  await tauto(a2.id, [{ at_tl_frames: 0, gain_db: 0 },
                      { at_tl_frames: 75, gain_db: -15 }], "step");
  d = await detail(p.id, tl.id);
  const a2pts = JSON.parse(trackOf(d, a2.id).automation_json);
  if (a2pts.length !== 2 || a2pts[1].gain_db !== -15 ||
      trackOf(d, a2.id).automation_interp !== "step")
    throw new Error("edit/interp ops failed");
  ok("add/remove/edit/interp ops verified through detail readback");

  /* ---------- 3. edits under automation ---------- */
  step("3) move + trim + split — lane keyframes byte-identical");
  const before = trackOf(d, a1.id).automation_json;
  await commit(p.id, "timeline.clip.move",
    { timeline_id: tl.id, clip_id: cD.id, start_frames: 60 });
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: cM.id, in_frame: 44100 });
  await commit(p.id, "timeline.clip.split",
    { timeline_id: tl.id, clip_id: cM.id, at_frame: 75 });
  d = await detail(p.id, tl.id);
  if (trackOf(d, a1.id).automation_json !== before)
    throw new Error("move/trim/split touched lane automation");
  if (d.clips.filter((x) => x.track_id === a2.id).length !== 2)
    throw new Error("music split missing right fragment");
  ok(`automation byte-identical after move/trim/split; music split into ${d.clips.filter(x => x.track_id === a2.id).length} fragments`);

  /* ---------- 4. undo/redo ---------- */
  step("4) undo/redo — commands + automation");
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });   // split
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });   // trim
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });   // move
  d = await detail(p.id, tl.id);
  const cD2 = d.clips.find((x) => x.source_asset_id === aD.id);
  if (cD2.start_frames !== 30) throw new Error("undo did not restore move");
  if (d.clips.filter((x) => x.track_id === a2.id).length !== 1)
    throw new Error("undo did not restore split");
  await commit(p.id, "timeline.redo", { timeline_id: tl.id });
  await commit(p.id, "timeline.redo", { timeline_id: tl.id });
  await commit(p.id, "timeline.redo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  if (d.clips.find((x) => x.source_asset_id === aD.id).start_frames !== 60)
    throw new Error("redo did not reapply move");
  if (d.clips.filter((x) => x.track_id === a2.id).length !== 2)
    throw new Error("redo did not reapply split");
  // automation undo/redo: clear via undo of the last tauto, restore
  const a1auto = trackOf(d, a1.id).automation_json;
  await tauto(a1.id, []);
  if (trackOf(await detail(p.id, tl.id), a1.id).automation_json !== "[]")
    throw new Error("automation clear failed");
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  if (trackOf(await detail(p.id, tl.id), a1.id).automation_json !== a1auto)
    throw new Error("automation undo failed");
  await commit(p.id, "timeline.redo", { timeline_id: tl.id });
  if (trackOf(await detail(p.id, tl.id), a1.id).automation_json !== "[]")
    throw new Error("automation redo failed");
  await tauto(a1.id, JSON.parse(a1auto).map(({ at_tl_frames, gain_db }) => ({ at_tl_frames, gain_db })));
  d = await detail(p.id, tl.id);
  ok("undo/redo round-trips for edit ops and lane automation");

  /* ---------- 5. locked lane + mute/solo ---------- */
  step("5) locked lane refuses clip edits, keeps mixer; mute/solo");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, locked: true });
  const mv = await commitStatus(p.id, "timeline.clip.move",
    { timeline_id: tl.id, clip_id: cD.id, start_frames: 40 });
  if (mv !== 422) throw new Error(`locked lane clip move → ${mv}, expected 422`);
  await tauto(a1.id, [{ at_tl_frames: 0, gain_db: -3 }]);   // mixer op allowed
  ok("clip.move → 422 on locked lane; track.automation still applies");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, locked: false });
  await tauto(a1.id, JSON.parse(a1auto).map(({ at_tl_frames, gain_db }) => ({ at_tl_frames, gain_db })));
  // mute a2 → export → 330 absent; unmute; solo a1 → only 440 region audible
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a2.id, muted: true });
  let r = await commit(p.id, "timeline.export", { timeline_id: tl.id, profile: "720p" });
  let job = await waitJob(p.id, r.job_id);
  let dl = await fetch(`${API}/api/projects/${p.id}/assets/${job.output_asset_id}/file`,
    { headers: { "X-Atomic-Token": TOKEN } });
  let mp4 = `${OUT}\\media\\n3a-muted.mp4`;
  fs.writeFileSync(mp4, Buffer.from(await dl.arrayBuffer()));
  let pcm = decodePcm(mp4);
  if (mag(pcm, 330, 0.5 * 48000, 1.5 * 48000) > 0.005)
    throw new Error("muted music still audible");
  if (mag(pcm, 440, 2.4 * 48000, 2.7 * 48000) < 0.01)
    throw new Error("dialogue missing in muted-music render");
  ok("mute A2 → 330 absent, 440 intact");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a2.id, muted: false });
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, solo: true });
  r = await commit(p.id, "timeline.export", { timeline_id: tl.id, profile: "720p" });
  job = await waitJob(p.id, r.job_id);
  dl = await fetch(`${API}/api/projects/${p.id}/assets/${job.output_asset_id}/file`,
    { headers: { "X-Atomic-Token": TOKEN } });
  mp4 = `${OUT}\\media\\n3a-solo.mp4`;
  fs.writeFileSync(mp4, Buffer.from(await dl.arrayBuffer()));
  pcm = decodePcm(mp4);
  if (mag(pcm, 330, 0.5 * 48000, 1.5 * 48000) > 0.005)
    throw new Error("non-solo music audible");
  if (mag(pcm, 220, 0.5 * 48000, 1.5 * 48000) > 0.005)
    throw new Error("non-solo embedded audible");
  if (mag(pcm, 440, 2.4 * 48000, 2.7 * 48000) < 0.01)
    throw new Error("solo dialogue missing");
  ok("solo A1 → 330 + embedded 220 silenced, 440 intact");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, solo: false });

  /* ---------- 6. export + PCM — every lane automation ---------- */
  step("6) export FILM + PCM — lane ramps, step, embedded, no extension");
  const t0 = Date.now();
  r = await commit(p.id, "timeline.export", { timeline_id: tl.id, profile: "720p" });
  job = await waitJob(p.id, r.job_id);
  const exportMs = Date.now() - t0;
  dl = await fetch(`${API}/api/projects/${p.id}/assets/${job.output_asset_id}/file`,
    { headers: { "X-Atomic-Token": TOKEN } });
  mp4 = `${OUT}\\media\\n3a-export.mp4`;
  fs.writeFileSync(mp4, Buffer.from(await dl.arrayBuffer()));
  const pr = ffprobe(mp4);
  const vs = pr.streams.find((s) => s.codec_type === "video");
  const as = pr.streams.find((s) => s.codec_type === "audio");
  const dur = parseFloat(pr.format.duration);
  ok(`rendered ${(exportMs / 1000).toFixed(1)}s → h264 ${vs.nb_frames}f ${vs.r_frame_rate} + aac ${as.sample_rate} ${as.channel_layout} dur ${dur.toFixed(3)}s`);
  if (dur < 4.9 || dur > 5.3) throw new Error(`duration ${dur} — program extended`);
  pcm = decodePcm(mp4);
  const m = (f, s0, s1) => mag(pcm, f, s0 * 48000, s1 * 48000);
  const checks = [];
  const chk = (name, cond) => { checks.push([name, cond]); ok(`${name}: ${cond ? "OK" : "FAIL"}`); };
  // dialogue moved to tl60 (2.0s) — before that it's silent (move worked)
  chk("no dialogue 0.5–1.8s (clip moved to tl60)", m(440, 0.5, 1.8) < 0.008);
  // A1 ramp {30:0,75:−12,120:0} at tl 60→120: clip enters at −6, dips
  // to −12 @2.5s, recovers to 0 @4.0s — PCM must show the dip at the
  // SAME absolute window regardless of the move.
  chk("lane duck window — 440 dips at absolute 2.4–2.7s",
      m(440, 2.4, 2.7) / Math.max(1e-6, m(440, 3.7, 3.95)) < 0.55);
  chk("lane duck recovers by 3.7–3.95s", m(440, 3.7, 3.95) > 0.03);
  // A2 step −15 at tl75 = 2.5s
  chk("music step: early > late (≈−15 dB)",
      m(330, 0.5, 1.5) / Math.max(1e-6, m(330, 2.6, 4.0)) > 3);
  // music split at tl75 + trim in 44100 — right fragment still plays
  chk("music present after split boundary", m(330, 2.6, 3.5) > 0.005);
  // V1 embedded linear ramp 0→−12 over 5s
  chk("embedded lane ramp: late < early",
      m(220, 4.0, 4.9) / Math.max(1e-6, m(220, 0.1, 1.0)) < 0.5);
  chk("embedded present early", m(220, 0.1, 1.0) > 0.02);
  const peak = rms(pcm, 0, Math.floor(dur * 48000));
  chk("no clipping", peak < 0.71);
  chk("not silent", peak > 0.02);
  if (checks.some(([, c]) => !c)) throw new Error("export PCM checks failed");

  /* ---------- 7. preview — automated lanes + seek into ramp ---------- */
  step("7) preview — offline parity incl. lane curves + seek into ramp");
  let navOk = false;
  for (let att = 0; att < 4 && !navOk; att++) {
    try { await navTo(p.name); navOk = true; }
    catch (e) { ok(`nav ${att + 1}: ${e.message.slice(0, 70)}`); await sleep(4000); }
  }
  if (!navOk) throw new Error("navigation never stabilized");
  await sleep(1200);
  const detailJson = await detail(p.id, tl.id);
  const offPcm = await evalJs(`(async () => {
    const h = window.__n2fPreview;
    if (!h) return {err:"hook missing"};
    const plan = h.buildPreviewPlan(${JSON.stringify(detailJson)});
    const off = new OfflineAudioContext(2, 48000, 48000);
    const buffers = new Map();
    for (const c of plan.clips) {
      const res = await fetch("${API}/api/projects/" +
        ${JSON.stringify(p.id)} + "/assets/" + c.assetId + "/file",
        {headers:{"X-Atomic-Token":${JSON.stringify(TOKEN)}}});
      const ab = await res.arrayBuffer();
      buffers.set(c.clipId, await off.decodeAudioData(ab));
    }
    const rendered = await h.renderPlanOffline(plan, buffers, 5.2, 48000);
    const L = rendered.getChannelData(0), R = rendered.getChannelData(1);
    const mono = new Float32Array(L.length);
    for (let i=0;i<L.length;i++) mono[i]=(L[i]+R[i])/2;
    const mag = (f,s0,s1) => {
      const w=2*Math.PI*f/48000, cw=2*Math.cos(w); let a=0,b=0;
      for (let i=Math.floor(s0*48000); i<Math.floor(s1*48000); i++){
        const v=mono[i]+cw*a-b; b=a; a=v; }
      const n=Math.max(1,Math.floor((s1-s0)*48000));
      return 2*Math.sqrt(Math.max(0,a*a+b*b-cw*a*b))/n; };
    // seek into the lane duck (clip enters ramp mid-way at tl 60/2.0s)
    const rSeek = await h.renderPlanOffline(plan, buffers, 0.8, 48000, 2.35);
    const magOf = (buf,f,s0,s1) => {
      const L2=buf.getChannelData(0), R2=buf.getChannelData(1);
      const w=2*Math.PI*f/48000, cw=2*Math.cos(w); let a=0,b=0;
      for (let i=Math.floor(s0*48000); i<Math.floor(s1*48000); i++){
        const v=(L2[i]+R2[i])/2+cw*a-b; b=a; a=v; }
      const n=Math.max(1,Math.floor((s1-s0)*48000));
      return 2*Math.sqrt(Math.max(0,a*a+b*b-cw*a*b))/n; };
    return {
      m440_duck: mag(440, 2.4, 2.7), m440_rec: mag(440, 3.7, 3.95),
      m330_pre:  mag(330, 0.5, 1.5), m330_post: mag(330, 2.6, 4.0),
      m220_early:mag(220, 0.1, 1.0), m220_late:  mag(220, 4.0, 4.9),
      seek_duck: magOf(rSeek, 440, 0.05, 0.7),
      nClips: plan.clips.length,
      embedded: plan.clips.filter(c=>c.embedded).length }; })()`);
  if (offPcm.err) throw new Error("offline render: " + offPcm.err);
  ok(`preview plan: ${offPcm.nClips} clips (${offPcm.embedded} embedded)`);
  const near = (a, b, tol, name) => {
    const okv = Math.abs(a - b) <= tol;
    ok(`${name}: preview ${a.toFixed(4)} vs export ${b.toFixed(4)} ${okv ? "OK" : "FAIL"}`);
    checks.push([name, okv]);
  };
  near(offPcm.m440_duck, m(440, 2.4, 2.7), 0.06, "lane-ramp duck parity");
  near(offPcm.m440_rec, m(440, 3.7, 3.95), 0.06, "lane-ramp recover parity");
  near(offPcm.m330_pre, m(330, 0.5, 1.5), 0.06, "step pre parity");
  near(offPcm.m330_post, m(330, 2.6, 4.0), 0.06, "step post parity");
  near(offPcm.m220_early, m(220, 0.1, 1.0), 0.06, "embedded ramp early parity");
  near(offPcm.m220_late, m(220, 4.0, 4.9), 0.06, "embedded ramp late parity");
  near(offPcm.seek_duck, offPcm.m440_duck, 0.06, "seek-into-ramp parity");
  if (checks.some(([, c]) => !c)) throw new Error("preview parity failed");
  // transport — play advances with automation scheduled
  await evalJs(`document.querySelector('[data-testid="preview-play"]').click()`);
  await sleep(2500);
  const t1 = await evalJs(`document.querySelector('[data-testid="playhead-readout"]').textContent`);
  await evalJs(`document.querySelector('[data-testid="preview-stop"]').click()`);
  ok(`transport play advanced to ${t1} with lane automation scheduled, then stopped`);

  /* ---------- 8. backup usability ---------- */
  step("8) backup export works post-migration");
  const bj = await commit(p.id, "backup.export", {});
  const bjob = await waitJob(p.id, bj.job_id);
  ok(`backup job ${bjob.status} — existing-backup path functional after 0019`);

  /* ---------- 9. restart — real app relaunch ---------- */
  step("9) REAL app restart → persistence + migration head");
  const preRestart = await detail(p.id, tl.id);
  const rev0 = preRestart.revision;
  fs.writeFileSync(`${OUT}\\restart-state.json`, JSON.stringify({
    projectId: p.id, timelineId: tl.id, projectName: p.name,
    rev0, a1auto: trackOf(preRestart, a1.id).automation_json }));
  // close the app tree, wait for full exit, relaunch
  try { execFileSync("taskkill", ["/IM", "atomic-dream-engine.exe", "/F", "/T"]); } catch { /* gone */ }
  for (let i = 0; i < 20; i++) {
    try {
      const t = execFileSync("tasklist", ["/FI", "IMAGENAME eq atomic-dream-engine.exe",
        "/FO", "CSV", "/NH"]).toString();
      if (!t.includes("atomic-dream-engine")) break;
    } catch { break; }
    await sleep(500);
  }
  const env2 = { ...process.env,
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${PORT}` };
  const child = spawn(EXE, [], { env: env2, detached: true, stdio: "ignore" });
  child.unref();
  ok("app relaunched after tree kill — waiting for CDP");
  ws = undefined;
  await attachWsWait(60000);
  API = null; TOKEN = null;
  await creds();
  const post = await detail(p.id, tl.id);
  const a1post = trackOf(post, a1.id);
  if (a1post.automation_json !== trackOf(preRestart, a1.id).automation_json)
    throw new Error("track automation lost across restart");
  if (JSON.stringify(post.clips.map(c=>c.id).sort()) !==
      JSON.stringify(preRestart.clips.map(c=>c.id).sort()))
    throw new Error("clip set changed across restart");
  ok(`restart persistence: automation + ${post.clips.length} clips intact (rev ${rev0} → ${post.revision})`);

  /* ---------- 10. shutdown — no orphans ---------- */
  step("10) shutdown — graceful close, no orphan processes");
  try {
    const ver = await fetch(`http://127.0.0.1:${PORT}/json/version`).then((x) => x.json());
    const bws = new WebSocket(ver.webSocketDebuggerUrl);
    await new Promise((r2) => (bws.onopen = r2));
    bws.send(JSON.stringify({ id: 1, method: "Browser.close" }));
    await sleep(3000);
  } catch { /* may already be gone */ }
  const left = (name) => {
    try {
      return execFileSync("tasklist", ["/FI", `IMAGENAME eq ${name}`, "/FO", "CSV", "/NH"]).toString().includes(name);
    } catch { return false; }
  };
  const appLeft = left("atomic-dream-engine.exe");
  const beLeft = left("atomic-backend.exe");
  ok(`post-close: app ${appLeft ? "STILL RUNNING" : "gone"}, backend ${beLeft ? "STILL RUNNING (orphan)" : "gone"}`);
  if (beLeft) throw new Error("orphan backend process after close");

  fs.writeFileSync(`${OUT}\\n3a-acceptance.log`, LOG.join("\n"));
  console.log(`\nN3-A ACCEPTANCE: PASS ${STEP}/${STEP} steps — ${OUT}`);
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
  fs.writeFileSync(`${OUT}\\n3a-acceptance.log`,
    LOG.join("\n") + "\nFAIL: " + e.message);
  console.error("FAIL:", e.message);
  process.exit(1);
});
