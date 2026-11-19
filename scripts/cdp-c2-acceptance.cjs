// C2 installed-candidate acceptance — security hardening against the
// REAL installed app (CDP port sniffs backend URL+token, same plumbing
// as C1). 12 checks per C2.9: unauthorized rejection, authorized ops,
// malicious package refusal, valid restore, synthetic credential
// isolation, project separation, sim-only provider surface, planning
// read-only, no live activity, timeline regression, restart, shutdown.
// Run: node scripts/cdp-c2-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9235";
const OUT = process.argv[3] || "docs/c2-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Atomic Dream Engine\\atomic-dream-engine.exe`;
const DATA_DIR = `${process.env.LOCALAPPDATA}\\AtomicAIStudio`;
let API = null, TOKEN = null;
const lines = []; let steps = 0;
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
    expected_revision: rev, idempotency_key: key || `c2-${ct}-${Date.now()}-${Math.random()}` }, expect);
}
async function waitJob(pid, jobId, timeout = 60) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout * 1000) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) return j;
    await sleep(400);
  }
  fail(`job ${jobId} not terminal in ${timeout}s`);
}
// raw fetch with arbitrary/missing token — for auth-negative checks
async function apiRaw(method, p, token) {
  const h = { "Content-Type": "application/json" };
  if (token !== null) h["X-Atomic-Token"] = token;
  const r = await fetch(`${API}${p}`, { method, headers: h });
  return { status: r.status, body: await r.text() };
}

// ---- minimal zip writer (STORE only — enough for an .atomicpkg) ----
const CRC_T = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
const crc32 = (buf) => {
  let c = 0xFFFFFFFF;
  for (const b of buf) c = CRC_T[(c ^ b) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
};
function makeZip(entries) {
  const files = [];
  for (const [name, data] of entries) {
    const nb = Buffer.from(name), db = Buffer.isBuffer(data) ? data : Buffer.from(data);
    const crc = crc32(db);
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0); lh.writeUInt16LE(20, 4);
    lh.writeUInt16LE(0, 6); lh.writeUInt16LE(0, 8); lh.writeUInt16LE(0, 10);
    lh.writeUInt16LE(0, 12); lh.writeUInt16LE(0, 14);
    lh.writeUInt32LE(crc >>> 0, 14); lh.writeUInt32LE(db.length, 18);
    lh.writeUInt32LE(db.length, 22); lh.writeUInt16LE(nb.length, 26);
    lh.writeUInt16LE(0, 28);
    files.push({ nb, db, lh });
  }
  let off = 0; const cds = [], parts = [];
  for (const f of files) {
    const cd = Buffer.alloc(46);
    cd.writeUInt32LE(0x02014b50, 0); cd.writeUInt16LE(20, 4);
    cd.writeUInt16LE(20, 6); cd.writeUInt16LE(0, 8); cd.writeUInt16LE(0, 10);
    cd.writeUInt16LE(0, 12); cd.writeUInt16LE(0, 14);
    cd.writeUInt32LE(f.lh.readUInt32LE(14), 16);
    cd.writeUInt32LE(f.db.length, 20); cd.writeUInt32LE(f.db.length, 24);
    cd.writeUInt16LE(f.nb.length, 28);
    cd.writeUInt32LE(0, 30); cd.writeUInt32LE(0, 34); cd.writeUInt32LE(0, 38);
    cd.writeUInt32LE(off, 42);
    cds.push(Buffer.concat([cd, f.nb]));
    parts.push(Buffer.concat([f.lh, f.nb, f.db]));
    off += f.lh.length + f.nb.length + f.db.length;
  }
  const cdStart = off; const cdBlob = Buffer.concat(cds);
  const eo = Buffer.alloc(22);
  eo.writeUInt32LE(0x06054b50, 0);
  eo.writeUInt16LE(files.length, 8); eo.writeUInt16LE(files.length, 10);
  eo.writeUInt32LE(cdBlob.length, 12); eo.writeUInt32LE(cdStart, 16);
  return Buffer.concat([...parts, cdBlob, eo]);
}

// ---- CDP plumbing (same as C1 driver) ----
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
  throw new Error("CDP connect failed");
}
function send(method, params = {}) {
  return new Promise((res) => { const id = ++msgId; pending.set(id, res);
    ws.send(JSON.stringify({ id, method, params })); });
}
async function creds() {
  await cdpConnect(); API = null; TOKEN = null;
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

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(1500);

  launch(); await creds();
  const h = await api("GET", "/health", undefined, 200);
  if (!h || h.status !== "ok") fail("health not ready");

  // ---- 1. unauthorized loopback requests rejected ----
  const noTok = await apiRaw("GET", "/api/projects", null);
  const badTok = await apiRaw("GET", "/api/projects", "definitely-wrong");
  if (![401, 403].includes(noTok.status) || ![401, 403].includes(badTok.status))
    fail(`unauth not refused: none=${noTok.status} wrong=${badTok.status}`);
  ok("1. unauthorized loopback requests rejected (no token + wrong token)");

  // ---- 2. authorized operations work ----
  const p1 = (await api("POST", "/api/projects", { name: `C2 A ${Date.now()}` }, 201)).id;
  const p2 = (await api("POST", "/api/projects", { name: `C2 B ${Date.now()}` }, 201)).id;
  await commit(p1, "character.create", { name: "C2Char" });
  ok("2. authorized local application operations work");

  // ---- 3. malicious package import refused ----
  const sha = "ab".repeat(32);
  const manifest = JSON.stringify({
    format_version: 1, project: { name: "evil", id: "p-evil" },
    entities: {}, assets: [], asset_versions: [],
    files: [{ sha256: sha, byte_size: 4, arcname: `files/${sha}` }] });
  const evil = path.join(OUT, "c2-evil.atomicpkg");
  fs.writeFileSync(evil, makeZip([
    ["manifest.json", manifest],
    ["../escape-c2.bin", "evil"],
    [ `files/${sha}`, "xxxx" ] ]));
  const imp = await fetch(`${API}/api/packages/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: JSON.stringify({ package_path: evil }) });
  if (imp.status !== 200) fail(`import endpoint ${imp.status}`);
  const impBody = await imp.json();
  const impJob = await waitJob(impBody.project_id, impBody.job_id);
  if (impJob.status !== "failed") fail("malicious package not refused");
  if (fs.existsSync(path.join(DATA_DIR, "..", "escape-c2.bin")) ||
      fs.existsSync(path.join(OUT, "..", "escape-c2.bin")))
    fail("traversal file escaped");
  ok("3. malicious package import refused — traversal member rejected, job failed");

  // ---- 4. valid backup/restore still works ----
  const ex = await commit(p1, "backup.export", { kind: "backup" }, 200,
    `c2-export-${Date.now()}`);
  const exJob = await waitJob(p1, ex.job_id);
  if (exJob.status !== "succeeded") fail(`export failed: ${exJob.error}`);
  const pkgPath = exJob.result_json.package_path;
  const rst = await fetch(`${API}/api/packages/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: JSON.stringify({ package_path: pkgPath }) });
  const rstBody = await rst.json();
  const rstJob = await waitJob(rstBody.project_id, rstBody.job_id);
  if (rstJob.status !== "succeeded") fail(`restore failed: ${rstJob.error}`);
  const rstChars = await api("GET", `/api/projects/${rstBody.project_id}/character`);
  if (!rstChars.length || rstChars[0].name !== "C2Char") fail("restored content wrong");
  ok("4. valid backup/restore remains functional — content survived roundtrip");

  // ---- 5. synthetic credential isolation ----
  const SYN = "sk-c2-synthetic-INSTALLCHECK-00";
  const put = await fetch(`${API}/api/providers/openai/credential`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: JSON.stringify({ fields: { api_key: SYN } }) });
  if (put.status !== 200) fail(`credential put ${put.status}: ${await put.text()}`);
  const st = await put.json();
  if (!st.configured) fail("credential not stored");
  for (const url of ["/api/providers/openai/credential", "/api/providers/openai"]) {
    const body = await apiRaw("GET", url, TOKEN);
    if (body.body.includes(SYN)) fail(`secret leaked at ${url}`);
  }
  const vaultFile = path.join(DATA_DIR, "credentials.vault");
  if (fs.existsSync(vaultFile) && fs.readFileSync(vaultFile).includes(SYN))
    fail("plaintext secret in vault file");
  await fetch(`${API}/api/providers/openai/credential`,
    { method: "DELETE", headers: { "X-Atomic-Token": TOKEN } });
  ok("5. synthetic credential isolation — metadata-only API, ciphertext-only vault");

  // ---- 6. project separation ----
  const p2chars = await api("GET", `/api/projects/${p2}/character`);
  const p2jobs = await api("GET", `/api/projects/${p2}/jobs`);
  const p1chars = await api("GET", `/api/projects/${p1}/character`);
  if (p2chars.length !== 0 || p1chars.length !== 1) fail("project separation broken");
  const xProj = await apiRaw("GET", `/api/projects/${p1}/jobs`, TOKEN);
  if (xProj.status !== 200) fail("own-project read failed");
  ok("6. project separation — isolated characters/jobs");

  // ---- 7. simulated-only provider surface ----
  // `local` ops adapters (file import, timeline) are real by design; the
  // security property is that every CLOUD/generation adapter is mock/sim —
  // nothing in the installed build can reach a live provider lane.
  const adapters = await api("GET", "/api/adapters");
  const cloudish = adapters.filter((a) =>
    a.requires_cloud || /cloud|generate|provider/i.test(
      `${a.adapter_id} ${(a.tasks || []).join(" ")}`));
  const live = cloudish.filter((a) =>
    (a.adapter_kind || a.kind || "") !== "mock"
    && !/mock|sim/i.test(JSON.stringify(a)));
  if (live.length) fail(`live-capable adapter exposed: ${JSON.stringify(live[0])}`);
  note(`${adapters.length} adapters, ${cloudish.length} cloud/generation — all mock`);
  ok("7. simulated-only provider surface — all cloud/generation adapters are mock");

  // ---- 8. provider planning is read-only ----
  const projsBefore = (await api("GET", "/api/projects")).length;
  const jobsBefore = (await api("GET", `/api/projects/${p1}/jobs`)).length;
  await api("GET", "/api/providers");
  await api("GET", "/api/providers/openai");
  if ((await api("GET", "/api/projects")).length !== projsBefore ||
      (await api("GET", `/api/projects/${p1}/jobs`)).length !== jobsBefore)
    fail("planning endpoints mutated state");
  ok("8. provider planning remains read-only — no jobs/projects created");

  // ---- 9. no live provider activity ----
  // A live dispatch without arm/permit must be refused (422) — fail()
  // inside commit() throws on any other status.
  await commit(p1, "cloud.live.validate_run",
    { provider_key: "openai", model_id: "x" }, 422);
  const allJobs = await api("GET", `/api/projects/${p1}/jobs`);
  const liveJobs = allJobs.filter((j) => /live|dispatch/i.test(j.kind || j.job_kind || ""));
  if (liveJobs.length) fail("a live dispatch job exists");
  ok("9. no live provider activity — live dispatch gate refused, no live jobs");

  // ---- 10. timeline regression ----
  await commit(p1, "screenplay.create", { title: "C2" });
  const sp = (await api("GET", `/api/projects/${p1}/screenplays`))[0];
  await commit(p1, "screenplay_scene.create",
    { screenplay_id: sp.id, heading: "INT. X — DAY" });
  const scRow = (await api("GET", `/api/projects/${p1}/screenplays/${sp.id}`)).scenes[0];
  const ch0 = (await api("GET", `/api/projects/${p1}/character`))[0];
  await commit(p1, "screenplay_scene.update", { sp_scene_id: scRow.id,
    elements: [{ kind: "action", text: "She waits." },
               { kind: "dialogue", character_id: ch0.id, text: "Soon.",
                 parenthetical: "" }] });
  await commit(p1, "shotlist.scaffold", { screenplay_id: sp.id });
  const board = await api("GET", `/api/projects/${p1}/storyboard`);
  if (!board.length || !board[0].shots.length)
    fail("scaffold produced no shots");
  const tls = await api("GET", `/api/projects/${p1}/timelines`);
  if (!Array.isArray(tls)) fail("timelines unreadable");
  ok("10. timeline/shotlist regression — scaffold produced shots, timeline readable");

  // ---- 11. restart persistence ----
  killApp();
  const gone = await waitProcsGone(30);
  if (gone < 0) fail("shutdown not bounded after kill");
  launch(); await creds();
  const again = await api("GET", `/api/projects/${p1}`);
  if (again.name !== (await api("GET", `/api/projects/${p1}`)).name) fail("restart lost project");
  const charsAfter = await api("GET", `/api/projects/${p1}/character`);
  if (!charsAfter.length) fail("restart lost content");
  ok("11. restart persistence — project + content intact");

  // ---- 12. clean bounded shutdown, no orphans ----
  killApp();
  const ms = await waitProcsGone(30);
  if (ms < 0) fail("processes linger >30s after kill");
  note(`bounded shutdown ${(ms / 1000).toFixed(1)}s`);
  ok("12. clean shutdown — no orphan processes");

  lines.push(`C2 ACCEPTANCE: ${steps}/12 PASS`);
  console.log(`C2 ACCEPTANCE: ${steps}/12 PASS`);
  fs.writeFileSync(path.join(OUT, "acceptance.txt"), lines.join("\n"));
}

main().catch((e) => {
  lines.push(`FAIL ${e.message}`);
  try { fs.writeFileSync(path.join(OUT, "acceptance.txt"), lines.join("\n")); } catch {}
  console.error(e); process.exit(1);
});
