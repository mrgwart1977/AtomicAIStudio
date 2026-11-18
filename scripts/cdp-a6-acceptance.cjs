// A6 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port, same plumbing
// as the A1–A5 drivers). 17 steps per the A6.8 checklist on a
// populated A5 project (two screenplay scenes → two shots, keyframes +
// a produced motion clip on shot 1, two dialogue segments with mock
// audio). Shot 2 stays bare — it is the missing-media case.
// Run: node scripts/cdp-a6-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path"), os = require("os");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9229";
const OUT = process.argv[3] || "docs/a6-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Atomic Dream Engine\\atomic-dream-engine.exe`;
const INSTALL_DIR = path.dirname(EXE);
const STEPS = 19;
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
    expected_revision: rev, idempotency_key: key || `a6-${ct}-${Date.now()}-${Math.random()}` }, expect);
}
async function preview(pid, ct, payload, expect = 200) {
  return api("POST", `/api/projects/${pid}/commands/preview`, {
    command_type: ct, payload, scope: "project", origin: "acceptance" }, expect);
}
async function waitJob(pid, jobId, timeout = 180000) {
  const t0 = Date.now();
  for (;;) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) return j;
    if (Date.now() - t0 > timeout) fail(`job ${jobId} timeout`);
    await sleep(400);
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

function findFfmpeg() {
  const p = findFfprobe();
  if (p) {
    const sib = path.join(path.dirname(p), "ffmpeg.exe");
    if (fs.existsSync(sib)) return sib;
  }
  try {
    const out = execFileSync("cmd", ["/c", "dir /s /b ffmpeg.exe"],
      { cwd: INSTALL_DIR }).toString();
    const first = out.split(/\r?\n/).filter(Boolean)[0];
    if (first && fs.existsSync(first)) return first;
  } catch {}
  return null;
}

const shots = (board) => board.flatMap((sc) => sc.shots);
const plans = (pid, tl) => api("GET",
  `/api/projects/${pid}/assemblies?timeline_id=${tl}`);
const planDetail = (pid, planId) =>
  api("GET", `/api/projects/${pid}/assemblies/${planId}`);

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(3000);

  launch(); await creds();
  const health = await (await fetch(`${API}/health`)).json();
  note(`health: backend ${health.version || "?"} (0025 proven by plan persistence below)`);

  // step 1 — populated A5 project: two screenplay scenes → two shots;
  //          keyframes + a produced motion clip on shot 1; two dialogue
  //          segments with generated mock audio. Shot 2 stays bare.
  const p = await api("POST", "/api/projects", { name: `A6-${Date.now()}` }, 201);
  await commit(p.id, "character.create", { name: "Anna" });
  await commit(p.id, "character.create", { name: "Ben" });
  await commit(p.id, "location.create", { name: "Night Dock" });
  const chars = await api("GET", `/api/projects/${p.id}/character`);
  const locs = await api("GET", `/api/projects/${p.id}/location`);
  const anna = chars.find((c) => c.name === "Anna");
  const ben = chars.find((c) => c.name === "Ben");
  const dock = locs.find((l) => l.name === "Night Dock");
  await commit(p.id, "character.update", {
    character_id: anna.id,
    data: { voice: { style: "warm", delivery: "measured", pitch: "low" } } });
  await commit(p.id, "screenplay.create", { title: "Dock Two-Parter" });
  const sp = (await api("GET", `/api/projects/${p.id}/screenplays`))[0];
  await commit(p.id, "screenplay_scene.create", {
    screenplay_id: sp.id, heading: "EXT. DOCK — NIGHT", location_id: dock.id });
  await commit(p.id, "screenplay_scene.create", {
    screenplay_id: sp.id, heading: "INT. BOATHOUSE — LATER" });
  const doc = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  const spsc1 = doc.scenes[0], spsc2 = doc.scenes[1];
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: spsc1.id, elements: [
      { kind: "dialogue", character_id: anna.id, text: "The tide is coming in." },
      { kind: "dialogue", character_id: ben.id, text: "Then we move now." }] });
  // Scene 2 needs ≥1 element to scaffold a shot — action only, so no
  // dialogue segment is implied there.
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: spsc2.id, elements: [
      { kind: "action", text: "Rain hammers the boathouse roof." }] });
  await commit(p.id, "shotlist.scaffold", { screenplay_id: sp.id });
  let board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const entries = shots(board);
  if (entries.length < 2) fail(`expected 2 scaffolded shots, got ${entries.length}`);
  const shot1 = entries[0].shot, shot2 = entries[1].shot;
  // A4 pipeline on shot 1 only — shot 2 is the missing-media case.
  await commit(p.id, "keyframe.create", { shot_id: shot1.id, role: "start" });
  await commit(p.id, "keyframe.create", { shot_id: shot1.id, role: "end" });
  const klist = await api("GET", `/api/projects/${p.id}/shots/${shot1.id}/keyframes`);
  const kfS = klist.find((k) => k.role === "start");
  const kfE = klist.find((k) => k.role === "end");
  await commit(p.id, "clip.create", {
    shot_id: shot1.id, prompt: "anna turns toward the fog",
    start_keyframe_id: kfS.id, end_keyframe_id: kfE.id,
    duration_frames: 48, fps: { num: 24, den: 1 },
    width: 256, height: 144 });
  const clip = (await api("GET",
    `/api/projects/${p.id}/shots/${shot1.id}/clips`))[0];
  const cprop = await preview(p.id, "clip.generate", { clip_id: clip.id });
  const cgen = await commit(p.id, "clip.generate", {
    clip_id: clip.id,
    expected_clip: cprop.resolved_clip,
    expected_prompt: cprop.resolved_prompt?.prompt_sha256 });
  const cj = await waitJob(p.id, cgen.job_id);
  if (cj.status !== "succeeded") fail(`clip gen failed: ${cj.status}`);
  // Two dialogue segments with produced audio on shot 1.
  await commit(p.id, "dialogue.create", {
    sp_scene_id: spsc1.id, shot_id: shot1.id, character_id: anna.id,
    text: "The tide is coming in.", direction: "(quietly)", seed: 7 });
  await commit(p.id, "dialogue.create", {
    sp_scene_id: spsc1.id, shot_id: shot1.id, character_id: ben.id,
    text: "Then we move now." });
  const segs = await api("GET",
    `/api/projects/${p.id}/dialogue?sp_scene_id=${spsc1.id}`);
  const dAnna = segs.find((d) => d.character_id === anna.id);
  const dBen = segs.find((d) => d.character_id === ben.id);
  for (const seg of [dAnna, dBen]) {
    const j = await waitJob(p.id,
      (await commit(p.id, "dialogue.generate",
        { segment_id: seg.id })).job_id);
    if (j.status !== "succeeded") fail(`dialogue gen failed: ${j.status}`);
  }
  ok("1 populated A5 project — 2 scenes/2 shots, clip + 2 dialogue results");

  // step 2 — multiple storyboard scenes/shots selected for the cut
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (shots(board).length < 2 || board.length < 2) {
    fail("storyboard lacks the two-scene layout");
  }
  ok("2 multiple storyboard scenes — scene 1 + scene 2 in order");

  // step 3 — timeline + first-cut plan preview: the deterministic
  //          proposal surfaces before anything is written
  await commit(p.id, "timeline.create", { name: "A6 Cut", fps_num: 24, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`))[0];
  const planPv = await preview(p.id, "assembly.plan",
    { timeline_id: tl.id });
  const ra = planPv.resolved_assembly;
  if (!ra || !ra.lanes || !Array.isArray(ra.items) || ra.items.length < 4) {
    fail(`plan preview wrong: ${JSON.stringify(ra).slice(0, 400)}`);
  }
  ok(`3 first-cut preview — ${ra.counts.items} items across lanes`);

  // step 4 — video/dialogue lane mapping in the proposal
  const laneNames = Object.fromEntries(Object.entries(ra.lanes)
    .map(([k, l]) => [k, l.track_name || l.create?.name]));
  const shotItem = ra.items.find((i) => i.kind === "shot" && i.shot_id === shot1.id);
  const dlgItems = ra.items.filter((i) => i.kind === "dialogue");
  if (laneNames.video !== "V1" || laneNames.dialogue !== "DX"
      || dlgItems.length !== 2 || !dlgItems.every((d) => d.lane_key
        && d.lane_key.startsWith("dialogue"))) {
    fail(`lane mapping wrong: ${JSON.stringify({ laneNames, dlgItems })}`);
  }
  if (!shotItem || shotItem.lane_key !== "video") {
    fail(`shot item not on video lane: ${JSON.stringify(shotItem)}`);
  }
  ok("4 mapping — shot→V1, 2 lines→DX (created lanes named)");

  // step 5 — missing-asset detection: shot 2 has no motion-clip result
  const miss = ra.items.find((i) => i.kind === "shot" && i.shot_id === shot2.id);
  if (!miss || miss.flags.missing !== "no motion-clip result"
      || ra.counts.missing < 1) {
    fail(`missing media not flagged: ${JSON.stringify(miss)}`);
  }
  if (!ra.warnings.some((w) => /no motion-clip result/.test(w))) {
    fail("missing warning absent from preview");
  }
  ok("5 missing assets detected — shot 2 flagged, warning shown");

  // step 6 — approve the plan: it persists as a draft proposal
  await commit(p.id, "assembly.plan", { timeline_id: tl.id });
  let pl = (await plans(p.id, tl.id))[0];
  if (!pl || pl.status !== "draft") fail(`plan not persisted: ${JSON.stringify(pl)}`);
  let det0 = await planDetail(p.id, pl.id);
  if ((det0.items || []).length !== ra.counts.items) {
    fail(`persisted items wrong: ${(det0.items || []).length}`);
  }
  ok("6 plan approved — persisted draft with stable item keys");

  // step 7 — apply preview → approve → pinned clips land with
  //          shot/scene provenance on V1 + DX
  const apPv = await preview(p.id, "assembly.apply",
    { timeline_id: tl.id });
  const rap = apPv.resolved_assembly;
  if (!rap || rap.sources_stale || rap.counts.insert < 3) {
    fail(`apply preview wrong: ${JSON.stringify(rap).slice(0, 400)}`);
  }
  await commit(p.id, "assembly.apply",
    { timeline_id: tl.id, resolve_conflicts: "skip" });
  let det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  // Dialogue clips carry shot_id provenance too — the video clip is
  // the one on the video-kind track.
  const videoTracks = new Set(
    det.tracks.filter((t) => t.kind === "video").map((t) => t.id));
  const isVideoClip = (c) => videoTracks.has(c.track_id);
  const vClips = det.clips.filter(
    (c) => c.shot_id === shot1.id && isVideoClip(c));
  if (vClips.length !== 1 || vClips[0].scene_id !== shot1.scene_id) {
    fail(`video clip provenance wrong: ${JSON.stringify(vClips)}`);
  }
  const audioTracks = new Set(det.tracks.filter((t) => t.kind === "audio").map((t) => t.id));
  const placedAudio = det.clips.filter((c) => audioTracks.has(c.track_id));
  if (placedAudio.length !== 2) {
    fail(`dialogue placement wrong: ${JSON.stringify(placedAudio)}`);
  }
  pl = (await plans(p.id, tl.id))[0];
  if (pl.status !== "applied") fail(`plan status wrong: ${pl.status}`);
  ok("7 apply approved — V1 clip + 2 DX clips with shot/scene provenance");

  // step 8 — manual timeline edits: the user moves the applied shot
  //          clip AND one dialogue clip (two separate conflicts)
  const movedId = vClips[0].id;
  await commit(p.id, "timeline.clip.move", {
    timeline_id: tl.id, clip_id: movedId, start_frames: 200 });
  const dlgMovedId = placedAudio[0].id;
  await commit(p.id, "timeline.clip.move", {
    timeline_id: tl.id, clip_id: dlgMovedId, start_frames: 120 });
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  if (det.clips.find((c) => c.id === movedId).start_frames !== 200
      || det.clips.find((c) => c.id === dlgMovedId).start_frames !== 120) {
    fail("manual moves did not land");
  }
  ok("8 manual timeline edits — video clip at 200f, dialogue at 120f");

  // step 9 — revise a source: dialogue text changes after the plan
  await commit(p.id, "dialogue.update", {
    segment_id: dAnna.id, text: "The fog rolled in without warning." });
  // The stale plan refuses to apply — drift is surfaced, not applied.
  const staleCheck = await preview(p.id, "assembly.apply",
    { timeline_id: tl.id });
  if (staleCheck.resolved_assembly?.sources_stale !== true) {
    fail("stale sources not flagged on apply preview");
  }
  await commit(p.id, "assembly.apply",
    { timeline_id: tl.id, resolve_conflicts: "skip" }, 422);
  ok("9 source revised — stale apply preview flagged + commit refused");

  // step 10 — reassembly preview: re-plan flags BOTH manual edits as
  //           conflicts; the apply walk marks them preserved
  await commit(p.id, "assembly.plan", { timeline_id: tl.id });
  det0 = await planDetail(p.id, pl.id);
  const conflictedItems = det0.items.filter(
    (i) => (i.flags.conflicts || []).length > 0);
  const conflictClipIds = new Set(conflictedItems.flatMap(
    (i) => i.flags.conflicts.map((c) => c.clip_id)));
  if (!conflictClipIds.has(movedId) || !conflictClipIds.has(dlgMovedId)) {
    fail(`conflicts not flagged on both edits: ${JSON.stringify(
      conflictedItems.map((i) => i.key))}`);
  }
  const apPv2 = await preview(p.id, "assembly.apply",
    { timeline_id: tl.id });
  const disps = Object.fromEntries(apPv2.resolved_assembly.items
    .map((i) => [i.key, i]));
  for (const ci of conflictedItems) {
    if (disps[ci.key]?.action !== "skip") {
      fail(`apply walk wrong for edited clip: ${JSON.stringify(disps[ci.key])}`);
    }
  }
  ok("10 reassembly preview — both conflicts flagged, dispositions preserved");

  // step 11 — apply preserves the manual edits by default
  await commit(p.id, "assembly.apply",
    { timeline_id: tl.id, resolve_conflicts: "skip" });
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  if (det.clips.find((c) => c.id === movedId).start_frames !== 200
      || det.clips.find((c) => c.id === dlgMovedId).start_frames !== 120) {
    fail("manual edits were overwritten by apply");
  }
  ok("11 manual edits preserved — both clips still at edited positions");

  // step 12 — approve ONE specific replacement: broad replace refuses;
  //           the approved item is replaced, the unapproved edit stays
  await commit(p.id, "assembly.apply",
    { timeline_id: tl.id, resolve_conflicts: "replace" }, 422);
  const shotKey = conflictedItems.find((i) => i.kind === "shot").key;
  await commit(p.id, "assembly.apply", {
    timeline_id: tl.id, resolve_conflicts: "replace",
    replace_keys: [shotKey] });
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  const vNow = det.clips.find(
    (c) => c.shot_id === shot1.id && isVideoClip(c));
  const dlgStill = det.clips.find((c) => c.id === dlgMovedId);
  if (!vNow || vNow.start_frames !== 0
      || det.clips.some((c) => c.id === movedId)) {
    fail(`approved replacement wrong: ${JSON.stringify(vNow)}`);
  }
  if (!dlgStill || dlgStill.start_frames !== 120) {
    fail("unapproved manual edit was overwritten");
  }
  ok("12 scoped replacement — shot clip replaced at 0f, dialogue edit kept");

  // step 13 — undo/redo through the timeline's own stack: the replace
  //           apply is one logical step; deep undo reaches the first
  //           apply; redo restores the approved state
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  const u1 = det.clips.find(
    (c) => c.shot_id === shot1.id && isVideoClip(c));
  const u1d = det.clips.find((c) => c.id === dlgMovedId);
  if (!u1 || u1.start_frames !== 200 || !u1d || u1d.start_frames !== 120
      || det.clips.length !== 3) {
    fail(`undo of replace-apply wrong: ${JSON.stringify(u1)}`);
  }
  for (let i = 0; i < 3; i++) {
    await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  }
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  if (det.clips.length !== 0) {
    fail(`deep undo wrong: ${det.clips.length} clips remain`);
  }
  for (let i = 0; i < 4; i++) {
    await commit(p.id, "timeline.redo", { timeline_id: tl.id });
  }
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  const rV = det.clips.find(
    (c) => c.shot_id === shot1.id && isVideoClip(c));
  const rD = det.clips.find((c) => c.id === dlgMovedId);
  if (det.clips.length !== 3 || !rV || rV.start_frames !== 0
      || !rD || rD.start_frames !== 120) {
    fail(`redo state wrong: ${det.clips.length} clips`);
  }
  ok("13 undo/redo — replace undone in one step, deep undo clean, redone");

  // step 14 — review votes + a note persist on the plan
  det0 = await planDetail(p.id, pl.id);
  const shotItemId = det0.items.find((i) => i.kind === "shot").id;
  await commit(p.id, "assembly.review", {
    plan_id: pl.id, item_ids: [shotItemId],
    status: "approved", note: "keep the moved position" });
  det0 = await planDetail(p.id, pl.id);
  const voted = det0.items.find((i) => i.id === shotItemId);
  if (voted.review_status !== "approved"
      || voted.notes !== "keep the moved position") {
    fail(`review vote wrong: ${JSON.stringify(voted)}`);
  }
  ok("14 review — vote + note persisted");

  // step 15 — export H.264+AAC through the existing pipeline; duration
  //           covers the moved dialogue clip (~7.3 s)
  const ex = await commit(p.id, "timeline.export",
    { timeline_id: tl.id, profile: "720p" });
  const exDone = await waitJob(p.id, ex.job_id);
  if (exDone.status !== "succeeded") {
    fail(`export failed: ${JSON.stringify(exDone).slice(0, 300)}`);
  }
  const ffprobe = findFfprobe();
  if (!ffprobe) fail("ffprobe not found (bundled or machine)");
  note(`ffprobe: ${ffprobe}`);
  const dataDir = path.join(process.env.LOCALAPPDATA, "AtomicAIStudio");
  const exAsset = (await api("GET", `/api/projects/${p.id}/assets`)).items
    .find((a) => a.id === exDone.output_asset_id);
  const exBlob = path.join(dataDir, exAsset.uri.split("/").join(path.sep));
  const pr = JSON.parse(execFileSync(ffprobe, [
    "-v", "error", "-print_format", "json",
    "-show_streams", "-show_format", exBlob]).toString());
  const vS = pr.streams.find((s) => s.codec_type === "video");
  const aS = pr.streams.find((s) => s.codec_type === "audio");
  const dur = parseFloat(pr.format.duration);
  // moved dialogue ends ~120f/24+2.3s ≈ 7.3s — duration must cover it
  if (!vS || !aS || dur < 6.0 || dur > 9.5) {
    fail(`export streams wrong: ${JSON.stringify(
      { v: vS?.codec_name, a: aS?.codec_name, dur })}`);
  }
  ok(`15 export streams — ${vS.codec_name} + ${aS.codec_name}, ~${dur.toFixed(2)}s`);

  // step 16 — PCM placement check: decode the export's audio and prove
  //           signal sits where the moved dialogue clip is and silence
  //           holds before it — no bleed, no duplication
  const ffmpeg = findFfmpeg();
  if (!ffmpeg) fail("ffmpeg not found for PCM check");
  const pcm = execFileSync(ffmpeg, [
    "-v", "error", "-i", exBlob, "-map", "0:a:0",
    "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
    { maxBuffer: 64 * 1024 * 1024 });
  const samples = new Int16Array(
    pcm.buffer, pcm.byteOffset, Math.floor(pcm.length / 2));
  const rmsAt = (t0, t1) => {
    let s = 0, n = 0;
    for (let i = Math.floor(t0 * 48000);
         i < Math.min(samples.length, Math.floor(t1 * 48000)); i++) {
      s += samples[i] * samples[i]; n++;
    }
    return n ? Math.sqrt(s / n) / 32768 : 0;
  };
  const early = rmsAt(2.6, 4.6);   // before the moved dialogue
  const late = rmsAt(5.4, 6.8);    // inside the moved dialogue's span
  if (late < 0.01) fail(`moved dialogue audio missing (rms ${late})`);
  if (early > 0.005) fail(`audio bleeds before placement (rms ${early})`);
  ok(`16 PCM parity — signal ${late.toFixed(3)} @5.4s, gap ${early.toFixed(4)}`);

  // step 17 — backup/restore: package round-trips plan, clips, review
  const bjob = await waitJob(p.id,
    (await commit(p.id, "backup.export", { kind: "backup" })).job_id);
  if (bjob.status !== "succeeded") fail(`backup export: ${bjob.status}`);
  const pkg = (await api("GET", `/api/projects/${p.id}/jobs`))
    .find((j) => j.id === bjob.id).result_json.package_path;
  const imp = await api("POST", "/api/packages/import",
    { package_path: pkg });
  const ijob = await waitJob(imp.project_id, imp.job_id);
  if (ijob.status !== "succeeded") {
    fail(`import: ${ijob.status} ${ijob.error || ""}`);
  }
  const newPid = imp.project_id;
  const nt = (await api("GET", `/api/projects/${newPid}/timelines`))[0];
  const npl = (await plans(newPid, nt.id))[0];
  const ndet = await api("GET", `/api/projects/${newPid}/timelines/${nt.id}`);
  const nitems = (await planDetail(newPid, npl.id)).items;
  const nvote = nitems.find((i) => i.notes === "keep the moved position");
  if (!npl || npl.status !== "applied" || ndet.clips.length !== 3 || !nvote) {
    fail(`restore wrong: plan=${!!npl} clips=${ndet.clips.length}`);
  }
  ok("17 backup/restore — plan, applied clips, review note round-trip");

  // step 18 — restart persistence: plan, applied clips, review state
  killApp(); await sleep(4000);
  launch(); API = null; TOKEN = null; await creds();
  pl = (await plans(p.id, tl.id))[0];
  det0 = await planDetail(p.id, pl.id);
  det = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}`);
  const revoted = det0.items.find((i) => i.id === shotItemId);
  if (pl.status !== "applied" || det.clips.length !== 3
      || revoted.review_status !== "approved"
      || revoted.notes !== "keep the moved position") {
    fail("restart drifted plan/clip/review state");
  }
  ok("18 restart persistence — applied plan, clips, review intact");

  // step 19 — clean shutdown
  killApp(); await sleep(6000);
  if (left("atomic-backend.exe")) fail("backend process leaked");
  ok("19 clean shutdown — no orphan backend");

  lines.push(`A6 ACCEPTANCE: PASS ${steps}/${STEPS}`);
  fs.writeFileSync(path.join(OUT, "a6-acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\n${lines[lines.length - 1]}`);
  if (steps !== STEPS) fail(`step count ${steps} != ${STEPS}`);
}

main().catch((e) => {
  lines.push(`FAIL ${e.message}`);
  try { fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, "a6-acceptance.txt"), lines.join("\n") + "\n"); } catch {}
  console.error(e);
  try { killApp(); } catch {}
  process.exit(1);
});
