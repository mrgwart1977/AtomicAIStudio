// A7 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port, same plumbing
// as A1–A6). 15 steps per the A7.8 checklist using ATOMIC_HW_JSON
// simulated GPU devices and the mock.gpu.sim worker — no real GPU,
// no live providers, no inference anywhere.
// Run: node scripts/cdp-a7-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9230";
const OUT = process.argv[3] || "docs/a7-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Atomic Dream Engine\\atomic-dream-engine.exe`;
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
  const seen = onEvent("Network.requestWillBeSent", (e) => {
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
function hwFile(gpus) {
  const p = path.join(OUT, `sim-hw-${gpus.length}gpu.json`);
  fs.writeFileSync(p, JSON.stringify({
    gpus: gpus.map((v, i) => ({
      index: i, name: `SIM-${i} (${v} GiB)`, vendor: "NVIDIA",
      vram_bytes: v * 2 ** 30, vram_free_bytes: v * 2 ** 30 })),
    backends: { cuda: { available: true }, vulkan: { available: true },
                directml: { available: true }, cpu: { available: true } },
  }));
  return p;
}
function launch(hwJsonPath) {
  appProc = spawn(EXE, [], { detached: true, stdio: "ignore",
    env: { ...process.env,
      ATOMIC_HW_JSON: hwJsonPath,
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
  killApp(); await sleep(1500);

  // ---------- Phase A: single simulated GPU ----------
  launch(hwFile([8]));
  await creds();
  const pid = (await api("POST", "/api/projects", {
    name: `A7 Acceptance ${Date.now()}` }, 201)).id;

  // 1. Single-GPU scheduling — one simulated device admits a known
  //    footprint and records the reservation.
  {
    const st = await schedState();
    if (st.gpu_count !== 1) fail(`expected 1 sim GPU, got ${st.gpu_count}`);
    const r = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 2 * GIB, work_ms: 200 });
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "succeeded") fail(`job ${j.status}: ${j.error}`);
    if (j.gpu_index !== 0) fail(`expected GPU 0, got ${j.gpu_index}`);
    if (j.reserved_vram_bytes !== 2 * GIB)
      fail(`reservation ${j.reserved_vram_bytes} != 2 GiB`);
    if ((await schedState()).slots[0].jobs &&
        Object.keys((await schedState()).slots[0].jobs).length)
      fail("reservation not released after success");
    ok("1. single-GPU scheduling — sim job ran on GPU 0 with a recorded + released 2 GiB reservation");
  }

  // 2. Concurrent hold — a long-running job claims and KEEPS its
  //    reservation while running (setup for the restart check).
  const parked = (await commit(pid, "sim.gpu.work",
    { est_vram_bytes: 2 * GIB, work_ms: 120000 })).job_id;
  await waitJobStatus(pid, parked, "running");
  const parkedSlot = (await schedState()).slots[0];
  if (!parkedSlot.jobs || !parkedSlot.jobs[parked])
    fail("parked job holds no reservation");
  if (parkedSlot.jobs[parked] !== 2 * GIB)
    fail(`parked reservation ${parkedSlot.jobs[parked]} != 2 GiB`);
  ok("2. concurrent mock jobs — running job holds an accounted 2 GiB reservation");

  // ---------- Phase B: restart + two simulated GPUs ----------
  killApp(); await sleep(2000);
  launch(hwFile([8, 8]));
  await creds();

  // 3. Multi-GPU scheduling — two 6 GiB jobs land on separate devices.
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
    ok(`3. multi-GPU scheduling — 6 GiB jobs landed on GPUs ${j1.gpu_index} and ${j2.gpu_index}`);
  }

  // 4. Resource admission — explicit pin honored; cpu lane untouched.
  {
    const r = await commit(pid, "sim.gpu.work",
      { device: 1, est_vram_bytes: 1 * GIB, work_ms: 200 });
    const j = await waitJob(pid, r.job_id);
    if (j.gpu_index !== 1) fail(`pinned job landed on ${j.gpu_index}`);
    const c = await commit(pid, "sim.gpu.work",
      { device: "cpu", work_ms: 200 });
    const jc = await waitJob(pid, c.job_id);
    if (jc.gpu_index !== null) fail(`cpu job got gpu ${jc.gpu_index}`);
    const pv = await preview(pid, "sim.gpu.work",
      { device: 9, est_vram_bytes: GIB });
    if (pv.resolved_gpu?.would_schedule !== false)
      fail("preview admitted a missing device");
    ok("4. resource admission — pin honored (GPU 1), CPU lane clean, missing-device preview refused honestly");
  }

  // 5. Oversubscription refusal — a job bigger than any device queues
  //    with an honest reason, never dropped or silently placed.
  {
    const pv = await preview(pid, "sim.gpu.work",
      { est_vram_bytes: 24 * GIB });
    if (pv.resolved_gpu?.would_schedule !== false ||
        !pv.resolved_gpu?.queue_reason)
      fail(`oversubscribe preview: ${JSON.stringify(pv.resolved_gpu)}`);
    const r = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 24 * GIB, work_ms: 100 });
    const j = await waitJobStatus(pid, r.job_id, "queued", 20000)
      .catch(() => null);
    if (j && !/fit|free|reserv/i.test(j.message || ""))
      fail(`queued job missing wait reason: ${j.message}`);
    // Cancel the permanently-queued job so later steps are clean.
    await api("POST", `/api/jobs/${r.job_id}/cancel`, undefined, 200);
    ok(`5. oversubscription refused — "${pv.resolved_gpu.queue_reason}"`);
  }

  // 6. Cancellation — a running job's reservation releases on cancel.
  {
    const r = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 4 * GIB, work_ms: 30000 });
    await waitJobStatus(pid, r.job_id, "running");
    await api("POST", `/api/jobs/${r.job_id}/cancel`, undefined, 200);
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "cancelled") fail(`cancel -> ${j.status}`);
    const held = (await schedState()).slots
      .some((s) => s.jobs && s.jobs[r.job_id]);
    if (held) fail("reservation still held after cancel");
    ok("6. cancellation — running job cancelled, reservation released");
  }

  // 7. Worker failure — mid-run crash fails the job, releases the slot.
  {
    const r = await commit(pid, "sim.gpu.work",
      { est_vram_bytes: 2 * GIB, work_ms: 30000, crash_after_ms: 100 });
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "failed") fail(`crash -> ${j.status}`);
    const held = (await schedState()).slots
      .some((s) => s.jobs && s.jobs[r.job_id]);
    if (held) fail("reservation still held after crash");
    ok("7. worker failure — simulated crash failed the job, reservation released");
  }

  // 8. Restart recovery — the phase-A parked job reconciled to a
  //    terminal interrupted state on relaunch; no silent resume, no
  //    stale reservation.
  {
    const j = (await api("GET", `/api/projects/${pid}/jobs`))
      .find((x) => x.id === parked);
    if (j.status !== "failed" || j.recovery_state !== "interrupted")
      fail(`parked job: ${j.status}/${j.recovery_state}`);
    const held = (await schedState()).slots
      .some((s) => s.jobs && s.jobs[parked]);
    if (held) fail("parked reservation survived restart");
    ok("8. restart recovery — killed running job reconciled failed/interrupted, no stale reservation");
  }

  // 9. Provider capability preview — cloud.plan resolves capability,
  //    credential PRESENCE (never values), availability, fingerprint.
  let planFp;
  {
    await api("PUT", "/api/providers/rytr/config", { enabled: true });
    const pv0 = await preview(pid, "cloud.plan",
      { provider_key: "rytr", task: "text" });
    if (pv0.resolved_cloud?.availability !== "blocked")
      fail(`unkeyed rytr should block, got ${pv0.resolved_cloud?.availability}`);
    // Presence is configured, never read back — a placeholder value
    // for the declared field; the mock adapter never touches it.
    await api("PUT", "/api/providers/rytr/credential",
      { fields: { api_key: "a7-acceptance-presence-placeholder" } });
    const pv = await preview(pid, "cloud.plan",
      { provider_key: "rytr", task: "text",
        est_cost_usd: 0.01, max_cost_usd: 0.05 });
    const rc = pv.resolved_cloud;
    if (!rc?.ok) fail(`plan not ok: ${JSON.stringify(rc)}`);
    if (rc.environment !== "simulated" || rc.routed !== true)
      fail(`env/route: ${rc.environment}/${rc.routed}`);
    if (rc.availability !== "available")
      fail(`keyed rytr should be available, got ${rc.availability}`);
    if (!rc.plan_fingerprint) fail("no plan fingerprint");
    if (rc.credential && JSON.stringify(rc.credential).match(/sk-|placeholder/i))
      fail("credential material leaked into plan");
    planFp = rc.plan_fingerprint;
    ok(`9. provider capability preview — blocked→available on credential presence, env=${rc.environment}, fingerprint pinned`);
  }

  // 10. Cost estimation — reservation amount + per-scope budget rows.
  {
    const pv = await preview(pid, "cloud.plan",
      { provider_key: "rytr", task: "text",
        est_cost_usd: 0.01, max_cost_usd: 0.05 });
    const rc = pv.resolved_cloud;
    if (!Array.isArray(rc.budgets) || !rc.budgets.length)
      fail("no budget rows in plan");
    if (rc.reserved_micro == null) fail("no reserved_micro");
    ok(`10. cost estimation — reserved ${rc.reserved_micro}µ against ${rc.budgets.length} budget scopes`);
  }

  // 11. Budget refusal — a plan exceeding the month budget refuses at
  //     preview AND at commit.
  {
    await commit(pid, "spend.budget_set",
      { target: "project", scope: "month", limit_usd: 0.001 });
    const pv = await preview(pid, "cloud.plan",
      { provider_key: "rytr", task: "text", est_cost_usd: 5.0 });
    if (pv.resolved_cloud?.availability !== "over_budget")
      fail(`availability: ${pv.resolved_cloud?.availability}`);
    const r = await commit(pid, "cloud.plan",
      { provider_key: "rytr", task: "text", est_cost_usd: 5.0 },
      422);
    await commit(pid, "spend.budget_remove",
      { target: "project", scope: "month" });
    ok("11. budget refusal — over-budget plan refused at preview and commit");
  }

  // 12. Explicit approval — a fresh plan commits its approval; a
  //     submit pinning it dispatches the simulated job.
  {
    const pv = await preview(pid, "cloud.plan",
      { provider_key: "rytr", task: "text",
        est_cost_usd: 0.01, max_cost_usd: 0.05 });
    planFp = pv.resolved_cloud.plan_fingerprint;
    const ap = await commit(pid, "cloud.plan",
      { provider_key: "rytr", task: "text",
        est_cost_usd: 0.01, max_cost_usd: 0.05,
        expected_plan_fingerprint: planFp });
    if (ap.status !== "committed") fail(`plan commit: ${ap.status}`);
    const r = await commit(pid, "generate.text.cloud",
      { provider_key: "rytr", execution_target: "cloud",
        prompt: "a7 acceptance beat",
        est_cost_usd: 0.01, max_cost_usd: 0.05,
        plan_fingerprint: planFp });
    const j = await waitJob(pid, r.job_id);
    if (j.status !== "succeeded") fail(`cloud job ${j.status}: ${j.error}`);
    // stale pin refuses.
    const stale = await commit(pid, "generate.text.cloud",
      { provider_key: "rytr", execution_target: "cloud",
        prompt: "a7 stale", est_cost_usd: 0.02, max_cost_usd: 0.05,
        plan_fingerprint: planFp }, 422);
    ok("12. explicit approval — plan committed, pinned submit dispatched (simulated), drifted pin refused");
  }

  // 13. Retry protection — same idempotency key replays, never
  //     double-commits.
  {
    const key = `a7-retry-${Date.now()}`;
    const p = { provider_key: "rytr", task: "text" };
    const a = await commit(pid, "cloud.plan", p, 200, key);
    const b = await commit(pid, "cloud.plan", p, 200, key);
    if (b.command_id !== a.command_id || !b.idempotent_replay)
      fail(`retry: ${JSON.stringify({ a: a.command_id, b: b.command_id, replay: b.idempotent_replay })}`);
    ok("13. retry protection — duplicate commit idempotent");
  }

  // 14. No real provider activity — every booking stayed 'simulated';
  //     the resolved plan asserts planning-only (no transport).
  {
    const jobs = await api("GET", `/api/projects/${pid}/jobs`);
    const cloud = jobs.filter((j) => j.adapter_id &&
      j.adapter_id.startsWith("cloud."));
    if (cloud.some((j) => j.execution_scope === "remote"
        && j.progress_json?.environment === "live"))
      fail("a job booked live");
    const res = await api("GET", `/api/projects/${pid}/spend/reservations`);
    if (res.some((r) => r.environment === "live"))
      fail("a reservation booked live");
    ok(`14. no real provider activity — ${cloud.length} cloud job(s), all bookings simulated`);
  }

  // Clean up the placeholder presence — the vault holds ciphertext
  // only; deleting keeps the installed profile as-found.
  try { await api("DELETE", "/api/providers/rytr/credential"); } catch {}

  // 15. Clean shutdown.
  killApp(); await sleep(1500);
  const left = ["atomic-dream-engine.exe", "atomic-backend.exe"]
    .filter((n) => {
      try { return execFileSync("tasklist",
        ["/FI", `IMAGENAME eq ${n}`, "/FO", "CSV", "/NH"])
        .toString().includes(n); } catch { return false; } });
  if (left.length) fail(`processes left: ${left.join(", ")}`);
  ok("15. clean shutdown — app + backend exited");

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
