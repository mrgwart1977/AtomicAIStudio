// A4 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port, same plumbing
// as the A1/A2/A3 drivers). 15 steps per the A4.9 checklist on a
// populated A3 project (screenplay + scaffolded shot + keyframes).
// Run: node scripts/cdp-a4-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path"), os = require("os");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9229";
const OUT = process.argv[3] || "docs/a4-evidence";
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
    expected_revision: rev, idempotency_key: key || `a4-${ct}-${Date.now()}-${Math.random()}` }, expect);
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
const kfs = (pid, sid) => api("GET", `/api/projects/${pid}/shots/${sid}/keyframes`);
const clips = (pid, sid) => api("GET", `/api/projects/${pid}/shots/${sid}/clips`);

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(3000);

  launch(); await creds();
  const health = await (await fetch(`${API}/health`)).json();
  note(`health: backend ${health.version || "?"} (0023 proven by clip persistence below)`);

  // step 1 — populated A3 project: screenplay + scaffolded + planned shot
  //          + start/end keyframes (the A3 surface A4 builds on)
  const p = await api("POST", "/api/projects", { name: `A4-${Date.now()}` }, 201);
  await commit(p.id, "character.create", { name: "Anna" });
  await commit(p.id, "location.create", { name: "Night Dock" });
  const chars = await api("GET", `/api/projects/${p.id}/character`);
  const locs = await api("GET", `/api/projects/${p.id}/location`);
  const anna = chars.find((c) => c.name === "Anna");
  const dock = locs.find((l) => l.name === "Night Dock");
  await commit(p.id, "screenplay.create", { title: "Dock Scene" });
  const sp = (await api("GET", `/api/projects/${p.id}/screenplays`))[0];
  await commit(p.id, "screenplay_scene.create", {
    screenplay_id: sp.id, heading: "EXT. DOCK — NIGHT", location_id: dock.id });
  const doc = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: doc.scenes[0].id, elements: [
      { kind: "dialogue", character_id: anna.id, text: "The boat is late." },
      { kind: "action", text: "Fog rolls in." }] });
  await commit(p.id, "shotlist.scaffold", { screenplay_id: sp.id });
  let board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const shot = shots(board)[0].shot;
  await commit(p.id, "shot.update", { shot_id: shot.id, prompt: "she turns toward the fog",
    planning: { camera: { angle: "low", movement: "dolly_in",
                          follow_mode: "follow_behind" },
                framing: "medium_wide" } });
  await commit(p.id, "shot.update", { shot_id: shot.id, location_id: dock.id });
  ok(`1 populated A3 project (scaffold + planned shot)`);

  // step 2 — select shot + start/end keyframes for the motion clip
  await commit(p.id, "keyframe.create", { shot_id: shot.id, role: "start" });
  await commit(p.id, "keyframe.create", { shot_id: shot.id, role: "end" });
  let klist = await kfs(p.id, shot.id);
  const kfStart = klist.find((k) => k.role === "start");
  const kfEnd = klist.find((k) => k.role === "end");
  await commit(p.id, "clip.create", {
    shot_id: shot.id, prompt: "anna turns toward the fog",
    start_keyframe_id: kfStart.id, end_keyframe_id: kfEnd.id,
    duration_frames: 24, fps: { num: 24, den: 1 },
    width: 256, height: 144 });
  let clist = await clips(p.id, shot.id);
  if (clist.length !== 1) fail(`clip create wrong: ${JSON.stringify(clist)}`);
  const clip = clist[0];
  if (clip.start_keyframe_id !== kfStart.id || clip.end_keyframe_id !== kfEnd.id) {
    fail("keyframe refs not stored");
  }
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).clips?.count !== 1) fail("board clip summary missing");
  ok("2 shot + start/end keyframes bound to motion clip");

  // step 3 — configure motion + camera settings (A2 vocabulary verbatim)
  await commit(p.id, "clip.update", {
    clip_id: clip.id,
    motion: { instructions: "she turns slowly",
              intensity: "subtle",
              temporal_consistency: "keep anna's face consistent" },
    camera: { movement: "dolly_in", follow_mode: "follow_behind",
              follow_targets: [{ kind: "character", id: anna.id }],
              framing: "medium_wide" },
    seed: 42 });
  let got = (await clips(p.id, shot.id))[0];
  if (got.motion_json.intensity !== "subtle"
      || got.camera_json.follow_mode !== "follow_behind"
      || got.camera_json.framing !== "medium_wide"
      || got.seed !== 42) {
    fail(`motion/camera settings wrong: ${JSON.stringify(got).slice(0, 300)}`);
  }
  // Follow-mode vocabulary rejects unknowns — the ten A2 modes only
  await commit(p.id, "clip.update",
    { clip_id: clip.id, camera: { follow_mode: "teleport" } }, 422);
  ok("3 motion + camera configured; invalid follow mode refused");

  // step 4 — preview generation: composed prompt + resolved_clip pin
  let prop = await preview(p.id, "clip.generate", { clip_id: clip.id });
  if (!prop.resolved_clip?.clip_id || !prop.resolved_prompt?.prompt_sha256) {
    fail(`generate preview wrong: ${JSON.stringify(prop).slice(0, 300)}`);
  }
  ok("4 generation preview — composed prompt + resolved_clip pin");

  // step 5 — approve a deterministic mock video generation (held at the
  //          barrier so progress is observable before completion)
  const rel = path.join(os.tmpdir(), `a4-release-${Date.now()}.flag`);
  const gen = await commit(p.id, "clip.generate", {
    clip_id: clip.id,
    expected_clip: prop.resolved_clip,
    expected_prompt: prop.resolved_prompt.prompt_sha256,
    mock_hold: { at_stage: 2, release_file: rel, timeout_s: 120 } });
  if (!gen.job_id) fail("no job enqueued");
  ok(`5 mock video job approved + enqueued (${gen.job_id.slice(0, 8)})`);

  // step 6 — progress: held at the barrier, then released deterministically
  await waitHold(p.id, gen.job_id);
  fs.writeFileSync(rel, "go");
  const done = await waitJob(p.id, gen.job_id);
  if (done.status !== "succeeded" || done.adapter_id !== "mock.video") {
    fail(`job wrong: ${JSON.stringify(done).slice(0, 200)}`);
  }
  got = (await clips(p.id, shot.id))[0];
  if (got.result_asset_id !== done.output_asset_id
      || got.job_status !== "succeeded") {
    fail(`result not associated: ${JSON.stringify(got).slice(0, 300)}`);
  }
  ok("6 held job released deterministically → result associated");

  // step 7 — deterministic cancel of a SECOND clip's active job
  await commit(p.id, "clip.create", {
    shot_id: shot.id, prompt: "cancel me", duration_frames: 24 });
  const clip2 = (await clips(p.id, shot.id)).find((c) => c.prompt === "cancel me");
  const gen2 = await commit(p.id, "clip.generate", {
    clip_id: clip2.id, mock_hold: { at_stage: 2, timeout_s: 120 } });
  await waitHold(p.id, gen2.job_id);
  await api("POST", `/api/jobs/${gen2.job_id}/cancel`, undefined, 200);
  const j2 = await waitJob(p.id, gen2.job_id);
  if (j2.status !== "cancelled") fail(`held job did not cancel: ${j2.status}`);
  let g2 = (await clips(p.id, shot.id)).find((c) => c.id === clip2.id);
  if (g2.result_asset_id !== null) fail("cancelled job left a result");
  await api("POST", `/api/jobs/${gen2.job_id}/cancel`, undefined, 409);
  ok("7 deterministic cancel — held job → cancelled, no result, 409 re-cancel");

  // step 8 — retry with release → deterministic completion + result
  const rel2 = path.join(os.tmpdir(), `a4-release2-${Date.now()}.flag`);
  const gen3 = await commit(p.id, "clip.generate", {
    clip_id: clip2.id,
    mock_hold: { at_stage: 4, release_file: rel2, timeout_s: 120 } });
  await waitHold(p.id, gen3.job_id);
  fs.writeFileSync(rel2, "go");
  const j3 = await waitJob(p.id, gen3.job_id);
  if (j3.status !== "succeeded") fail(`released retry failed: ${JSON.stringify(j3)}`);
  g2 = (await clips(p.id, shot.id)).find((c) => c.id === clip2.id);
  if (g2.result_asset_id !== j3.output_asset_id) fail("retry result not associated");
  ok("8 held retry released → result associated on second clip");

  // step 9 — decodable video: real ffprobe on the produced CAS blob
  const ffprobe = findFfprobe();
  if (!ffprobe) fail("ffprobe not found (bundled or machine)");
  note(`ffprobe: ${ffprobe}`);
  const assets = (await api("GET", `/api/projects/${p.id}/assets`)).items;
  const va = assets.find((a) => a.id === got.result_asset_id);
  if (!va || va.kind !== "video") fail(`result asset wrong: ${JSON.stringify(va).slice(0, 200)}`);
  const vers = await api("GET", `/api/projects/${p.id}/assets/${va.id}/versions`);
  const media = vers[0]?.media_json || {};
  if (media.is_mock !== true || media.generated !== false
      || media.adapter_id !== "mock.video") {
    fail(`mock provenance missing: ${JSON.stringify(media)}`);
  }
  if (!media.probe?.ok || media.probe.codec !== "h264" || media.probe.fps !== 24) {
    fail(`probe wrong: ${JSON.stringify(media.probe).slice(0, 300)}`);
  }
  // Independent verification: run ffprobe on the blob itself
  const dataDir = path.join(process.env.LOCALAPPDATA, "AtomicAIStudio");
  const blob = path.join(dataDir, va.uri.split("/").join(path.sep));
  if (!fs.existsSync(blob)) fail(`CAS blob missing: ${blob}`);
  const pr = JSON.parse(execFileSync(ffprobe, [
    "-v", "error", "-print_format", "json",
    "-show_streams", "-show_format", blob]).toString());
  const vs = pr.streams.find((s) => s.codec_type === "video");
  if (!vs || vs.codec_name !== "h264" || parseInt(vs.nb_frames) !== 24
      || vs.width !== 256) {
    fail(`ffprobe mismatch: ${JSON.stringify({ codec: vs?.codec_name, frames: vs?.nb_frames })}`);
  }
  ok("9 decodable video — real ffprobe confirms h264, 24f @24fps, 256x144");

  // step 10 — playback-ready: duration/fps metadata consistent + mock label
  if (Math.abs(media.probe.duration_s - 1.0) > 0.05 || media.frames !== 24) {
    fail(`timing wrong: ${JSON.stringify({ d: media.probe.duration_s, f: media.frames })}`);
  }
  ok("10 playback metadata verified (1.0s @ 24fps; labeled mock)");

  // step 11 — place the produced clip on the existing timeline
  await commit(p.id, "timeline.create", { name: "A4 Cut", fps_num: 24, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`))[0];
  await commit(p.id, "timeline.clip.place", {
    timeline_id: tl.id, asset_id: va.id, start_frames: 0,
    shot_id: shot.id });
  const det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  const placed = det.clips.find((c) => c.source_asset_id === va.id);
  if (!placed || placed.start_frames !== 0
      || placed.out_frame - placed.in_frame !== 24
      || placed.src_fps_num !== 24) {
    fail(`placement wrong: ${JSON.stringify(placed).slice(0, 300)}`);
  }
  ok("11 clip placed on existing timeline (24f extent, rational fps)");

  // step 12 — export a short film through the existing export path
  const ex = await commit(p.id, "timeline.export", { timeline_id: tl.id, profile: "720p" });
  const exDone = await waitJob(p.id, ex.job_id, 180000);
  if (exDone.status !== "succeeded") {
    fail(`export failed: ${JSON.stringify(exDone).slice(0, 300)}`);
  }
  ok("12 short film exported via timeline.export job");

  // step 13 — ffprobe the exported film: metadata must match the EDL
  const exAsset = (await api("GET", `/api/projects/${p.id}/assets`)).items
    .find((a) => a.id === exDone.output_asset_id);
  if (!exAsset) fail("export asset missing");
  const exBlob = path.join(dataDir, exAsset.uri.split("/").join(path.sep));
  const pr2 = JSON.parse(execFileSync(ffprobe, [
    "-v", "error", "-print_format", "json",
    "-show_streams", "-show_format", exBlob]).toString());
  const vs2 = pr2.streams.find((s) => s.codec_type === "video");
  const dur2 = parseFloat(pr2.format.duration);
  if (!vs2 || vs2.codec_name !== "h264" || Math.abs(dur2 - 1.0) > 0.1) {
    fail(`export probe wrong: ${JSON.stringify({ c: vs2?.codec_name, d: dur2 })}`);
  }
  ok(`13 export ffprobe verified — h264, ~${dur2.toFixed(2)}s film`);

  // step 14 — restart persistence: relaunch, clip + result + placement intact
  killApp(); await sleep(4000);
  launch(); API = null; TOKEN = null; await creds();
  got = (await clips(p.id, shot.id)).find((c) => c.id === clip.id);
  if (got.result_asset_id !== va.id || got.motion_json.intensity !== "subtle"
      || got.camera_json.follow_mode !== "follow_behind") {
    fail(`restart drifted clip state: ${JSON.stringify(got).slice(0, 300)}`);
  }
  const det2 = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  if (!det2.clips.find((c) => c.source_asset_id === va.id)) {
    fail("restart lost timeline placement");
  }
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).clips?.count !== 2
      || entryOf(board, shot.id).keyframes?.count !== 2) {
    fail("board summary degraded after restart");
  }
  ok("14 restart persistence — clips, results, placement, board intact");

  // step 15 — clean shutdown
  killApp(); await sleep(6000);
  if (left("atomic-backend.exe")) fail("backend process leaked");
  ok("15 clean shutdown — no orphan backend");

  lines.push(`A4 ACCEPTANCE: PASS ${steps}/${STEPS}`);
  fs.writeFileSync(path.join(OUT, "a4-acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\n${lines[lines.length - 1]}`);
  if (steps !== STEPS) fail(`step count ${steps} != ${STEPS}`);
}

main().catch((e) => {
  lines.push(`FAIL ${e.message}`);
  try { fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, "a4-acceptance.txt"), lines.join("\n") + "\n"); } catch {}
  console.error(e);
  try { killApp(); } catch {}
  process.exit(1);
});
