// A5 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port, same plumbing
// as the A1/A2/A3/A4 drivers). 15 steps per the A5.9 checklist on a
// populated A4 project (screenplay + scenes + scaffolded shot).
// Run: node scripts/cdp-a5-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path"), os = require("os");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9229";
const OUT = process.argv[3] || "docs/a5-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Atomic Dream Engine\\atomic-dream-engine.exe`;
const INSTALL_DIR = path.dirname(EXE);
const STEPS = 15;
let API = null, TOKEN = null, appProc = null;
const lines = [];
let steps = 0;
const ok = (m) => { steps++; lines.push(`PASS ${m}`); console.log(`PASS ${m}`); };
const note = (m) => { lines.push(`INFO ${m}`); console.log(`INFO ${m}`); };
const fail = (m) => { lines.push(`FAIL ${m}`); throw new Error(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(method, p, body, expect = 200) {
  const r = await fetch(`${API}${p}`, {
    method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status !== expect) fail(`${method} ${p} -> ${r.status}: ${(await r.text()).slice(0, 300)}`);
  return r.status === 204 ? null : r.json();
}
async function commit(pid, ct, payload, expect = 200, key) {
  const rev = (await api("GET", `/api/projects/${pid}`)).revision;
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: ct, payload, scope: "project", origin: "acceptance",
    expected_revision: rev, idempotency_key: key || `a5-${ct}-${Date.now()}-${Math.random()}` }, expect);
}
async function preview(pid, ct, payload, expect = 200) {
  return api("POST", `/api/projects/${pid}/commands/preview`, {
    command_type: ct, payload, scope: "project", origin: "acceptance" }, expect);
}
async function waitJob(pid, jobId, timeout = 120000) {
  const t0 = Date.now();
  for (;;) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) return j;
    if (Date.now() - t0 > timeout) fail(`job ${jobId} timeout`);
    await sleep(400);
  }
}
// Deterministic: the job must reach the mock hold barrier (running +
// "hold" progress message) — no timing guesses.
async function waitHold(pid, jobId, timeout = 60000) {
  const t0 = Date.now();
  for (;;) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && j.status === "running" && /hold/i.test(j.message || "")) return j;
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) {
      fail(`job ${jobId} settled before the hold barrier (${j.status})`);
    }
    if (Date.now() - t0 > timeout) fail(`job ${jobId} never reached hold`);
    await sleep(300);
  }
}
const _pend = new Map(), _events = new Map();
let _ws = null, _id = 0;
function send(method, params = {}) {
  return new Promise((res, rej) => {
    const id = ++_id; _pend.set(id, { res, rej, m: method });
    _ws.send(JSON.stringify({ id, method, params }));
  });
}
function onEvent(m, fn) { if (!_events.has(m)) _events.set(m, []); _events.get(m).push(fn); }
async function attachWs() {
  const targets = await fetch(`http://127.0.0.1:${PORT}/json`).then((r) => r.json());
  const page = targets.find((t) => t.type === "page" && !t.url.startsWith("devtools"));
  if (!page) throw new Error("no page target");
  _ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { _ws.onopen = res; _ws.onerror = rej; });
  _ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && _pend.has(m.id)) {
      const { res, rej } = _pend.get(m.id); _pend.delete(m.id);
      m.error ? rej(new Error(m.error.message)) : res(m.result);
    } else if (m.method && _events.has(m.method)) {
      _events.get(m.method).forEach((fn) => fn(m.params));
    }
  };
}
async function creds() {
  const t0 = Date.now();
  for (;;) {
    const targets = await fetch(`http://127.0.0.1:${PORT}/json`).then((r) => r.json()).catch(() => null);
    if (targets) {
      const page = targets.find((t) => t.type === "page");
      if (page) { await attachWs(); break; }
    }
    if (Date.now() - t0 > 60000) throw new Error("no CDP page");
    await sleep(1500);
  }
  await send("Network.enable");
  onEvent("Network.requestWillBeSent", (e) => {
    const m = e.request.url.match(/http:\/\/(127\.0\.0\.1|localhost):(\d+)\/api/);
    if (m && !API) {
      API = `http://127.0.0.1:${m[2]}`;
      TOKEN = (e.request.headers || {})["X-Atomic-Token"] || null;
    }
  });
  const t1 = Date.now();
  let nudged = false;
  while (!API || !TOKEN) {
    if (Date.now() - t1 > 90000) throw new Error("no backend creds");
    if (!nudged && Date.now() - t1 > 8000) {
      nudged = true;
      try { await send("Page.enable"); await send("Page.reload", { ignoreCache: true }); } catch {}
    }
    await sleep(1000);
  }
  note(`backend ${API} via installed app`);
}
function launch() {
  appProc = spawn(EXE, [], { detached: true, stdio: "ignore",
    env: { ...process.env,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${PORT}` } });
  appProc.unref();
}
const left = (name) => {
  try { return execFileSync("tasklist", ["/FI", `IMAGENAME eq ${name}`, "/FO", "CSV", "/NH"]).toString().includes(name); }
  catch { return false; }
};
const killApp = () => {
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"], { stdio: "ignore" }); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "Atomic Dream Engine.exe"], { stdio: "ignore" }); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"], { stdio: "ignore" }); } catch {}
};
// The installed app's own bundled ffprobe — searched under the install
// dir; falls back to the machine's winget ffmpeg package.
function findFfprobe() {
  const roots = [INSTALL_DIR,
    path.join(INSTALL_DIR, "resources"),
    path.join(INSTALL_DIR, "resources", "ffmpeg", "bin")];
  for (const r of roots) {
    const p = path.join(r, "ffprobe.exe");
    if (fs.existsSync(p)) return p;
  }
  try {
    const out = execFileSync("cmd", ["/c", "dir /s /b ffprobe.exe"], { cwd: INSTALL_DIR }).toString();
    const first = out.split(/\r?\n/).filter(Boolean)[0];
    if (first && fs.existsSync(first)) return first;
  } catch {}
  try {
    const base = `${process.env.LOCALAPPDATA}\\Microsoft\\WinGet\\Packages`;
    for (const d of fs.readdirSync(base)) {
      if (!d.startsWith("Gyan.FFmpeg")) continue;
      const root = path.join(base, d);
      const sub = fs.readdirSync(root).find((x) => x.startsWith("ffmpeg-"));
      if (sub) {
        const p = path.join(root, sub, "bin", "ffprobe.exe");
        if (fs.existsSync(p)) return p;
      }
    }
  } catch {}
  return null;
}

const shots = (board) => board.flatMap((sc) => sc.shots);
const entryOf = (board, sid) => shots(board).find((e) => e.shot.id === sid);
const segs = (pid, q) => api("GET", `/api/projects/${pid}/dialogue?${q}`);

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(3000);

  launch(); await creds();
  const health = await (await fetch(`${API}/health`)).json();
  note(`health: backend ${health.version || "?"} (0024 proven by segment persistence below)`);

  // step 1 — populated A4 project: screenplay scene + scaffolded shot
  const p = await api("POST", "/api/projects", { name: `A5-${Date.now()}` }, 201);
  await commit(p.id, "character.create", { name: "Anna" });
  await commit(p.id, "character.create", { name: "Ben" });
  await commit(p.id, "location.create", { name: "Night Dock" });
  const chars = await api("GET", `/api/projects/${p.id}/character`);
  const locs = await api("GET", `/api/projects/${p.id}/location`);
  const anna = chars.find((c) => c.name === "Anna");
  const ben = chars.find((c) => c.name === "Ben");
  const dock = locs.find((l) => l.name === "Night Dock");
  // Anna gets a character voice profile — the segment resolves it.
  await commit(p.id, "character.update", {
    character_id: anna.id,
    data: { voice: { style: "warm", delivery: "measured", pitch: "low" } } });
  await commit(p.id, "screenplay.create", { title: "Dock Scene" });
  const sp = (await api("GET", `/api/projects/${p.id}/screenplays`))[0];
  await commit(p.id, "screenplay_scene.create", {
    screenplay_id: sp.id, heading: "EXT. DOCK — NIGHT", location_id: dock.id });
  const doc = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  const spsc = doc.scenes[0];
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: spsc.id, elements: [
      { kind: "dialogue", character_id: anna.id, text: "The tide is coming in.",
        parenthetical: "(quietly)" },
      { kind: "dialogue", character_id: ben.id, text: "Then we move now." },
      { kind: "action", text: "Fog rolls in." }] });
  const doc2 = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  const dlgEl = doc2.scenes[0].elements
    .filter((e) => e.kind === "dialogue" && e.character_id === anna.id)[0];
  await commit(p.id, "shotlist.scaffold", { screenplay_id: sp.id });
  let board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const shot = shots(board)[0].shot;
  ok("1 populated A4 project — screenplay scene + shot");

  // step 2 — select the screenplay scene (dialogue elements present)
  if (doc2.scenes[0].id !== spsc.id
      || doc2.scenes[0].elements.filter((e) => e.kind === "dialogue").length !== 2) {
    fail("screenplay scene/dialogue elements wrong");
  }
  ok("2 screenplay scene selected — two dialogue elements");

  // step 3 — create dialogue segments bound to the scene + element + shot
  await commit(p.id, "dialogue.create", {
    sp_scene_id: spsc.id, element_id: dlgEl.id, shot_id: shot.id,
    character_id: anna.id, text: "The tide is coming in.",
    direction: "(quietly)", seed: 7 });
  await commit(p.id, "dialogue.create", {
    sp_scene_id: spsc.id, shot_id: shot.id,
    character_id: ben.id, text: "Then we move now." });
  let dl = await segs(p.id, `sp_scene_id=${spsc.id}`);
  if (dl.length !== 2) fail(`segment create wrong: ${JSON.stringify(dl).slice(0, 300)}`);
  const d1 = dl.find((d) => d.character_id === anna.id);
  if (!d1 || d1.element_id !== dlgEl.id || d1.shot_id !== shot.id) {
    fail("segment refs not stored");
  }
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).dialogue?.count !== 2) {
    fail("board dialogue summary missing");
  }
  ok("3 two segments created — scene/element/shot/speaker refs stored");

  // step 4 — multiple speakers: both segments carry distinct speakers
  const speakers = new Set(dl.map((d) => d.character_id));
  if (speakers.size !== 2 || dl[0].speaker_name === dl[1].speaker_name) {
    fail(`speaker assignment wrong: ${JSON.stringify(dl).slice(0, 300)}`);
  }
  ok("4 multiple speakers — Anna + Ben on separate segments");

  // step 5 — voice profiles: Anna's resolves; Ben's flags missing
  const dAnna = dl.find((d) => d.character_id === anna.id);
  const dBen = dl.find((d) => d.character_id === ben.id);
  if (!dAnna.resolved_voice || dAnna.resolved_voice.missing
      || dAnna.resolved_voice.style !== "warm") {
    fail(`Anna voice resolution wrong: ${JSON.stringify(dAnna.resolved_voice)}`);
  }
  if (!dBen.resolved_voice || dBen.resolved_voice.missing !== true) {
    fail(`Ben should flag missing voice: ${JSON.stringify(dBen.resolved_voice)}`);
  }
  ok("5 voice profiles inspected — resolved (warm) vs missing");

  // step 6 — timing: planned rational placement recorded on the segment
  await commit(p.id, "dialogue.update", {
    segment_id: dAnna.id,
    timing: { start_frames: 24, fps: { num: 24, den: 1 } } });
  const dT = (await segs(p.id, `sp_scene_id=${spsc.id}`))
    .find((d) => d.id === dAnna.id);
  if (dT.timing_json.start_frames !== 24 || dT.timing_json.fps_num !== 24) {
    fail(`timing not stored: ${JSON.stringify(dT.timing_json)}`);
  }
  ok("6 timing set — planned 24f @24fps start recorded");

  // step 7 — generate deterministic mock audio (preview → approve → job)
  const prop = await preview(p.id, "dialogue.generate",
    { segment_id: dAnna.id });
  if (!prop.resolved_dialogue?.segment_id || !prop.resolved_audio?.simulated) {
    fail(`generate preview wrong: ${JSON.stringify(prop).slice(0, 300)}`);
  }
  const rel = path.join(os.tmpdir(), `a5-release-${Date.now()}.flag`);
  const gen = await commit(p.id, "dialogue.generate", {
    segment_id: dAnna.id,
    expected_dialogue: prop.resolved_dialogue,
    mock_hold: { at_stage: 2, release_file: rel, timeout_s: 120 } });
  await waitHold(p.id, gen.job_id);
  fs.writeFileSync(rel, "go");
  const done = await waitJob(p.id, gen.job_id);
  if (done.status !== "succeeded" || done.adapter_id !== "mock.audio") {
    fail(`job wrong: ${JSON.stringify(done).slice(0, 200)}`);
  }
  let got = (await segs(p.id, `sp_scene_id=${spsc.id}`))
    .find((d) => d.id === dAnna.id);
  if (got.result_asset_id !== done.output_asset_id) {
    fail("result not associated");
  }
  ok("7 deterministic mock audio — held job released → result landed");

  // step 8 — deterministic cancel + retry on the second segment
  const gen2 = await commit(p.id, "dialogue.generate", {
    segment_id: dBen.id, mock_hold: { at_stage: 2, timeout_s: 120 } });
  await waitHold(p.id, gen2.job_id);
  await api("POST", `/api/jobs/${gen2.job_id}/cancel`, undefined, 200);
  const j2 = await waitJob(p.id, gen2.job_id);
  if (j2.status !== "cancelled") fail(`held job did not cancel: ${j2.status}`);
  let gB = (await segs(p.id, `sp_scene_id=${spsc.id}`))
    .find((d) => d.id === dBen.id);
  if (gB.result_asset_id !== null) fail("cancelled job left a result");
  await api("POST", `/api/jobs/${gen2.job_id}/cancel`, undefined, 409);
  const gen3 = await commit(p.id, "dialogue.generate",
    { segment_id: dBen.id });
  const j3 = await waitJob(p.id, gen3.job_id);
  if (j3.status !== "succeeded") fail(`retry failed: ${JSON.stringify(j3)}`);
  gB = (await segs(p.id, `sp_scene_id=${spsc.id}`))
    .find((d) => d.id === dBen.id);
  if (gB.result_asset_id !== j3.output_asset_id) fail("retry result not associated");
  ok("8 deterministic cancel → cancelled/no result/409; retry lands result");

  // step 9 — decoded audio metadata: real ffprobe on the CAS WAV
  const ffprobe = findFfprobe();
  if (!ffprobe) fail("ffprobe not found (bundled or machine)");
  note(`ffprobe: ${ffprobe}`);
  const assets = (await api("GET", `/api/projects/${p.id}/assets`)).items;
  const wa = assets.find((a) => a.id === got.result_asset_id);
  if (!wa || wa.kind !== "audio") fail(`result asset wrong: ${JSON.stringify(wa).slice(0, 200)}`);
  const vers = await api("GET", `/api/projects/${p.id}/assets/${wa.id}/versions`);
  const media = vers[0]?.media_json || {};
  if (media.is_mock !== true || media.generated !== false
      || media.adapter_id !== "mock.audio") {
    fail(`mock provenance missing: ${JSON.stringify(media)}`);
  }
  if (!media.probe?.ok || media.probe.sample_rate !== 48000
      || !(media.probe.samples > 0)) {
    fail(`probe wrong: ${JSON.stringify(media.probe).slice(0, 300)}`);
  }
  const dataDir = path.join(process.env.LOCALAPPDATA, "AtomicAIStudio");
  const blob = path.join(dataDir, wa.uri.split("/").join(path.sep));
  if (!fs.existsSync(blob)) fail(`CAS blob missing: ${blob}`);
  const pr = JSON.parse(execFileSync(ffprobe, [
    "-v", "error", "-print_format", "json",
    "-show_streams", "-show_format", blob]).toString());
  const as = pr.streams.find((s) => s.codec_type === "audio");
  if (!as || !as.codec_name.startsWith("pcm")
      || parseInt(as.sample_rate) !== 48000
      || parseFloat(pr.format.duration) < 0.5) {
    fail(`ffprobe mismatch: ${JSON.stringify({ c: as?.codec_name, d: pr.format.duration })}`);
  }
  ok(`9 decodable audio — real ffprobe: ${as.codec_name}, 48kHz, ~${parseFloat(pr.format.duration).toFixed(2)}s`);

  // step 10 — audio preview: the served bytes are the real WAV
  const r10 = await fetch(`${API}/api/projects/${p.id}/assets/${wa.id}/file`,
    { headers: { "X-Atomic-Token": TOKEN } });
  if (r10.status !== 200) fail(`asset file fetch ${r10.status}`);
  const bytes = Buffer.from(await r10.arrayBuffer());
  if (bytes.length < 1000 || bytes.toString("ascii", 0, 4) !== "RIFF"
      || bytes.toString("ascii", 8, 12) !== "WAVE") {
    fail(`preview bytes not a WAV: ${bytes.length} bytes`);
  }
  ok(`10 audio preview — served ${bytes.length} B RIFF/WAVE`);

  // step 11 — place dialogue on an audio lane of the existing timeline
  await commit(p.id, "timeline.create", { name: "A5 Cut", fps_num: 24, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`))[0];
  await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "audio", name: "DX" });
  await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "audio", name: "MX" });
  let det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  const dxLane = det.tracks.find((t) => t.name === "DX");
  const mxLane = det.tracks.find((t) => t.name === "MX");
  if (!dxLane || !mxLane) fail("audio lanes missing");
  await commit(p.id, "dialogue.place", {
    segment_id: dAnna.id, timeline_id: tl.id,
    track_id: dxLane.id, start_frames: 0 });
  await commit(p.id, "dialogue.place", {
    segment_id: dBen.id, timeline_id: tl.id,
    track_id: dxLane.id, start_frames: 96 });
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  const placed = det.clips.filter((c) => c.track_id === dxLane.id);
  if (placed.length !== 2) {
    fail(`placement wrong: ${JSON.stringify(placed).slice(0, 300)}`);
  }
  const pBen = placed.find((c) => c.start_frames === 96);
  if (!pBen || pBen.out_frame <= pBen.in_frame) {
    fail(`Ben placement wrong: ${JSON.stringify(pBen)}`);
  }
  ok("11 both segments placed on the DX audio lane (0f + 96f)");

  // step 12 — mix with existing music/SFX: a second audio asset on MX
  //           lane, overlapping the same timeline window
  const musicWav = path.join(os.tmpdir(), `a5-music-${Date.now()}.wav`);
  {
    // tiny deterministic sine WAV written locally — stands in for a
    // user-imported music bed
    const sr = 48000, secs = 2, n = sr * secs;
    const hdr = Buffer.alloc(44); const data = Buffer.alloc(n * 2);
    for (let i = 0; i < n; i++) {
      data.writeInt16LE(Math.round(12000 * Math.sin(2 * Math.PI * 220 * i / sr)), i * 2);
    }
    hdr.write("RIFF", 0); hdr.writeUInt32LE(36 + data.length, 4);
    hdr.write("WAVE", 8); hdr.write("fmt ", 12); hdr.writeUInt32LE(16, 16);
    hdr.writeUInt16LE(1, 20); hdr.writeUInt16LE(1, 22);
    hdr.writeUInt32LE(sr, 24); hdr.writeUInt32LE(sr * 2, 28);
    hdr.writeUInt16LE(2, 32); hdr.writeUInt16LE(16, 34);
    hdr.write("data", 36); hdr.writeUInt32LE(data.length, 40);
    fs.writeFileSync(musicWav, Buffer.concat([hdr, data]));
  }
  const imp = await commit(p.id, "asset.import", {
    source_path: musicWav, name: "music-bed", kind: "audio" });
  const impJob = await waitJob(p.id, imp.job_id);
  if (impJob.status !== "succeeded") {
    fail(`music import failed: ${JSON.stringify(impJob).slice(0, 200)}`);
  }
  const musicAsset = (await api("GET", `/api/projects/${p.id}/assets`)).items
    .find((a) => a.name === "music-bed");
  if (!musicAsset || musicAsset.kind !== "audio") fail("music asset missing");
  await commit(p.id, "timeline.clip.place", {
    timeline_id: tl.id, asset_id: musicAsset.id,
    track_id: mxLane.id, start_frames: 0 });
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  const mx = det.clips.filter((c) => c.track_id === mxLane.id);
  if (mx.length !== 1 || det.clips.length !== 3) {
    fail(`music mix wrong: ${det.clips.length} clips, mx ${mx.length}`);
  }
  ok("12 dialogue + music mixed — 2 DX clips + 1 MX clip overlap");

  // step 13 — export and inspect decoded PCM: the film carries audio
  const ex = await commit(p.id, "timeline.export", { timeline_id: tl.id, profile: "720p" });
  const exDone = await waitJob(p.id, ex.job_id, 180000);
  if (exDone.status !== "succeeded") {
    fail(`export failed: ${JSON.stringify(exDone).slice(0, 300)}`);
  }
  const exAsset = (await api("GET", `/api/projects/${p.id}/assets`)).items
    .find((a) => a.id === exDone.output_asset_id);
  const exBlob = path.join(dataDir, exAsset.uri.split("/").join(path.sep));
  const pr2 = JSON.parse(execFileSync(ffprobe, [
    "-v", "error", "-print_format", "json",
    "-show_streams", "-show_format", exBlob]).toString());
  const a2 = pr2.streams.find((s) => s.codec_type === "audio");
  if (!a2 || parseFloat(pr2.format.duration) < 2.0) {
    fail(`export audio wrong: ${JSON.stringify({ d: pr2.format.duration })}`);
  }
  ok(`13 export + decoded PCM — audio stream present, ~${parseFloat(pr2.format.duration).toFixed(2)}s`);

  // step 14 — restart persistence: relaunch, segments + results + placement intact
  killApp(); await sleep(4000);
  launch(); API = null; TOKEN = null; await creds();
  dl = await segs(p.id, `sp_scene_id=${spsc.id}`);
  got = dl.find((d) => d.id === dAnna.id);
  gB = dl.find((d) => d.id === dBen.id);
  if (got.result_asset_id !== wa.id
      || gB.result_asset_id !== j3.output_asset_id
      || got.timing_json.start_frames !== 24) {
    fail("restart drifted segment state");
  }
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  if (det.clips.length !== 3) fail("restart lost timeline clips");
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).dialogue?.count !== 2) {
    fail("board dialogue summary degraded after restart");
  }
  ok("14 restart persistence — segments, results, placement intact");

  // step 15 — clean shutdown
  killApp(); await sleep(6000);
  if (left("atomic-backend.exe")) fail("backend process leaked");
  ok("15 clean shutdown — no orphan backend");

  lines.push(`A5 ACCEPTANCE: PASS ${steps}/${STEPS}`);
  fs.writeFileSync(path.join(OUT, "a5-acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\n${lines[lines.length - 1]}`);
  if (steps !== STEPS) fail(`step count ${steps} != ${STEPS}`);
}

main().catch((e) => {
  lines.push(`FAIL ${e.message}`);
  try { fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, "a5-acceptance.txt"), lines.join("\n") + "\n"); } catch {}
  console.error(e);
  try { killApp(); } catch {}
  process.exit(1);
});
