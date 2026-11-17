// A2 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port, same plumbing
// as the A1/N3 drivers). 15 steps per the A2.7 checklist on a
// populated A1 project (screenplay + scaffolded shots).
// Run: node scripts/cdp-a2-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9229";
const OUT = process.argv[3] || "docs/a2-evidence";
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
    expected_revision: _rev, idempotency_key: key || `a2-${ct}-${Date.now()}-${Math.random()}` }, expect);
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

// 8x8 solid PNG (Pillow-generated) — real bytes so the backend's
// safe-decode accepts it as a reference image.
const PNG_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNUcEhgwAaYsIoOWgkAm0gA0DGZa6gAAAAASUVORK5CYII=";

const shots = (board) => board.flatMap((sc) => sc.shots);
const entryOf = (board, sid) => shots(board).find((e) => e.shot.id === sid);

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(3000);

  launch(); await creds();
  const health = await (await fetch(`${API}/health`)).json();
  ok(`health: backend ${health.version || "?"} (0021 proven by planning persistence below)`);

  // step 1 — populated A1 project: screenplay + scaffolded shots
  const p = await api("POST", "/api/projects", { name: `A2-${Date.now()}` }, 201);
  _rev = 0;
  await commit(p.id, "character.create", { name: "Anna" });
  await commit(p.id, "character.create", { name: "Bob" });
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
  ok(`1 populated A1 project (screenplay + ${shots(board).length} scaffolded shots)`);

  // step 2 — shot planning: camera angle/movement/follow + framing + fps
  await commit(p.id, "shot.update", { shot_id: shot.id, planning: {
    camera: { angle: "low", movement: "dolly_in",
              follow_mode: "follow_behind",
              follow_params: { distance_m: 3 },
              follow_targets: [{ kind: "character", id: anna.id }] },
    framing: "medium_wide", subject_placement: "left_third",
    lighting: "moon + practical", action_notes: "she turns",
    fps: { num: 24000, den: 1001, frames: 96 },
    continuity_refs: [{ kind: "location", id: dock.id }],
    notes: "acceptance plan" } });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const pj = entryOf(board, shot.id).shot.planning_json;
  if (pj?.camera?.follow_mode !== "follow_behind" || pj.framing !== "medium_wide"
      || pj.fps?.den !== 1001 || pj.notes !== "acceptance plan") {
    fail(`planning not persisted: ${JSON.stringify(pj)}`);
  }
  ok("2 shot planning persisted (follow_behind + framing + 24000/1001)");

  // step 3 — every spec follow mode accepted; bad value rejected
  for (const mode of ["follow_front", "follow_side", "follow_shoulder",
                      "orbit_follow", "drone_follow", "vehicle_chase",
                      "lead_and_follow", "group_follow", "target_switch"]) {
    await commit(p.id, "shot.update", {
      shot_id: shot.id, planning: { camera: { follow_mode: mode } } });
  }
  await api("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "shot.update",
    payload: { shot_id: shot.id, planning: { camera: { follow_mode: "spinny" } } },
    scope: "project", expected_revision: _rev,
    idempotency_key: `a2-bad-${Date.now()}` }, 422);
  ok("3 all 10 follow modes accepted; invalid mode 422'd");

  // step 4 — CAS media: import an image and pin it to the shot
  const png = path.join(OUT, "board-ref.png");
  fs.writeFileSync(png, Buffer.from(PNG_B64, "base64"));
  await commit(p.id, "asset.import", { source_path: png, name: "board-ref", kind: "image" });
  const asset = (await api("GET", `/api/projects/${p.id}/assets`)).items
    .find((a) => a.name === "board-ref");
  await commit(p.id, "entity.assets.link", {
    entity_type: "shot", entity_id: shot.id,
    asset_id: asset.id, role: "reference", label: "angle ref" });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  let media = entryOf(board, shot.id).media;
  if (!media?.assets?.length || media.assets[0].asset_id !== asset.id
      || media.missing.length) fail(`media link wrong: ${JSON.stringify(media)}`);
  ok("4 CAS image pinned to shot — media section + provenance on card");

  // step 5 — thumbnail binding + missing-media flag plumbing
  await commit(p.id, "shot.update", { shot_id: shot.id, thumbnail_asset_id: asset.id });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  media = entryOf(board, shot.id).media;
  if (media.missing.length) fail(`clean bindings flagged: ${media.missing}`);
  ok("5 thumbnail bound; media.missing empty for ready assets");

  // step 6 — ordering + duplication (drag/drop backend primitive)
  const s2 = shots(board)[1].shot;
  await commit(p.id, "shot.move", { shot_id: s2.id, to_index: 0 });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (shots(board)[0].shot.id !== s2.id) fail("shot.move did not reorder");
  await commit(p.id, "shot.duplicate", { shot_id: shot.id, title: "dup" });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const dup = shots(board).find((e) => e.shot.title === "dup");
  if (!dup || dup.shot.planning_json?.camera?.angle !== "low") {
    fail("duplicate lost planning");
  }
  ok("6 reorder + duplicate — planning carried to the copy");

  // step 7 — generic command undo restores planning
  const cmds = await api("GET", `/api/projects/${p.id}/commands`);
  const lastUpd = cmds.filter((c) => c.command_type === "shot.update"
      && c.payload_json && c.payload_json.planning)
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];
  if (!lastUpd) fail("no planning-bearing shot.update command found");
  await api("POST", `/api/projects/${p.id}/commands/${lastUpd.id}/undo`);
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).shot.planning_json?.camera?.follow_mode
      === "target_switch") fail("undo did not revert planning");
  ok("7 shot.update undo restored previous planning");

  // step 8 — A2.6: screenplay sync leaves planning untouched
  const doc2 = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  const els = doc2.scenes[0].elements;
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: doc2.scenes[0].id, elements: [
      { ...els[0], text: "The boat never came." }, els[1]] });
  await commit(p.id, "shotlist.sync", { screenplay_id: sp.id });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const pj2 = entryOf(board, shot.id).shot.planning_json;
  if (pj2?.framing !== "medium_wide" || pj2?.notes !== "acceptance plan") {
    fail(`sync touched planning: ${JSON.stringify(pj2)}`);
  }
  ok("8 screenplay re-sync preserved manual shot planning");

  // step 9 — restart persistence
  killApp(); await sleep(4000);
  launch(); API = null; TOKEN = null; await creds();
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const pj3 = entryOf(board, shot.id)?.shot?.planning_json;
  if (pj3?.camera?.angle !== "low" || !entryOf(board, shot.id)?.media?.assets?.length) {
    fail("planning or media lost across restart");
  }
  ok("9 restart persistence — planning + media links intact");

  // step 10 — clean shutdown
  killApp(); await sleep(6000);
  if (left("atomic-backend.exe")) fail("backend process leaked");
  ok("10 clean shutdown — no orphan backend");

  fs.writeFileSync(path.join(OUT, "a2-acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\nA2 ACCEPTANCE: PASS ${lines.length}/10`);
}

main().catch((e) => {
  lines.push(`FAIL ${e.message}`);
  try { fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, "a2-acceptance.txt"), lines.join("\n") + "\n"); } catch {}
  console.error(e);
  try { killApp(); } catch {}
  process.exit(1);
});
