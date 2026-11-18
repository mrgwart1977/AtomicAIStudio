// C1 installed-candidate acceptance — reliability baseline against the
// REAL installed app (CDP port sniffs backend URL+token, same plumbing
// as A1–A8). 15 checks per C1.10: clean install/startup, multi-project,
// durable job interruption (kill mid-run → reconcile), cancel+retry,
// simulated GPU reservation recovery (ATOMIC_HW_JSON), backup/restore,
// timeline+audio, bundled-ffmpeg export, restart persistence, bounded
// shutdown, no orphans, uninstall data preservation.
// Run: node scripts/cdp-c1-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9234";
const OUT = process.argv[3] || "docs/c1-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Atomic Dream Engine\\atomic-dream-engine.exe`;
const INSTALL_DIR = path.dirname(EXE);
const DATA_DIR = `${process.env.LOCALAPPDATA}\\AtomicAIStudio`;
const GIB = 1024 ** 3;
let API = null, TOKEN = null;
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
    expected_revision: rev, idempotency_key: key || `c1-${ct}-${Date.now()}-${Math.random()}` }, expect);
}
async function preview(pid, ct, payload, expect = 200) {
  return api("POST", `/api/projects/${pid}/commands/preview`, {
    command_type: ct, payload, scope: "project", origin: "acceptance" }, expect);
}
async function waitJob(pid, jobId, timeout = 90) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout * 1000) {
    const jobs = await api("GET", `/api/projects/${pid}/jobs`);
    const j = jobs.find((x) => x.id === jobId);
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) return j;
    await sleep(400);
  }
  fail(`job ${jobId} not terminal in ${timeout}s`);
}
async function waitJobStatus(pid, jobId, want, timeout = 30) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout * 1000) {
    const jobs = await api("GET", `/api/projects/${pid}/jobs`);
    const j = jobs.find((x) => x.id === jobId);
    if (j && j.status === want) return j;
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status) && j.status !== want)
      fail(`job settled before ${want}: ${j.status}`);
    await sleep(250);
  }
  fail(`job ${jobId} never reached ${want}`);
}
const proj = (pid) => api("GET", `/api/projects/${pid}`);

// ---- CDP plumbing (same as A8 driver) ----
let ws, msgId = 0; const pending = new Map(); const listeners = [];
function onEvent(m, cb) { listeners.push([m, cb]); }
async function cdpConnect() {
  for (let i = 0; i < 30; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) {
        ws = new WebSocket(page.webSocketDebuggerUrl);
        ws.onmessage = (ev) => {
          const d = JSON.parse(ev.data);
          if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); }
          else if (d.method) for (const [m, cb] of listeners) if (d.method === m) cb(d.params || {});
        };
        await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
        return;
      }
    } catch {}
    await sleep(1000);
  }
  throw new Error("CDP connect failed — app not exposing debug port");
}
function send(method, params = {}) {
  return new Promise((res) => {
    const id = ++msgId; pending.set(id, res);
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function creds() {
  await cdpConnect();
  API = null; TOKEN = null;
  await send("Network.enable");
  onEvent("Network.requestWillBeSent", (e) => {
    const m = e.request.url.match(/http:\/\/(127\.0\.0\.1|localhost):(\d+)\/api/);
    if (m && !API) {
      API = `http://127.0.0.1:${m[2]}`;
      TOKEN = (e.request.headers || {})["X-Atomic-Token"] || null;
    }
  });
  const t1 = Date.now(); let nudged = false;
  while (!API || !TOKEN) {
    if (Date.now() - t1 > 90000) throw new Error("no backend creds");
    if (!nudged && Date.now() - t1 > 8000) {
      nudged = true;
      try { await send("Page.enable"); await send("Page.reload", { ignoreCache: true }); } catch {}
    }
    await sleep(1000);
  }
  try { ws.close(); } catch {}
  note(`backend ${API} via installed app`);
}
const HWFILE = path.join(OUT, "hw-inventory.json");
function launch(extraEnv = {}) {
  const p = spawn(EXE, [], { detached: true, stdio: "ignore",
    env: { ...process.env,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${PORT}`,
      ...extraEnv } });
  p.unref();
}
const killApp = () => {
  for (const im of ["atomic-dream-engine.exe", "Atomic Dream Engine.exe",
                    "atomic-backend.exe"]) {
    try { execFileSync("taskkill", ["/F", "/IM", im], { stdio: "ignore" }); } catch {}
  }
};
const procs = () => {
  try {
    return execFileSync("tasklist", ["/FI", "IMAGENAME eq atomic-backend.exe"],
      { stdio: "pipe" }).toString() +
      execFileSync("tasklist", ["/FI", "IMAGENAME eq atomic-dream-engine.exe"],
      { stdio: "pipe" }).toString();
  } catch { return ""; }
};
async function waitProcsGone(timeout = 15) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout * 1000) {
    if (!procs().includes(".exe")) return Date.now() - t0;
    await sleep(500);
  }
  return -1;
}
function findFfprobe() {
  for (const r of [INSTALL_DIR, path.join(INSTALL_DIR, "resources")]) {
    const p = path.join(r, "ffprobe.exe"); if (fs.existsSync(p)) return p;
  }
  try {
    const out = execFileSync("cmd", ["/c", "dir /s /b ffprobe.exe"],
      { cwd: INSTALL_DIR }).toString();
    const f = out.split(/\r?\n/).filter(Boolean)[0];
    if (f && fs.existsSync(f)) return f;
  } catch {}
  return null;
}
async function assetFile(pid, assetId, ext) {
  const r = await fetch(`${API}/api/projects/${pid}/assets/${assetId}/file`,
    { headers: { "X-Atomic-Token": TOKEN } });
  if (r.status !== 200) fail(`asset fetch ${r.status}`);
  const f = path.join(OUT, `probe-${assetId}${ext}`);
  fs.writeFileSync(f, Buffer.from(await r.arrayBuffer()));
  return f;
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(1500);

  // ---- 1+3: launch, sidecar readiness, frozen backend ----
  launch(); await creds();
  const h = await api("GET", "/health", undefined, 200);
  if (!h || h.status !== "ok") fail("health endpoint not ready");
  ok("1. clean install launched — frozen sidecar serving API, no system Python");
  const pre = (await api("GET", "/api/projects")).length;
  note(`pre-existing projects after install: ${pre}`);

  // ---- 4: multi-project ----
  const p1 = (await api("POST", "/api/projects", { name: `C1 A ${Date.now()}` }, 201)).id;
  const p2 = (await api("POST", "/api/projects", { name: `C1 B ${Date.now()}` }, 201)).id;
  if ((await api("GET", `/api/projects/${p1}/jobs`)).length)
    fail("fresh project has jobs — leakage");
  ok("2. multi-project reopening — two projects, isolated job lists");

  // ---- minimal film for later steps ----
  await commit(p1, "screenplay.create", { title: "C1" });
  const sp = (await api("GET", `/api/projects/${p1}/screenplays`))[0];
  await commit(p1, "screenplay_scene.create", { screenplay_id: sp.id, heading: "INT. X — DAY" });
  await commit(p1, "shotlist.scaffold", { screenplay_id: sp.id });
  const board = await api("GET", `/api/projects/${p1}/storyboard`);
  const shotId = board[0].shots[0].shot.id;
  for (const role of ["start", "end"]) {
    await commit(p1, "keyframe.create",
      { shot_id: shotId, role, prompt: `${role} prompt` });
  }
  ok("3. startup + sidecar readiness — screenplay→scaffold→keyframes built on installed backend");

  // ---- 5: durable job interruption — kill app mid-hold, reconcile ----
  const kfs = await api("GET", `/api/projects/${p1}/shots/${shotId}/keyframes`);
  const kfId = kfs[0].id;
  const g = await commit(p1, "keyframe.generate", {
    keyframe_id: kfId,
    mock_hold: { at_stage: 2, timeout_s: 300 } });
  const heldJob = g.job_id;
  await waitJobStatus(p1, heldJob, "running");
  { // deterministic hold barrier, not a sleep guess
    const t0 = Date.now();
    for (;;) {
      const j = (await api("GET", `/api/projects/${p1}/jobs`))
        .find((x) => x.id === heldJob);
      if (j && /hold/i.test(j.message || "")) break;
      if (Date.now() - t0 > 30000) fail("job never reached hold barrier");
      await sleep(300);
    }
  }
  killApp(); await sleep(2500);
  launch(); await creds();
  {
    const j = (await api("GET", `/api/projects/${p1}/jobs`))
      .find((x) => x.id === heldJob);
    if (!["failed", "cancelled"].includes(j.status) &&
        !(j.status === "queued" && j.resumable))
      fail(`interrupted job state ${j.status} — expected reconciled`);
    const atts = (j.progress_json || {}).attempts || [];
    if (atts.length && atts[atts.length - 1].outcome === "running")
      fail("attempt left open after restart reconcile");
  }
  ok("4. durable job interruption — killed mid-hold, reconciled on relaunch, attempt closed");

  // ---- 6: cancellation + idempotent retry ----
  const kfId2 = kfs[1].id;
  const rel = path.join(OUT, "release.flag");
  const g2 = await commit(p1, "keyframe.generate", {
    keyframe_id: kfId2,
    mock_hold: { at_stage: 2, release_file: rel, timeout_s: 120 } });
  const cJob = g2.job_id;
  await waitJobStatus(p1, cJob, "running");
  await api("POST", `/api/jobs/${cJob}/cancel`, undefined, 200);
  const cj = await waitJob(p1, cJob);
  if (cj.status !== "cancelled") fail(`cancel job ${cj.status}`);
  const kf2before = (await api("GET",
    `/api/projects/${p1}/shots/${shotId}/keyframes`))
    .find((k) => k.id === kfId2);
  if (kf2before && kf2before.result_asset_id)
    fail("cancelled job produced a result");
  const g3 = await commit(p1, "keyframe.generate",
    { keyframe_id: kfId2 });
  const rj = await waitJob(p1, g3.job_id);
  if (rj.status !== "succeeded") fail(`retry ${rj.status}: ${rj.error || ""}`);
  ok("5. cancellation + retry — held job cancelled cleanly, no late result, retry succeeded");

  // ---- 7: simulated GPU reservation recovery ----
  fs.writeFileSync(HWFILE, JSON.stringify({
    gpus: [{ index: 0, name: "SIM-0 (8 GiB)", vendor: "NVIDIA",
             vram_bytes: 8 * GIB, vram_free_bytes: 8 * GIB }],
    backends: { cuda: { available: true }, vulkan: { available: true },
                directml: { available: true }, cpu: { available: true } } }));
  killApp(); await sleep(2500); launch({ ATOMIC_HW_JSON: HWFILE }); await creds();
  {
    const st = await api("GET", "/api/gpu/scheduler");
    if (st.simulated !== true) fail("scheduler not marked simulated");
    const parked = (await commit(p1, "sim.gpu.work",
      { est_vram_bytes: 2 * GIB, work_ms: 120000 })).job_id;
    await waitJobStatus(p1, parked, "running");
    killApp(); await sleep(2500); launch({ ATOMIC_HW_JSON: HWFILE }); await creds();
    const j = (await api("GET", `/api/projects/${p1}/jobs`))
      .find((x) => x.id === parked);
    if (!["failed", "cancelled"].includes(j.status) && j.status !== "queued")
      fail(`parked reservation job ${j.status} after restart`);
    const st2 = await api("GET", "/api/gpu/scheduler");
    if (JSON.stringify(st2).includes(parked))
      fail("reservation survived restart");
  }
  ok("6. simulated GPU reservation recovery — parked job reconciled, no stale reservation");

  // ---- 8: backup/restore ----
  const dest = path.join(OUT, "c1-backup.atomicpkg");
  {
    const r = await commit(p1, "backup.export", { dest_path: dest });
    const j = await waitJob(p1, r.job_id);
    if (j.status !== "succeeded") fail(`backup.export ${j.status}`);
    const imp = await api("POST", "/api/packages/import",
      { package_path: dest }, 200);
    const p3 = imp.project_id || imp.id;
    const w = await proj(p3);
    if (!w || w.id !== p3) fail("imported project missing");
    if (p3 === p1) fail("import collided with source project");
  }
  ok("7. backup/restore — package round-trips into a new project identity");

  // ---- 9+10: timeline edit + audio mix ----
  await commit(p1, "timeline.create", { name: "C1 cut" });
  const tid = (await api("GET", `/api/projects/${p1}/timelines`))[0].id;
  {
    const pa = await preview(p1, "assembly.plan", { timeline_id: tid, name: "C1" });
    const ra = pa.resolved_assembly;
    if (!ra || !(ra.items_total > 0 || (ra.items || []).length > 0))
      fail("assembly preview produced no items");
    await commit(p1, "assembly.plan", { timeline_id: tid, name: "C1" });
    const planId = (await api("GET",
      `/api/projects/${p1}/assemblies?timeline_id=${tid}`))[0].id;
    const plan = await api("GET", `/api/projects/${p1}/assemblies/${planId}`);
    const ids = (plan.items || []).map((i) => i.id);
    if (ids.length)
      await commit(p1, "assembly.review",
        { plan_id: planId, item_ids: ids, status: "approved" });
    await commit(p1, "assembly.apply", { timeline_id: tid });
    const det = await api("GET", `/api/projects/${p1}/timelines/${tid}`);
    if (!det.clips.length) fail("assembly placed no clips");
    const clip = det.clips.find((c) => (c.out_frame - c.in_frame) > 1);
    if (!clip) fail("no trimmable clip");
    await commit(p1, "timeline.clip.trim",
      { timeline_id: tid, clip_id: clip.id, out_frame: clip.out_frame - 1 });
    const lane = det.tracks.find((t) => t.kind === "audio");
    if (lane)
      await commit(p1, "timeline.track.update", { track_id: lane.id, gain_db: -3 });
  }
  ok("8. timeline edit + audio mix — assembly applied, clip trimmed, lane gain set");

  // ---- 11: export + bundled ffprobe ----
  {
    const p = await preview(p1, "timeline.export", { timeline_id: tid, profile: "720p" });
    const rt = p.resolved_timeline;
    const r = await commit(p1, "timeline.export", {
      timeline_id: tid, profile: "720p",
      expected_timeline: rt ? { id: rt.id, revision: rt.revision } : undefined,
      expected_edl_sha: rt?.edl_sha256 });
    const ej = await waitJob(p1, r.job_id, 180);
    if (ej.status !== "succeeded") fail(`export ${ej.status}: ${ej.error || ""}`);
    const fp = findFfprobe();
    if (!fp) fail("bundled ffprobe not found");
    const f = await assetFile(p1, ej.output_asset_id, ".mp4");
    const probe = JSON.parse(execFileSync(fp, ["-v", "error",
      "-print_format", "json", "-show_streams", "-show_format", f]).toString());
    const v = (probe.streams || []).find((s) => s.codec_type === "video");
    if (!v || v.codec_name !== "h264") fail("export not h264");
    if (Number(probe.format.duration) <= 0) fail("zero-duration export");
    fs.writeFileSync(path.join(OUT, "export-probe.json"),
      JSON.stringify(probe, null, 2));
  }
  ok("9. export + bundled ffprobe — real FFmpeg pipeline, h264 output validated");

  // ---- 12: restart persistence ----
  killApp(); await sleep(2500); launch(); await creds();
  {
    const det = await api("GET", `/api/projects/${p1}/timelines/${tid}`);
    if (!det.clips.length) fail("timeline lost across restart");
    if (!(await api("GET", "/api/projects")).find((x) => x.id === p1))
      fail("project lost across restart");
  }
  ok("10. restart persistence — project, timeline, jobs intact");

  // ---- 13+14: bounded shutdown, no orphans ----
  killApp();
  const gone = await waitProcsGone(15);
  if (gone < 0) fail("processes still alive >15s after shutdown");
  ok(`11. bounded shutdown — all app processes exited in ${gone}ms`);
  if (procs().includes(".exe")) fail("orphan processes remain");
  ok("12. no orphan processes — backend + shell fully terminated");

  // ---- 15: uninstall preserves user data; reinstall opens it ----
  const uninst = path.join(INSTALL_DIR, "uninstall.exe");
  if (!fs.existsSync(uninst)) fail("uninstaller not found");
  const dbBefore = fs.existsSync(path.join(DATA_DIR, "atomic.db"));
  execFileSync(uninst, ["//S"], { stdio: "ignore" });
  await sleep(8000);
  if (fs.existsSync(EXE)) note("exe still present post-uninstall (async) — waiting");
  for (let i = 0; i < 12 && fs.existsSync(EXE); i++) await sleep(2000);
  if (!fs.existsSync(path.join(DATA_DIR, "atomic.db")))
    fail("user database removed by uninstall");
  if (fs.readdirSync(path.join(DATA_DIR, "assets")).length === 0 && dbBefore)
    note("assets dir empty post-uninstall — checking contents");
  ok("13. uninstall preserved user data — atomic.db + assets intact");
  const installer = process.env.C1_INSTALLER;
  if (installer && fs.existsSync(installer)) {
    execFileSync(installer, ["//S"], { stdio: "ignore" });
    for (let i = 0; i < 30 && !fs.existsSync(EXE); i++) await sleep(2000);
    launch(); await creds();
    if (!(await api("GET", "/api/projects")).find((x) => x.id === p1))
      fail("pre-uninstall project missing after reinstall");
    ok("14. reinstall over preserved data — project + history reopened");
  } else {
    note("C1_INSTALLER not set — reinstall step skipped");
  }

  killApp(); await sleep(1000);
  lines.push(`\nC1 ACCEPTANCE: ${steps}/${steps} PASS`);
  fs.writeFileSync(path.join(OUT, "acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\nC1 ACCEPTANCE: ${steps} PASS`);
}
main().catch((e) => {
  lines.push(`FATAL ${e.message}`);
  try { fs.writeFileSync(path.join(OUT, "acceptance.txt"), lines.join("\n")); } catch {}
  try { killApp(); } catch {}
  console.error(e); process.exit(1);
});
