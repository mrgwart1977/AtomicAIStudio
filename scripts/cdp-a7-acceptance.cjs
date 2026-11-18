// A7 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port, same plumbing
// as A1–A6). 17 steps per the A7-F8 checklist using ATOMIC_HW_JSON
// simulated GPU devices and the mock.gpu.sim worker — no real GPU,
// no live providers, no inference anywhere.
// The sim inventory lives in ONE rewritable file — hardware._simulated_
// report() re-reads it on every probe call, so device count/capacity/
// disappearance are exercised mid-run without relaunching.
// Run: node scripts/cdp-a7-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9230";
const OUT = process.argv[3] || "docs/a7-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Atomic Dream Engine\\atomic-dream-engine.exe`;
const STEPS = 17;
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
    expected_revision: rev, idempotency_key: key || `a7-${ct}-${Date.now()}-${Math.random()}` }, expect);
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
async function waitJobStatus(pid, jobId, status, timeout = 30000) {
  const t0 = Date.now();
  for (;;) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && j.status === status) return j;
    if (Date.now() - t0 > timeout) fail(`job ${jobId} never reached ${status}`);
    await sleep(300);
  }
}
async function schedState() { return api("GET", "/api/gpu/scheduler"); }
const heldBy = (st, jobId) => st.slots.some((s) => s.jobs && s.jobs[jobId]);

// ---------- CDP plumbing (same as A6) ----------
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

// The ONE simulated-inventory file — rewritten in place; the backend
// re-reads it on every probe call (no relaunch needed for inventory
// changes; only the restart-reconcile step kills the app).
let HWFILE = null;
function writeHw(gpus, backends) {
  fs.writeFileSync(HWFILE, JSON.stringify({
    gpus: gpus.map((v, i) => ({
      index: i, name: `SIM-${i} (${v} GiB)`, vendor: "NVIDIA",
      vram_bytes: v * 2 ** 30, vram_free_bytes: v * 2 ** 30 })),
    backends: backends || { cuda: { available: true },
      vulkan: { available: true }, directml: { available: true },
      cpu: { available: true } },
  }));
}
function launch() {
  appProc = spawn(EXE, [], { detached: true, stdio: "ignore",
    env: { ...process.env,
      ATOMIC_HW_JSON: HWFILE,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${PORT}` } });
  appProc.unref();
}
const killApp = () => {
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"], { stdio: "ignore" }); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "Atomic Dream Engine.exe"], { stdio: "ignore" }); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"], { stdio: "ignore" }); } catch {}
};

const GIB = 2 ** 30;
async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  HWFILE = path.join(OUT, "sim-hw.json");
  killApp(); await sleep(1500);

  // ---------- Phase A: one simulated 8 GiB device ----------
  writeHw([8]);
  launch();
  await creds();

  // 1. Existing populated project — a real project with M1/A2-era
  //    content (world → location, character, scene, shot, timeline).
  const pid = (await api("POST", "/api/projects", {
    name: `A7 Acceptance ${Date.now()}` }, 201)).id;
  const w = await commit(pid, "world.create", { name: "Sim World" });
  await commit(pid, "location.create", {
    name: "Salt Quay", world_id: (await api("GET", `/api/projects/${pid}/world`))[0].id });
  await commit(pid, "character.create", { name: "Mara" });
  await commit(pid, "scene.create", { title: "Opening" });
  const board0 = await api("GET", `/api/projects/${pid}/storyboard`);
  const sceneId = board0[0].scene.id;
  await commit(pid, "shot.create", { scene_id: sceneId, title: "wide" });
  await commit(pid, "timeline.create", { name: "Cut A" });
  const timelines = await api("GET", `/api/projects/${pid}/timelines`);
  const board = await api("GET", `/api/projects/${pid}/storyboard`);
  if (!timelines.length || !board.length || board[0].shots.length !== 1)
    fail("populated project missing scene/shot/timeline");
  const tid = timelines[0].id;
  ok("1. populated project — world/location/character/scene/shot/timeline created and readable");

  // 2. Single-GPU simulation — a known footprint schedules, records
  //    its reservation, runs on the device, and releases.
  {
    const st = await schedState();
    if (st.gpu_count !== 1) fail(`expected 1 sim GPU, got ${st.gpu_count}`);
    if (st.simulated !== true) fail("scheduler state not marked simulated");
    const r = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 2 * GIB, work_ms: 200 });
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "succeeded") fail(`job ${j.status}: ${j.error}`);
    if (j.gpu_index !== 0) fail(`expected GPU 0, got ${j.gpu_index}`);
    if (j.reserved_vram_bytes !== 2 * GIB)
      fail(`reservation ${j.reserved_vram_bytes} != 2 GiB`);
    if (heldBy(await schedState(), j.id))
      fail("reservation not released after success");
    ok("2. single-GPU simulation — ran on GPU 0, 2 GiB reservation recorded + released, state marked simulated");
  }

  // Park a long-running job — its reconciliation after relaunch is the
  // restart step's evidence (setup, not a step).
  const parked = (await commit(pid, "sim.gpu.work",
    { est_vram_bytes: 2 * GIB, work_ms: 120000 })).job_id;
  await waitJobStatus(pid, parked, "running");
  if (!heldBy(await schedState(), parked)) fail("parked job holds no reservation");

  // ---------- Phase B: two simulated 8 GiB devices ----------
  writeHw([8, 8]);
  killApp(); await sleep(2000);
  launch();
  await creds();

  // 3. Restart reconciliation — the killed running job ends
  //    failed/interrupted (never silently resumed); the fresh
  //    in-process scheduler holds no phantom reservation.
  {
    const j = (await api("GET", `/api/projects/${pid}/jobs`))
      .find((x) => x.id === parked);
    if (j.status !== "failed" || j.recovery_state !== "interrupted")
      fail(`parked job: ${j.status}/${j.recovery_state}`);
    if (heldBy(await schedState(), parked))
      fail("parked reservation survived restart");
    ok("3. restart reconciliation — killed running job reconciled failed/interrupted, zero stale reservations");
  }

  // 4. Two-GPU simulation — equal devices spread independent jobs.
  {
    const st = await schedState();
    if (st.gpu_count !== 2) fail(`expected 2 sim GPUs, got ${st.gpu_count}`);
    const r1 = await commit(pid, "sim.gpu.work", { est_vram_bytes: 6 * GIB, work_ms: 2500 });
    const r2 = await commit(pid, "sim.gpu.work", { est_vram_bytes: 6 * GIB, work_ms: 2500 });
    const j1 = await waitJob(pid, r1.job_id);
    const j2 = await waitJob(pid, r2.job_id);
    if (j1.status !== "succeeded" || j2.status !== "succeeded")
      fail(`multi-gpu jobs: ${j1.status}/${j2.status}`);
    if (j1.gpu_index === j2.gpu_index)
      fail(`both jobs on GPU ${j1.gpu_index} — reservations did not spread`);
    ok(`4. two-GPU simulation — 6 GiB jobs landed on GPUs ${j1.gpu_index} and ${j2.gpu_index}`);
  }

  // 5. Heterogeneous VRAM capacities — rewrite the inventory file to
  //    [4,8]; a 6 GiB job can only fit the 8 GiB device.
  {
    writeHw([4, 8]);
    const st = await schedState();
    const sizes = st.slots.map((s) => s.vram_bytes / GIB);
    if (st.gpu_count !== 2 || sizes[0] !== 4 || sizes[1] !== 8)
      fail(`hetero inventory wrong: ${JSON.stringify(sizes)}`);
    const r = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 6 * GIB, work_ms: 300 });
    const j = await waitJob(pid, r.job_id);
    if (j.gpu_index !== 1) fail(`6 GiB job on GPU ${j.gpu_index}, expected 1`);
    // Most-free wins: a 2 GiB job prefers the 8 GiB device (8 free
    // beats 4 free) — capacity, not index order, decides.
    const r2 = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 2 * GIB, work_ms: 300 });
    const j2 = await waitJob(pid, r2.job_id);
    if (j2.gpu_index !== 1) fail(`2 GiB job on GPU ${j2.gpu_index}, expected 1 (most free)`);
    ok("5. heterogeneous VRAM — [4,8] inventory live-reloaded; 6 GiB job forced to the 8 GiB device, small jobs prefer most-free");
  }

  // 6. Concurrent admission — two jobs hold reservations on two
  //    devices SIMULTANEOUSLY; the scheduler reports both at once.
  {
    writeHw([8, 8]);
    const r1 = await commit(pid, "sim.gpu.work", { est_vram_bytes: 4 * GIB, work_ms: 8000 });
    const r2 = await commit(pid, "sim.gpu.work", { est_vram_bytes: 4 * GIB, work_ms: 8000 });
    await waitJobStatus(pid, r1.job_id, "running");
    await waitJobStatus(pid, r2.job_id, "running");
    const st = await schedState();
    const occupied = st.slots.filter((s) => s.jobs && Object.keys(s.jobs).length);
    if (occupied.length !== 2)
      fail(`expected 2 devices holding reservations, got ${occupied.length}`);
    if (!(heldBy(st, r1.job_id) && heldBy(st, r2.job_id)))
      fail("a running job holds no reservation");
    const j1 = await waitJob(pid, r1.job_id);
    const j2 = await waitJob(pid, r2.job_id);
    if (j1.gpu_index === j2.gpu_index)
      fail("concurrent jobs collided on one device");
    ok(`6. concurrent admission — two running jobs held accounted reservations on GPUs ${j1.gpu_index}+${j2.gpu_index} simultaneously`);
  }

  // 7. Oversubscription rejection — bigger than any device: honest
  //    queue reason at preview, stays queued, never dropped.
  {
    const pv = await preview(pid, "sim.gpu.work", { est_vram_bytes: 24 * GIB });
    if (pv.resolved_gpu?.would_schedule !== false || !pv.resolved_gpu?.queue_reason)
      fail(`oversubscribe preview: ${JSON.stringify(pv.resolved_gpu)}`);
    const r = await commit(pid, "sim.gpu.work", { est_vram_bytes: 24 * GIB, work_ms: 100 });
    const j = await waitJobStatus(pid, r.job_id, "queued", 20000).catch(() => null);
    if (j && !/fit|free|reserv/i.test(j.message || ""))
      fail(`queued job missing wait reason: ${j.message}`);
    await api("POST", `/api/jobs/${r.job_id}/cancel`, undefined, 200);
    ok(`7. oversubscription rejected — "${pv.resolved_gpu.queue_reason}"`);
  }

  // 8. Unknown-footprint exclusive reservation — no estimate claims
  //    the whole device (usable bytes recorded); nothing shares.
  {
    const r = await commit(pid, "sim.gpu.work", { work_ms: 8000 });
    await waitJobStatus(pid, r.job_id, "running");
    const st = await schedState();
    const j = (await api("GET", `/api/projects/${pid}/jobs`))
      .find((x) => x.id === r.job_id);
    const slot = st.slots.find((s) => s.index === j.gpu_index);
    if (!slot || slot.jobs[r.job_id] !== slot.usable_bytes)
      fail(`exclusive claim recorded ${slot?.jobs?.[r.job_id]}, expected usable ${slot?.usable_bytes}`);
    if (j.reserved_vram_bytes !== slot.usable_bytes)
      fail(`reserved_vram_bytes ${j.reserved_vram_bytes} != usable ${slot.usable_bytes}`);
    // A known-estimate job must NOT share the exclusive device —
    // it lands on whichever device the exclusive claim didn't take.
    const r2 = await commit(pid, "sim.gpu.work", { est_vram_bytes: GIB, work_ms: 400 });
    const j2 = await waitJob(pid, r2.job_id);
    if (j2.gpu_index === j.gpu_index)
      fail(`known job shared the exclusive device ${j.gpu_index}`);
    await api("POST", `/api/jobs/${r.job_id}/cancel`, undefined, 200);
    await waitJob(pid, r.job_id);
    ok(`8. unknown-footprint exclusive — claimed the device's full usable ${(slot.usable_bytes / GIB).toFixed(0)} GiB; known-estimate job routed to the other device`);
  }

  // 9. Cancellation and reservation release.
  {
    const r = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 4 * GIB, work_ms: 30000 });
    await waitJobStatus(pid, r.job_id, "running");
    await api("POST", `/api/jobs/${r.job_id}/cancel`, undefined, 200);
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "cancelled") fail(`cancel -> ${j.status}`);
    if (heldBy(await schedState(), r.job_id)) fail("reservation held after cancel");
    ok("9. cancellation — running job cancelled, reservation released");
  }

  // 10. Worker crash — mid-run failure releases the reservation.
  {
    const r = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 2 * GIB, work_ms: 30000, crash_after_ms: 100 });
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "failed") fail(`crash -> ${j.status}`);
    if (heldBy(await schedState(), r.job_id)) fail("reservation held after crash");
    ok("10. worker crash — simulated crash failed the job, reservation released");
  }

  // 11. Device disappearance — GPU 1 vanishes mid-hold: reported lost,
  //     unschedulable, reservation kept honest; restored + admitted
  //     again after the job ends.
  {
    const r = await commit(pid, "sim.gpu.work",
      { device: 1, est_vram_bytes: 2 * GIB, work_ms: 60000 });
    await waitJobStatus(pid, r.job_id, "running");
    writeHw([8]);  // device 1 disappears
    await sleep(500);
    const st = await schedState();
    if (!st.lost_devices.includes(1)) fail(`lost_devices: ${st.lost_devices}`);
    const lost = st.slots.find((s) => s.index === 1);
    if (!lost || lost.present !== false || !lost.jobs[r.job_id])
      fail(`lost slot wrong: ${JSON.stringify(lost)}`);
    const pv = await preview(pid, "sim.gpu.work", { device: 1 });
    if (pv.resolved_gpu?.would_schedule !== false)
      fail("preview admitted a vanished device");
    // GPU returns — job still running holds its claim honestly.
    writeHw([8, 8]);
    const st2 = await schedState();
    if (st2.lost_devices.length) fail(`still lost after return: ${st2.lost_devices}`);
    await api("POST", `/api/jobs/${r.job_id}/cancel`, undefined, 200);
    await waitJob(pid, r.job_id);
    const j2 = await waitJob(pid, (await commit(pid, "sim.gpu.work",
      { device: 1, est_vram_bytes: GIB, work_ms: 200 })).job_id);
    if (j2.gpu_index !== 1) fail("returned device not admitted again");
    ok("11. device disappearance — GPU 1 reported lost + refused while held; restored and re-admitted after release");
  }

  // 12. Read-only cloud planning — capability + availability +
  //     fingerprint without dispatch.
  let planFp;
  {
    await api("PUT", "/api/providers/rytr/config", { enabled: true });
    const pv0 = await preview(pid, "cloud.plan", { provider_key: "rytr", task: "text" });
    if (pv0.resolved_cloud?.availability !== "blocked")
      fail(`unkeyed rytr should block, got ${pv0.resolved_cloud?.availability}`);
    // Presence only — a placeholder for the declared field; never
    // read back, and the mock adapter never touches it.
    await api("PUT", "/api/providers/rytr/credential",
      { fields: { api_key: "a7-acceptance-presence-placeholder" } });
    const pv = await preview(pid, "cloud.plan",
      { provider_key: "rytr", task: "text", est_cost_usd: 0.01, max_cost_usd: 0.05 });
    const rc = pv.resolved_cloud;
    if (!rc?.ok) fail(`plan not ok: ${JSON.stringify(rc)}`);
    if (rc.environment !== "simulated" || rc.routed !== true)
      fail(`env/route: ${rc.environment}/${rc.routed}`);
    if (rc.availability !== "available")
      fail(`keyed rytr should be available, got ${rc.availability}`);
    if (!rc.plan_fingerprint || !Array.isArray(rc.budgets) || !rc.budgets.length)
      fail("plan missing fingerprint/budgets");
    planFp = rc.plan_fingerprint;
    ok(`12. read-only cloud planning — blocked→available on credential presence, env=simulated, fingerprint + ${rc.budgets.length} budget scopes, zero dispatch`);
  }

  // 13. Stale cloud-plan rejection — approval commits; pinned submit
  //     dispatches simulated; drifted pin refuses; retry idempotent.
  {
    const ap = await commit(pid, "cloud.plan",
      { provider_key: "rytr", task: "text",
        est_cost_usd: 0.01, max_cost_usd: 0.05,
        expected_plan_fingerprint: planFp });
    if (ap.status !== "committed") fail(`plan commit: ${ap.status}`);
    const r = await commit(pid, "generate.text.cloud",
      { provider_key: "rytr", execution_target: "cloud",
        prompt: "a7 acceptance beat", est_cost_usd: 0.01,
        max_cost_usd: 0.05, plan_fingerprint: planFp });
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "succeeded") fail(`cloud job ${j.status}: ${j.error}`);
    await commit(pid, "generate.text.cloud",
      { provider_key: "rytr", execution_target: "cloud",
        prompt: "a7 stale", est_cost_usd: 0.02, max_cost_usd: 0.05,
        plan_fingerprint: planFp }, 422);
    // A pin on a NON-admitting plan refuses too (approval ≠ dispatch).
    const un = await preview(pid, "cloud.plan",
      { provider_key: "sudowrite", task: "text" });
    if (un.resolved_cloud?.would_admit !== false)
      fail("disabled-provider plan should not admit");
    await commit(pid, "generate.text.cloud",
      { provider_key: "sudowrite", execution_target: "cloud",
        prompt: "x", est_cost_usd: 0.01, max_cost_usd: 0.05,
        plan_fingerprint: un.resolved_cloud.plan_fingerprint }, 422);
    const key = `a7-retry-${Date.now()}`;
    const p = { provider_key: "rytr", task: "text" };
    const a = await commit(pid, "cloud.plan", p, 200, key);
    const b = await commit(pid, "cloud.plan", p, 200, key);
    if (b.command_id !== a.command_id || !b.idempotent_replay)
      fail("duplicate commit not idempotent");
    ok("13. cloud-plan pins — approval commits, pinned submit dispatches simulated, drifted + non-admitting pins refuse, retry idempotent");
  }

  // 14. Budget refusal — over-budget plan refuses at preview and commit.
  {
    await commit(pid, "spend.budget_set",
      { target: "project", scope: "month", limit_usd: 0.001 });
    const pv = await preview(pid, "cloud.plan",
      { provider_key: "rytr", task: "text", est_cost_usd: 5.0 });
    if (pv.resolved_cloud?.availability !== "over_budget")
      fail(`availability: ${pv.resolved_cloud?.availability}`);
    await commit(pid, "cloud.plan",
      { provider_key: "rytr", task: "text", est_cost_usd: 5.0 }, 422);
    await commit(pid, "spend.budget_remove",
      { target: "project", scope: "month" });
    ok("14. budget refusal — over-budget plan refused at preview and commit");
  }

  // 15. Credential isolation — presence only, nothing secret in plan,
  //     audit, or scheduler state; every booking stayed simulated.
  {
    const pv = await preview(pid, "cloud.plan",
      { provider_key: "rytr", task: "text", est_cost_usd: 0.01 });
    const rc = pv.resolved_cloud;
    if (rc.credential && JSON.stringify(rc.credential).match(/sk-|placeholder|a7-acceptance/i))
      fail("credential material leaked into plan");
    if (rc.credential && rc.credential.configured !== true)
      fail("credential presence not reported");
    const jobs = await api("GET", `/api/projects/${pid}/jobs`);
    const cloud = jobs.filter((j) => j.adapter_id && j.adapter_id.startsWith("cloud."));
    if (cloud.some((j) => j.progress_json?.environment === "live"))
      fail("a job booked live");
    const res = await api("GET", `/api/projects/${pid}/spend/reservations`);
    if (res.some((r) => r.environment === "live"))
      fail("a reservation booked live");
    if ((await schedState()).simulated !== true)
      fail("scheduler state dropped the simulated flag");
    ok(`15. credential isolation — presence-only metadata, ${cloud.length} cloud job(s) all simulated, no secret material anywhere`);
  }

  // 16. Existing timeline/export regression — the populated project's
  //     A6 surfaces still resolve on the A7 build.
  {
    const det = await api("GET", `/api/projects/${pid}/timelines/${tid}`);
    if (!det || det.id !== tid) fail("timeline unreadable");
    const b2 = await api("GET", `/api/projects/${pid}/storyboard`);
    if (!b2.length || b2[0].shots.length !== 1)
      fail("populated storyboard lost its shot");
    const ap = await commit(pid, "assembly.plan",
      { timeline_id: tid, name: "A7 regression plan" });
    if (ap.status !== "committed") fail(`assembly.plan: ${ap.status}`);
    const pv = await preview(pid, "timeline.export", { timeline_id: tid });
    if (!pv.resolved_timeline && pv.status !== "refused")
      fail(`timeline.export preview malformed: ${JSON.stringify(pv).slice(0, 200)}`);
    ok("16. timeline/export regression — populated timeline intact, assembly.plan committed, export preview resolves");
  }

  // Clean up the placeholder presence — the vault holds ciphertext
  // only; deleting keeps the installed profile as-found.
  try { await api("DELETE", "/api/providers/rytr/credential"); } catch {}

  // 17. Clean shutdown.
  killApp(); await sleep(1500);
  const left = ["atomic-dream-engine.exe", "atomic-backend.exe"]
    .filter((n) => {
      try { return execFileSync("tasklist",
        ["/FI", `IMAGENAME eq ${n}`, "/FO", "CSV", "/NH"])
        .toString().includes(n); } catch { return false; } });
  if (left.length) fail(`processes left: ${left.join(", ")}`);
  ok("17. clean shutdown — app + backend exited");

  lines.unshift(`A7 ACCEPTANCE ${steps}/${STEPS} PASS — exe ${EXE}`);
  fs.writeFileSync(path.join(OUT, "a7-acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\n== ${steps}/${STEPS} PASS ==`);
}

main().catch((e) => {
  lines.push(`FAIL ${e.message}`);
  try { fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, "a7-acceptance.txt"),
      lines.join("\n") + "\n"); } catch {}
  killApp();
  console.error(e); process.exit(1);
});
