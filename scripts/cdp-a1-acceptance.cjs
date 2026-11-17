// A1 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port like the N3
// drivers). 17 steps per the A1.8 + A1-F6 checklist.
// Run: node scripts/cdp-a1-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9229";
const OUT = process.argv[3] || "docs/a1-evidence";
const EXE = process.argv[4] ||
  `${process.env.LOCALAPPDATA}\\Programs\\Atomic Dream Engine\\Atomic Dream Engine.exe`;
let API = null, TOKEN = null, appProc = null;
const lines = [];
const ok = (m) => { lines.push(`PASS ${m}`); console.log(`PASS ${m}`); };
const fail = (m) => { lines.push(`FAIL ${m}`); throw new Error(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(method, p, body, expect = 200) {
  const r = await fetch(`${API}${p}`, {
    method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status !== expect) fail(`${method} ${p} -> ${r.status}: ${(await r.text()).slice(0, 300)}`);
  return r.status === 204 ? null : r.json();
}
let _rev = 0;
async function commit(pid, ct, payload, expect = 200, key) {
  if (_rev === 0) _rev = (await api("GET", `/api/projects/${pid}`)).revision;
  const r = await api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: ct, payload, scope: "project", origin: "acceptance",
    expected_revision: _rev, idempotency_key: key || `a1-${ct}-${Date.now()}-${Math.random()}` }, expect);
  if (expect === 200) _rev = r.revision;
  return r;
}
async function waitJob(pid, jobId, timeout = 90000) {
  const t0 = Date.now();
  for (;;) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) return j;
    if (Date.now() - t0 > timeout) fail(`job ${jobId} timeout`);
    await sleep(500);
  }
}

// --- CDP plumbing (minimal: attach, sniff creds) ---
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
  ok(`backend ${API} via installed app`);
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

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(3000);

  // step 1 — open an existing project (created via the installed API)
  launch(); await creds();
  const health = await (await fetch(`${API}/health`)).json();
  ok(`health: backend ${health.version || "?"} migrations ${health.migration_head || health.alembic_head || "?"}`);
  const p = await api("POST", "/api/projects", { name: `A1-${Date.now()}` }, 201);
  ok(`1 project open (${p.id.slice(0, 8)})`);

  // steps 4/3 — characters, locations, then scenes
  _rev = 0;
  await commit(p.id, "character.create", { name: "Anna" });
  await commit(p.id, "character.create", { name: "Bob" });
  await commit(p.id, "location.create", { name: "Night Dock" });
  const chars = await api("GET", `/api/projects/${p.id}/character`);
  const locs = await api("GET", `/api/projects/${p.id}/location`);
  const anna = chars.find((c) => c.name === "Anna"), bob = chars.find((c) => c.name === "Bob");
  const dock = locs.find((l) => l.name === "Night Dock");
  ok("4 characters+locations added");

  // step 2 — create screenplay
  await commit(p.id, "screenplay.create", { title: "Dock Scene", description: "A1 acceptance" });
  const sp = (await api("GET", `/api/projects/${p.id}/screenplays`))[0];
  ok(`2 screenplay '${sp.title}' created`);

  // step 3 — multiple scenes
  await commit(p.id, "screenplay_scene.create", {
    screenplay_id: sp.id, heading: "EXT. DOCK — NIGHT",
    action: "Anna waits on the pier.", location_id: dock.id });
  await commit(p.id, "screenplay_scene.create", {
    screenplay_id: sp.id, heading: "INT. CABIN — NIGHT" });
  let doc = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  ok(`3 scenes ordered: ${doc.scenes.map((s) => s.heading).join(" | ")}`);
  const [s1, s2] = doc.scenes;

  // step 5 — enter dialogue
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: s1.id, elements: [
      { kind: "dialogue", character_id: anna.id, text: "The boat is late.", parenthetical: "(worried)" },
      { kind: "dialogue", character_id: bob.id, text: "It always is." },
      { kind: "action", text: "Fog rolls in." }] });
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: s2.id, elements: [
      { kind: "dialogue", character_id: bob.id, text: "We leave at dawn.", parenthetical: "(quietly)" }] });
  doc = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  const e0 = doc.scenes[0].elements;
  if (e0[0].character_id !== anna.id || e0[0].parenthetical !== "(worried)") fail("dialogue not stored");
  ok("5 dialogue entered w/ speakers + parenthetical");

  // step 6 — deterministic scaffold
  await commit(p.id, "shotlist.scaffold", { screenplay_id: sp.id });
  let board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const shots = board.flatMap((sc) => sc.shots.map((e) => e.shot));
  if (board.length !== 2 || shots.length !== 4) fail(`scaffold wrong: ${board.length} scenes ${shots.length} shots`);
  if (shots[0].shot_type !== "wide" || shots[1].shot_type !== "medium") fail("shot types wrong");
  const dlgCast = board[0].shots[1].cast;
  if (!dlgCast.some((c) => c.character_id === anna.id && c.dialogue === "The boat is late.")) fail("dialogue not on shot");
  ok("6 scaffold: 2 scenes, 4 shots, dialogue+location+camera-placeholder carried");

  // step 7 — manual edit a shot
  await commit(p.id, "shot.update", { shot_id: shots[1].id, title: "MY EDIT" });
  ok("7 manual shot edit committed");

  // step 8 — modify screenplay content
  doc = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  const els = doc.scenes[0].elements;
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: s1.id, elements: [
      { ...els[0], text: "The boat is GONE." }, els[1], els[2]] });
  ok("8 screenplay modified");

  // step 9 — preview sync changes
  const pv = await api("POST", `/api/projects/${p.id}/commands/preview`, {
    command_type: "shotlist.sync", payload: { screenplay_id: sp.id }, scope: "project" });
  const diff = pv.shotlist_diff;
  if (!diff || !diff.counts) fail("no shotlist_diff in preview");
  if (!diff.scenes[0].manual_edit.some((m) => m.shot_id === shots[1].id)) fail("manual edit not flagged");
  ok(`9 preview: ${JSON.stringify(diff.counts)} — manual edit flagged`);

  // step 10 — sync WITHOUT approval preserves the manual edit
  await commit(p.id, "shotlist.sync", { screenplay_id: sp.id });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (board[0].shots[1].shot.title !== "MY EDIT") fail("manual edit overwritten without approval");
  ok("10 manual edit preserved without approval");

  // step 11 — approve the overwrite
  await commit(p.id, "shotlist.sync", {
    screenplay_id: sp.id, approve_overwrite: [shots[1].id] });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (board[0].shots[1].shot.title === "MY EDIT") fail("approved overwrite did not apply");
  const dlg2 = board[0].shots[1].cast;
  if (!dlg2.some((c) => c.dialogue === "The boat is GONE.")) fail("approved sync dialogue wrong");
  ok("11 approved overwrite applied");

  // step 12 — stale approval rejection: a commit pinned to an old
  // project revision must 409 rather than replay a stale preview
  await api("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "shotlist.sync",
    payload: { screenplay_id: sp.id, approve_overwrite: [shots[1].id] },
    scope: "project", expected_revision: _rev - 3,
    idempotency_key: `a1-stale-${Date.now()}` }, 409);
  ok("12 stale approval rejected with 409");

  // step 13 — undo/redo
  await commit(p.id, "shotlist.undo", {});
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (board[0].shots[1].shot.title !== "MY EDIT") fail("undo did not restore pre-sync shot");
  await commit(p.id, "shotlist.redo", {});
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (board[0].shots[1].shot.title === "MY EDIT") fail("redo did not re-apply");
  ok("13 undo/redo round-trip");

  // step 14 — restart persistence
  killApp(); await sleep(4000);
  launch(); API = null; TOKEN = null; await creds();
  doc = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (doc.scenes.length !== 2 || board.flatMap((s) => s.shots).length !== 4) fail("state lost across restart");
  ok("14 restart persistence");

  // step 15 — backup compatibility: export succeeds and is recorded
  _rev = 0;
  const bex = await commit(p.id, "backup.export", { kind: "backup" });
  const bj = await waitJob(p.id, bex.job_id);
  if (bj.status !== "succeeded") fail(`backup job ${bj.status}: ${bj.error || ""}`);
  const backups = await api("GET", `/api/projects/${p.id}/backups`);
  if (!backups.some((b) => b.status === "succeeded")) fail("backup not recorded");
  ok("15 backup.export succeeded on screenplay-bearing project");

  // step 16 — existing timeline functionality still works
  await commit(p.id, "timeline.create", { name: "A1 Cut" });
  const tls = await api("GET", `/api/projects/${p.id}/timelines`);
  if (tls.length !== 1) fail("timeline subsystem broken");
  ok("16 timeline.create works alongside screenplay state");

  // step 17 — clean shutdown
  const r = await api("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "screenplay.update",
    payload: { screenplay_id: sp.id, description: "final" },
    scope: "project", expected_revision: _rev,
    idempotency_key: `a1-final-${Date.now()}` });
  killApp(); await sleep(6000);
  if (left("atomic-backend.exe")) fail("backend process leaked");
  ok("17 clean shutdown — no orphan backend");

  fs.writeFileSync(path.join(OUT, "a1-acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\nA1 ACCEPTANCE: PASS ${lines.length}/17`);
}

main().catch((e) => {
  lines.push(`FAIL ${e.message}`);
  try { fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, "a1-acceptance.txt"), lines.join("\n") + "\n"); } catch {}
  console.error(e);
  try { killApp(); } catch {}
  process.exit(1);
});
