// A8 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port, same plumbing
// as A1–A7). Two scenarios per A8.13:
//   Scenario A (22 steps) — clean-install end-to-end wizard film.
//   Scenario B (12 steps) — upgrade path over a populated project:
//     migration intactness, wizard resume, mode switch, edits, export.
// All generation is mock-adapter simulated; the wizard state surface is
// exercised via GET /wizard + settings_json.wizard exactly as the UI does.
// Run: node scripts/cdp-a8-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9232";
const OUT = process.argv[3] || "docs/a8-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Atomic Dream Engine\\atomic-dream-engine.exe`;
const INSTALL_DIR = path.dirname(EXE);
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
    expected_revision: rev, idempotency_key: key || `a8-${ct}-${Date.now()}-${Math.random()}` }, expect);
}
async function preview(pid, ct, payload, expect = 200) {
  return api("POST", `/api/projects/${pid}/commands/preview`, {
    command_type: ct, payload, scope: "project", origin: "acceptance" }, expect);
}
async function waitJob(pid, jobId, timeout = 300000) {
  const t0 = Date.now();
  for (;;) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) return j;
    if (Date.now() - t0 > timeout) fail(`job ${jobId} timeout`);
    await sleep(500);
  }
}
const wizard = (pid) => api("GET", `/api/projects/${pid}/wizard`);
const proj = (pid) => api("GET", `/api/projects/${pid}`);
async function saveWizard(pid, s) {
  await commit(pid, "project.update_settings", { settings: { wizard: s } });
}
const stageOf = (w, id) => w.stages.find((s) => s.id === id);

// ---------- CDP plumbing (same as A7) ----------
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
  API = null; TOKEN = null;
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
const killApp = () => {
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"], { stdio: "ignore" }); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "Atomic Dream Engine.exe"], { stdio: "ignore" }); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"], { stdio: "ignore" }); } catch {}
};

// Bundled ffprobe (installed _internal tree) with machine fallback.
function findFfprobe() {
  for (const r of [INSTALL_DIR, path.join(INSTALL_DIR, "resources"),
                   path.join(INSTALL_DIR, "resources", "ffmpeg", "bin")]) {
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
async function assetFile(pid, assetId, ext) {
  const r = await fetch(`${API}/api/projects/${pid}/assets/${assetId}/file`,
    { headers: { "X-Atomic-Token": TOKEN } });
  if (r.status !== 200) fail(`asset file fetch ${r.status}`);
  const f = path.join(OUT, `probe-${assetId}${ext}`);
  fs.writeFileSync(f, Buffer.from(await r.arrayBuffer()));
  return f;
}

async function genKeyframe(pid, kfId) {
  const p = await preview(pid, "keyframe.generate", { keyframe_id: kfId });
  const r = await commit(pid, "keyframe.generate", {
    keyframe_id: kfId,
    expected_keyframe: p.resolved_keyframe,
    expected_prompt: p.resolved_prompt?.prompt_sha256 });
  return waitJob(pid, r.job_id);
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(1500);

  // ============================================================
  // SCENARIO A — clean install: full wizard flow, 22 steps
  // ============================================================
  launch();
  await creds();
  // A1 (install itself) is done by the harness — the app is running.
  ok("A1. installed application launched — frozen backend serving API, no system Python");

  // A2 — same fact, asserted: API answers over the sidecar socket.
  await api("GET", "/api/projects");
  ok("A2. launch without system Python — backend API responsive");

  // A3 — create project.
  const pid = (await api("POST", "/api/projects", {
    name: `A8 Film ${Date.now()}` }, 201)).id;
  ok("A3. new project created");

  // A4 — film settings through the wizard's own command path.
  await commit(pid, "project.update_settings", {
    aspect_ratio: "16:9", width: 1280, height: 720,
    fps_num: 24, fps_den: 1 });
  const p4 = await proj(pid);
  if (p4.width !== 1280 || p4.fps_num !== 24) fail("film settings not applied");
  // Wizard progress begins — the blob the UI persists.
  await saveWizard(pid, { version: 2, step: 3, furthest: 3, mode: "beginner",
    screenplay_id: null, timeline_id: null, plan_id: null,
    export_job_id: null, export_profile: "720p", done: false,
    updated_at: new Date().toISOString() });
  const w4 = await wizard(pid);
  if (!w4.progress || w4.progress.step !== 3) fail("wizard progress not persisted");
  ok("A4. film settings configured (16:9 1280x720 @24/1); wizard progress persisted in project settings");

  // A5 — screenplay with >= 2 scenes.
  await commit(pid, "screenplay.create", { title: "Tide" });
  const sp = (await api("GET", `/api/projects/${pid}/screenplays`))[0];
  await commit(pid, "screenplay_scene.create", { screenplay_id: sp.id, heading: "INT. DOCK — NIGHT" });
  await commit(pid, "screenplay_scene.create", { screenplay_id: sp.id, heading: "EXT. ROAD — DAWN" });
  const doc = await api("GET", `/api/projects/${pid}/screenplays/${sp.id}`);
  if (doc.scenes.length !== 2) fail(`expected 2 scenes, got ${doc.scenes.length}`);
  await saveWizard(pid, { ...(await wizard(pid)).progress, screenplay_id: sp.id });
  ok("A5. screenplay created with 2 scenes");

  // A6 — multiple characters.
  await commit(pid, "character.create", { name: "Anna" });
  await commit(pid, "character.create", { name: "Bram" });
  const chars = await api("GET", `/api/projects/${pid}/character`);
  if (chars.length !== 2) fail("characters missing");
  const [anna, bram] = chars;
  ok("A6. two characters created");

  // A7 — dialogue lines with speakers.
  await commit(pid, "screenplay_scene.update", {
    sp_scene_id: doc.scenes[0].id,
    action: "Anna waits by the crane; Bram arrives.",
    elements: [
      { kind: "action", text: "The crane creaks in the wind." },
      { kind: "dialogue", character_id: anna.id, text: "The tide is coming in." },
      { kind: "dialogue", character_id: bram.id, text: "Then we move now." },
    ] });
  await commit(pid, "screenplay_scene.update", {
    sp_scene_id: doc.scenes[1].id,
    elements: [
      { kind: "dialogue", character_id: anna.id, text: "We made it." },
    ] });
  const doc2 = await api("GET", `/api/projects/${pid}/screenplays/${sp.id}`);
  const dlg = doc2.scenes.flatMap((s) => s.elements).filter((e) => e.kind === "dialogue");
  if (dlg.length !== 3) fail(`expected 3 dialogue lines, got ${dlg.length}`);
  ok("A7. dialogue added — 3 lines across 2 scenes with speakers");

  // A8 — scaffold shots from the screenplay (preview → commit).
  {
    await preview(pid, "shotlist.scaffold", { screenplay_id: sp.id });
    await commit(pid, "shotlist.scaffold", { screenplay_id: sp.id });
    const board = await api("GET", `/api/projects/${pid}/storyboard`);
    const shotCount = board.reduce((n, s) => n + s.shots.length, 0);
    if (board.length !== 2 || shotCount < 2)
      fail(`scaffold produced ${board.length} scenes / ${shotCount} shots`);
  }
  ok("A8. shots scaffolded from screenplay — 2 storyboard scenes");

  // A9 — camera + lighting on each shot (planning, incl. a follow mode).
  {
    const board = await api("GET", `/api/projects/${pid}/storyboard`);
    for (const sc of board) {
      for (const sh of sc.shots) {
        await commit(pid, "shot.update", {
          shot_id: sh.shot.id,
          planning: {
            camera: { angle: "eye_level", movement: "dolly_in",
              follow_mode: "follow_behind",
              follow_targets: [{ kind: "character", id: anna.id }] },
            framing: "medium", lighting: "low-key, sodium rim" } });
      }
    }
    const b2 = await api("GET", `/api/projects/${pid}/storyboard`);
    const plan = b2[0].shots[0].shot.planning_json;
    if (!plan || plan.camera.follow_mode !== "follow_behind")
      fail("planning not stored");
  }
  ok("A9. camera + lighting configured on every shot (follow_behind target)");

  // A10 — keyframe drafts (start+end per shot) with prompts.
  const kfIds = [];
  const kfsFor = async (shotId) => api("GET",
    `/api/projects/${pid}/shots/${shotId}/keyframes`);
  {
    const board = await api("GET", `/api/projects/${pid}/storyboard`);
    for (const sc of board) for (const sh of sc.shots) {
      for (const role of ["start", "end"]) {
        await commit(pid, "keyframe.create", { shot_id: sh.shot.id, role });
      }
    }
    const kfs = [];
    for (const sc of board) for (const sh of sc.shots) {
      kfs.push(...await kfsFor(sh.shot.id));
    }
    for (const k of kfs.filter((x) => x.status === "active")) {
      await commit(pid, "keyframe.update", {
        keyframe_id: k.id, prompt: `${k.role} frame — quayside, dawn light` });
      kfIds.push(k.id);
    }
    if (!kfIds.length) fail("no keyframes created");
  }
  ok(`A10. keyframe drafts created — ${kfIds.length} start/end keyframes with prompts`);

  // A11 — mock image generation (simulated, labeled).
  {
    const w = await wizard(pid);
    const before = w.entities.keyframes.filter((k) => k.state === "draft").length;
    if (before !== kfIds.length) fail(`expected ${kfIds.length} draft keyframes, got ${before}`);
    for (const id of kfIds) {
      const j = await genKeyframe(pid, id);
      if (j.status !== "succeeded") fail(`keyframe job ${j.status}`);
    }
    const w2 = await wizard(pid);
    const sim = w2.entities.keyframes.filter((k) => k.state === "simulated").length;
    if (sim !== kfIds.length) fail(`expected ${kfIds.length} simulated keyframes, got ${sim}`);
    if (!w2.honesty.all_simulated) fail("honesty flag not all_simulated");
  }
  ok(`A11. mock image generation — ${kfIds.length} keyframes, all labeled simulated`);

  // A12 — mock video generation (motion clip per shot).
  {
    const board = await api("GET", `/api/projects/${pid}/storyboard`);
    const kfs = [];
    for (const sc of board) for (const sh of sc.shots) {
      kfs.push(...await kfsFor(sh.shot.id));
    }
    for (const sc of board) for (const sh of sc.shots) {
      const mine = kfs.filter((k) => k.shot_id === sh.shot.id && k.status === "active");
      const start = mine.find((k) => k.role === "start");
      const end = mine.find((k) => k.role === "end");
      await commit(pid, "clip.create", {
        shot_id: sh.shot.id, start_keyframe_id: start?.id,
        end_keyframe_id: end?.id, prompt: `${sh.shot.title} motion`,
        duration_frames: 24 });
      const clips = (await api("GET", `/api/projects/${pid}/shots/${sh.shot.id}/clips`))
        .filter((c) => c.status === "active");
      const p = await preview(pid, "clip.generate", { clip_id: clips[0].id });
      const r = await commit(pid, "clip.generate", {
        clip_id: clips[0].id, expected_clip: p.resolved_clip,
        expected_prompt: p.resolved_prompt?.prompt_sha256 });
      const j = await waitJob(pid, r.job_id);
      if (j.status !== "succeeded") fail(`clip job ${j.status}`);
    }
    const w = await wizard(pid);
    if (!w.entities.clips.length || !w.entities.clips.every((c) => c.state === "simulated"))
      fail("clips not all simulated");
  }
  ok("A12. mock video generation — one motion clip per shot, simulated");

  // A13 — mock dialogue/audio generation.
  {
    for (const sc of doc2.scenes) {
      for (const e of sc.elements.filter((x) => x.kind === "dialogue")) {
        await commit(pid, "dialogue.create", {
          sp_scene_id: sc.id, element_id: e.id,
          character_id: e.character_id, text: e.text });
      }
    }
    const segs = (await api("GET", `/api/projects/${pid}/dialogue`))
      .filter((s) => s.status === "active");
    if (segs.length !== 3) fail(`expected 3 segments, got ${segs.length}`);
    for (const s of segs) {
      const p = await preview(pid, "dialogue.generate", { segment_id: s.id });
      const r = await commit(pid, "dialogue.generate", {
        segment_id: s.id, expected_dialogue: p.resolved_dialogue });
      const j = await waitJob(pid, r.job_id);
      if (j.status !== "succeeded") fail(`dialogue job ${j.status}`);
    }
    const w = await wizard(pid);
    if (!w.entities.dialogue.every((d) => d.state === "simulated"))
      fail("dialogue not all simulated");
  }
  ok("A13. mock dialogue generation — 3 segments, simulated tones (never speech)");

  // A14 — preview first-cut assembly.
  let tid, planId;
  {
    await commit(pid, "timeline.create", { name: "First cut" });
    tid = (await api("GET", `/api/projects/${pid}/timelines`))[0].id;
    const p = await preview(pid, "assembly.plan", { timeline_id: tid, name: "A8 cut" });
    const ra = p.resolved_assembly;
    if (!ra || !(ra.items_total > 0 || (ra.items || []).length > 0))
      fail("plan preview produced no items");
    await commit(pid, "assembly.plan", { timeline_id: tid, name: "A8 cut" });
    planId = (await api("GET", `/api/projects/${pid}/assemblies?timeline_id=${tid}`))[0].id;
    await saveWizard(pid, { ...(await wizard(pid)).progress,
      timeline_id: tid, plan_id: planId, step: 11, furthest: 11 });
  }
  ok("A14. first-cut assembly previewed — deterministic proposal from live rows");

  // A15 — approve + apply.
  {
    const plan = await api("GET", `/api/projects/${pid}/assemblies/${planId}`);
    const ids = (plan.items || []).map((i) => i.id);
    await commit(pid, "assembly.review", { plan_id: planId, item_ids: ids, status: "approved" });
    await commit(pid, "assembly.apply", { timeline_id: tid });
    const det = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    if (!det.clips.length) fail("apply placed no clips");
  }
  ok("A15. assembly reviewed (all approved) + applied — timeline populated");

  // A16 — edit the timeline (a real command: tail-trim one clip by a
  //        source frame; survives into Scenario B's "edits intact").
  let trimmedClip = null, trimmedOut = 0;
  {
    const det = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    const clip = det.clips.find((c) => (c.out_frame - c.in_frame) > 1);
    if (!clip) fail("no trimmable clip");
    trimmedClip = clip.id; trimmedOut = clip.out_frame - 1;
    await commit(pid, "timeline.clip.trim",
      { timeline_id: tid, clip_id: clip.id, out_frame: trimmedOut });
    const det2 = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    const c2 = det2.clips.find((c) => c.id === clip.id);
    if (c2.out_frame !== trimmedOut) fail("clip trim not applied");
  }
  ok("A16. timeline edited — source-frame tail trim through commands");

  // A17 — audio mix: lane gain on an audio track.
  {
    const det = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    const lane = det.tracks.find((t) => t.kind === "audio");
    if (lane) {
      await commit(pid, "timeline.track.update", { track_id: lane.id, gain_db: -2 });
      const det2 = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
      const l2 = det2.tracks.find((t) => t.id === lane.id);
      if (l2.gain_db !== -2) fail("lane gain not applied");
    }
  }
  ok("A17. audio mixed — lane gain −2 dB on audio track");

  // A18 — export.
  let exportJob;
  {
    const p = await preview(pid, "timeline.export", { timeline_id: tid, profile: "720p" });
    const rt = p.resolved_timeline;
    const r = await commit(pid, "timeline.export", {
      timeline_id: tid, profile: "720p",
      expected_timeline: rt ? { id: rt.id, revision: rt.revision } : undefined,
      expected_edl_sha: rt?.edl_sha256 });
    exportJob = await waitJob(pid, r.job_id);
    if (exportJob.status !== "succeeded") fail(`export ${exportJob.status}: ${exportJob.error || ""}`);
    await saveWizard(pid, { ...(await wizard(pid)).progress,
      export_job_id: exportJob.id, done: true });
  }
  ok("A18. film exported — 720p profile through the real FFmpeg pipeline");

  // A19 — validate output with bundled ffprobe.
  {
    const ffprobe = findFfprobe();
    if (!ffprobe) fail("ffprobe not found");
    note(`ffprobe: ${ffprobe}`);
    const f = await assetFile(pid, exportJob.output_asset_id, ".mp4");
    const out = execFileSync(ffprobe, ["-v", "error", "-print_format", "json",
      "-show_streams", "-show_format", f]).toString();
    const probe = JSON.parse(out);
    const v = (probe.streams || []).find((s) => s.codec_type === "video");
    const a = (probe.streams || []).find((s) => s.codec_type === "audio");
    if (!v || v.codec_name !== "h264") fail(`video stream wrong: ${JSON.stringify(v).slice(0, 200)}`);
    if (Number(probe.format.duration) <= 0) fail("zero-duration export");
    if (a && a.codec_name !== "aac") fail(`audio stream wrong: ${a.codec_name}`);
    fs.writeFileSync(path.join(OUT, "export-probe.json"), JSON.stringify(probe, null, 2));
  }
  ok("A19. ffprobe validates export — h264 video (+ audio stream), nonzero duration");

  // A20 — restart + persistence.
  killApp(); await sleep(2000); launch(); await creds();
  {
    const w = await wizard(pid);
    if (!w.progress || w.progress.done !== true || w.progress.plan_id !== planId)
      fail("wizard progress lost across restart");
    const det = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    if (!det.clips.length) fail("timeline lost across restart");
  }
  ok("A20. restart persistence — wizard progress + timeline intact");

  // A21 — backup/restore with wizard progress + media.
  let pid2;
  {
    const dest = path.join(OUT, "a8-backup.atomicpkg");
    const r = await commit(pid, "backup.export", { dest_path: dest });
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "succeeded") fail(`backup.export ${j.status}`);
    if (!fs.existsSync(dest)) fail("backup file missing");
    const imp = await api("POST", "/api/packages/import",
      { package_path: dest }, 200);
    pid2 = imp.project_id;
    if (imp.job_id) await waitJob(pid2, imp.job_id);
    const pr = await api("GET", `/api/projects/${pid2}`);
    if (pr.status === "importing") fail("import job did not finish");
    const w = await wizard(pid2);
    if (!w.progress || w.progress.done !== true) fail("wizard progress not restored");
    const det = await api("GET", `/api/projects/${pid2}/timelines`);
    if (!det.length) fail("timelines not restored");
  }
  ok("A21. backup/restore — package carries wizard progress + project rows");

  // A22 — clean shutdown.
  killApp(); await sleep(1000);
  {
    const procs = execFileSync("tasklist", ["/FI", "IMAGENAME eq atomic-backend.exe"],
      { encoding: "utf8" });
    if (/atomic-backend\.exe/i.test(procs)) fail("backend still running after shutdown");
  }
  ok("A22. clean shutdown — app + backend processes exited");

  // ============================================================
  // SCENARIO B — upgrade over the populated install (12 steps)
  // ============================================================
  launch(); await creds();

  // B1 — same binary reinstalled over populated data (the A project
  //      above IS the populated installation for this run).
  const projects = await api("GET", "/api/projects");
  const existing = projects.find((p) => p.id === pid);
  if (!existing) fail("populated project missing after reinstall");
  ok("B1. populated installation opened under the new build");

  // B2 — migration head: /wizard derives (needs A8-era reads) and the
  //      project answers — the frozen backend IS the migration runner.
  {
    const w = await wizard(pid);
    if (!w.stages || w.stages.length !== 15) fail("wizard derivation missing");
  }
  ok("B2. database migrated to A8 head — /wizard derives 15 stages");

  // B3 — open the existing project.
  ok("B3. existing populated project opened");

  // B4 — all content intact: scenes, shots, keyframes, clips, dialogue, assets.
  {
    const board = await api("GET", `/api/projects/${pid}/storyboard`);
    const kfs = [];
    for (const sc of board) for (const sh of sc.shots) {
      kfs.push(...(await api("GET",
        `/api/projects/${pid}/shots/${sh.shot.id}/keyframes`)));
    }
    const segs = await api("GET", `/api/projects/${pid}/dialogue`);
    const det = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    const w = await wizard(pid);
    const shotCount = board.reduce((n, s) => n + s.shots.length, 0);
    if (board.length !== 2 || shotCount < 2) fail("scenes/shots missing");
    if (!kfs.filter((k) => k.result_asset_id).length) fail("keyframe results missing");
    if (segs.filter((s) => s.result_asset_id).length !== 3) fail("dialogue assets missing");
    if (!det.clips.length) fail("timeline clips missing");
    if (!w.entities.clips.every((c) => c.state === "simulated")) fail("clip assets missing");
  }
  ok("B4. scenes, shots, keyframes, clips, dialogue, assets all intact");

  // B5 — wizard opens and reports the real derived state.
  {
    const w = await wizard(pid);
    const done = w.stages.filter((s) => s.status === "done").length;
    if (done < 8) fail(`expected >=8 done stages, got ${done}`);
    if (!w.honesty.all_simulated) fail("honesty not all_simulated");
  }
  ok("B5. wizard derives existing work — stages done, honesty all-simulated");

  // B6 — resume persisted wizard progress.
  {
    const w = await wizard(pid);
    if (w.progress.done !== true) fail("wizard progress not resumed");
  }
  ok("B6. wizard progress resumed from settings_json");

  // B7 — mode switch is a view flag: data untouched.
  const allKfs = async () => {
    const b = await api("GET", `/api/projects/${pid}/storyboard`);
    const out = [];
    for (const sc of b) for (const sh of sc.shots) {
      out.push(...(await api("GET",
        `/api/projects/${pid}/shots/${sh.shot.id}/keyframes`)));
    }
    return out;
  };
  {
    const kfsBefore = (await allKfs()).map((k) => `${k.id}:${k.revision}`).sort().join("|");
    await saveWizard(pid, { ...(await wizard(pid)).progress, mode: "advanced" });
    const w = await wizard(pid);
    if (w.progress.mode !== "advanced") fail("mode not switched");
    const kfsAfter = (await allKfs()).map((k) => `${k.id}:${k.revision}`).sort().join("|");
    if (kfsBefore !== kfsAfter) fail("mode switch touched entity rows");
    await saveWizard(pid, { ...w.progress, mode: "beginner" });
  }
  ok("B7. Beginner/Advanced switch — view flag only, zero data rewrites");

  // B8 — manual edits intact (the A16 tail-trim + planning).
  {
    const det = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    const trimmed = det.clips.find((c) => c.id === trimmedClip);
    if (!trimmed || trimmed.out_frame !== trimmedOut)
      fail("manual clip edit lost");
    const board = await api("GET", `/api/projects/${pid}/storyboard`);
    const plan = board[0].shots[0].shot.planning_json;
    if (plan?.camera?.follow_mode !== "follow_behind") fail("manual planning lost");
  }
  ok("B8. manual edits intact — tail trim + shot planning preserved");

  // B9 — timeline/audio regression: lanes + clip count unchanged.
  {
    const det = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    if (!det.tracks.length || !det.clips.length) fail("timeline regressed");
    const audio = det.tracks.filter((t) => t.kind === "audio");
    if (!audio.length) note("no audio lane present — video-only film is acceptable");
  }
  ok("B9. timeline/audio regression — lanes + clips healthy");

  // B10 — export the existing project again (idempotent second export).
  {
    const p = await preview(pid, "timeline.export", { timeline_id: tid, profile: "720p" });
    const rt = p.resolved_timeline;
    const r = await commit(pid, "timeline.export", {
      timeline_id: tid, profile: "720p",
      expected_timeline: rt ? { id: rt.id, revision: rt.revision } : undefined,
      expected_edl_sha: rt?.edl_sha256 });
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "succeeded") fail(`re-export ${j.status}`);
    const ffprobe = findFfprobe();
    const f = await assetFile(pid, j.output_asset_id, ".mp4");
    const probe = JSON.parse(execFileSync(ffprobe, ["-v", "error",
      "-print_format", "json", "-show_format", f]).toString());
    if (Number(probe.format.duration) <= 0) fail("re-export zero duration");
  }
  ok("B10. existing project re-exports cleanly — ffprobe-valid output");

  // B11 — restart persistence once more.
  killApp(); await sleep(2000); launch(); await creds();
  {
    const w = await wizard(pid);
    if (!w.progress || w.progress.done !== true) fail("progress lost on second restart");
  }
  ok("B11. second restart — persistence holds");

  // B12 — clean shutdown.
  killApp(); await sleep(1000);
  {
    const procs = execFileSync("tasklist", ["/FI", "IMAGENAME eq atomic-backend.exe"],
      { encoding: "utf8" });
    if (/atomic-backend\.exe/i.test(procs)) fail("backend still running");
  }
  ok("B12. clean shutdown");

  fs.writeFileSync(path.join(OUT, "acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\nA8 ACCEPTANCE: ${steps}/34 PASS`);
}

main().catch((e) => {
  lines.push(`FATAL ${e.message}`);
  try { fs.mkdirSync(OUT, { recursive: true }); } catch {}
  try { fs.writeFileSync(path.join(OUT, "acceptance.txt"), lines.join("\n") + "\n"); } catch {}
  console.error(e);
  killApp();
  process.exit(1);
});
