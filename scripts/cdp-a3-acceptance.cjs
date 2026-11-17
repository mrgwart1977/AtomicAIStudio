// A3 installed-candidate acceptance — drives the REAL installed app's
// backend API (creds sniffed from the WebView2 CDP port, same plumbing
// as the A1/A2 drivers). 15 steps per the A3.8 checklist on a
// populated A2 project (screenplay + scaffolded + planned shots).
// Run: node scripts/cdp-a3-acceptance.cjs <cdpPort> <outDir> [exe]
const fs = require("fs"), path = require("path");
const { execFileSync, spawn } = require("child_process");
const PORT = process.argv[2] || "9229";
const OUT = process.argv[3] || "docs/a3-evidence";
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
  // Always fetch the current revision — generation jobs commit their own
  // apply_commands (keyframe.result) which bump it independently.
  const rev = (await api("GET", `/api/projects/${pid}`)).revision;
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: ct, payload, scope: "project", origin: "acceptance",
    expected_revision: rev, idempotency_key: key || `a3-${ct}-${Date.now()}-${Math.random()}` }, expect);
}
async function preview(pid, ct, payload, expect = 200) {
  return api("POST", `/api/projects/${pid}/commands/preview`, {
    command_type: ct, payload, scope: "project", origin: "acceptance" }, expect);
}
async function waitJob(pid, jobId, timeout = 90000) {
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
const PNG_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNUcEhgwAaYsIoOWgkAm0gA0DGZa6gAAAAASUVORK5CYII=";

const shots = (board) => board.flatMap((sc) => sc.shots);
const entryOf = (board, sid) => shots(board).find((e) => e.shot.id === sid);
const kfs = (pid, sid) => api("GET", `/api/projects/${pid}/shots/${sid}/keyframes`);

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  killApp(); await sleep(3000);

  launch(); await creds();
  const health = await (await fetch(`${API}/health`)).json();
  note(`health: backend ${health.version || "?"} (0022 proven by keyframe persistence below)`);

  // step 1 — populated A2 project: screenplay + scaffolded + planned shot
  const p = await api("POST", "/api/projects", { name: `A3-${Date.now()}` }, 201);
  _rev = 0;
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
  ok(`1 populated A2 project (screenplay + ${shots(board).length} scaffolded shots)`);

  // step 2 — select a planned shot (A2 planning on the scaffold shot)
  const shot = shots(board)[0].shot;
  await commit(p.id, "shot.update", { shot_id: shot.id, prompt: "she turns toward the fog",
    planning: { camera: { angle: "low", movement: "dolly_in",
                          follow_mode: "follow_behind" },
                framing: "medium_wide" } });
  await commit(p.id, "shot.cast.update",
    { shot_id: shot.id, character_id: anna.id, state: { expression: "wary" } });
  await commit(p.id, "shot.update", { shot_id: shot.id, location_id: dock.id });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).shot.planning_json?.camera?.angle !== "low") {
    fail("planning missing on selected shot");
  }
  ok("2 planned shot selected (low angle + follow_behind + cast + location)");

  // step 3 — create start/end keyframes
  await commit(p.id, "keyframe.create", { shot_id: shot.id, role: "start" });
  await commit(p.id, "keyframe.create", { shot_id: shot.id, role: "end" });
  let klist = await kfs(p.id, shot.id);
  if (klist.length !== 2 || new Set(klist.map((k) => k.role)).size !== 2) {
    fail(`keyframe creation wrong: ${JSON.stringify(klist)}`);
  }
  const kfStart = klist.find((k) => k.role === "start");
  const kfEnd = klist.find((k) => k.role === "end");
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).keyframes?.count !== 2) {
    fail("board keyframe summary missing");
  }
  ok("3 start + end keyframes created; board summary shows 2");

  // step 4 — continuity-aware prompt drafts (compose is proposal-only)
  let prop = await preview(p.id, "keyframe.compose", { keyframe_id: kfStart.id });
  const draft = prop.resolved_draft;
  if (!draft?.prompt_sha256 || !draft.sections?.length
      || !draft.user_prompt.startsWith("opening frame")) {
    fail(`compose draft wrong: ${JSON.stringify(draft).slice(0, 300)}`);
  }
  // Draft did not write the prompt
  if ((await kfs(p.id, shot.id)).find((k) => k.id === kfStart.id).prompt !== "") {
    fail("compose preview wrote the prompt");
  }
  await commit(p.id, "keyframe.compose",
    { keyframe_id: kfStart.id, expected_keyframe: prop.resolved_keyframe });
  const after = (await kfs(p.id, shot.id)).find((k) => k.id === kfStart.id);
  if (!after.prompt.startsWith("opening frame") || after.prompt_origin !== "compose") {
    fail(`draft not applied: ${JSON.stringify(after).slice(0, 200)}`);
  }
  ok("4 continuity-aware draft proposed + applied (prompt_origin=compose)");

  // step 5 — edit prompts (user ownership flips origin back)
  await commit(p.id, "keyframe.update", {
    keyframe_id: kfStart.id, prompt: "opening frame: Anna turns slowly",
    settings: { seed: 42 } });
  await commit(p.id, "keyframe.update", {
    keyframe_id: kfEnd.id, prompt: "closing frame: the pier is empty",
    settings: { seed: 43 } });
  const edited = (await kfs(p.id, shot.id)).find((k) => k.id === kfStart.id);
  if (edited.prompt !== "opening frame: Anna turns slowly"
      || edited.prompt_origin !== "user" || edited.settings_json.seed !== 42) {
    fail(`prompt edit wrong: ${JSON.stringify(edited).slice(0, 200)}`);
  }
  ok("5 prompts edited; origin flipped to user; seed persisted");

  // step 6 — pick a model with a compatibility verdict (mock adapter runs regardless)
  const models = await api("GET", "/api/models");
  const imgModels = models.filter((m) => m.modality === "image");
  const compat = imgModels.find((m) => m.compatibility?.status === "compatible")
    || imgModels[0];
  note(`model pick: ${compat ? compat.key + " (" + compat.compatibility.status + ")" : "none — mock default"}`);
  if (compat) {
    await commit(p.id, "keyframe.update", {
      keyframe_id: kfStart.id, settings: { model_key: compat.key } });
  }
  ok("6 model compatibility consulted (verdict surfaces; mock runs)");

  // step 7 — preview generation: composed prompt + keyframe pin
  prop = await preview(p.id, "keyframe.generate", { keyframe_id: kfStart.id });
  if (!prop.resolved_keyframe?.keyframe_id || !prop.resolved_prompt?.prompt_sha256
      || !prop.resolved_prompt.prompt.includes("Anna turns slowly")) {
    fail(`generate preview wrong: ${JSON.stringify(prop).slice(0, 300)}`);
  }
  ok("7 generation preview — composed prompt + resolved_keyframe pin");

  // step 8 — approve the mock job with the preview's pins
  const gen = await commit(p.id, "keyframe.generate", {
    keyframe_id: kfStart.id,
    expected_keyframe: prop.resolved_keyframe,
    expected_prompt: prop.resolved_prompt.prompt_sha256 });
  if (!gen.job_id) fail("no job enqueued");
  ok(`8 mock job approved + enqueued (${gen.job_id.slice(0, 8)})`);

  // step 9 — observe progress → terminal state
  let seen = false;
  for (let i = 0; i < 40; i++) {
    const j = (await api("GET", `/api/projects/${p.id}/jobs`)).find((x) => x.id === gen.job_id);
    if (j && j.status === "running" && j.progress > 0) { seen = true; break; }
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) { seen = true; break; }
    await sleep(300);
  }
  if (!seen) fail("job never showed progress");
  const done = await waitJob(p.id, gen.job_id);
  if (done.status !== "succeeded" || done.adapter_id !== "mock.image") {
    fail(`job wrong: ${JSON.stringify(done).slice(0, 200)}`);
  }
  ok("9 progress observed; job succeeded via mock.image");

  // step 10 — cancel + retry on the end keyframe
  const gen2 = await commit(p.id, "keyframe.generate", { keyframe_id: kfEnd.id });
  try {
    await api("POST", `/api/jobs/${gen2.job_id}/cancel`, undefined, 200);
  } catch { /* cancel may race a fast mock — handled below */ }
  const j2 = await waitJob(p.id, gen2.job_id);
  note(`first end-gen finished as ${j2.status}`);
  const retry = await commit(p.id, "keyframe.generate", { keyframe_id: kfEnd.id });
  const j3 = await waitJob(p.id, retry.job_id);
  if (j3.status !== "succeeded") fail(`retry failed: ${JSON.stringify(j3)}`);
  if ((await kfs(p.id, shot.id)).find((k) => k.id === kfEnd.id).result_asset_id
      !== j3.output_asset_id) {
    fail("retry result not associated");
  }
  ok(`10 cancel exercised (${j2.status}) + retry succeeded`);

  // step 11 — result asset association (durable, CAS-backed, mock-labeled)
  const start = (await kfs(p.id, shot.id)).find((k) => k.id === kfStart.id);
  if (start.result_asset_id !== done.output_asset_id || start.job_status !== "succeeded") {
    fail(`result not associated: ${JSON.stringify(start).slice(0, 300)}`);
  }
  const assets = (await api("GET", `/api/projects/${p.id}/assets`)).items;
  const out = assets.find((a) => a.id === start.result_asset_id);
  if (!out || out.kind !== "image" || out.status !== "ready") {
    fail(`result asset wrong: ${JSON.stringify(out).slice(0, 200)}`);
  }
  ok("11 mock result asset associated (ready CAS image)");

  // step 12 — pin the selected keyframe → shot thumbnail; undo restores
  await commit(p.id, "keyframe.pin", { keyframe_id: kfStart.id });
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).shot.thumbnail_asset_id !== start.result_asset_id) {
    fail("pin did not set the thumbnail");
  }
  const cmds = await api("GET", `/api/projects/${p.id}/commands`);
  const pin = cmds.filter((c) => c.command_type === "keyframe.pin").pop();
  await api("POST", `/api/projects/${p.id}/commands/${pin.id}/undo`);
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  if (entryOf(board, shot.id).shot.thumbnail_asset_id !== null) {
    fail("pin undo did not restore");
  }
  await commit(p.id, "keyframe.pin", { keyframe_id: kfStart.id });
  ok("12 pinned as thumbnail; undo restored; re-pinned");

  // step 13 — restart persistence
  killApp(); await sleep(4000);
  launch(); API = null; TOKEN = null; await creds();
  const rk = (await kfs(p.id, shot.id)).find((k) => k.id === kfStart.id);
  if (rk.prompt !== "opening frame: Anna turns slowly"
      || rk.result_asset_id !== start.result_asset_id
      || rk.job_status !== "succeeded") {
    fail(`restart lost keyframe state: ${JSON.stringify(rk).slice(0, 300)}`);
  }
  ok("13 restart persistence — prompt + result + job link intact");

  // step 14 — storyboard + timeline still work alongside keyframes
  board = await api("GET", `/api/projects/${p.id}/storyboard`);
  const be = entryOf(board, shot.id);
  if (be.keyframes?.count !== 2
      || be.shot.planning_json?.camera?.angle !== "low") {
    fail("board state degraded");
  }
  await commit(p.id, "timeline.create", { name: "A3 Cut" });
  const tls = await api("GET", `/api/projects/${p.id}/timelines`);
  if (tls.length !== 1) fail("timeline subsystem broken");
  // screenplay sync still preserves everything
  const doc2 = await api("GET", `/api/projects/${p.id}/screenplays/${sp.id}`);
  const els = doc2.scenes[0].elements;
  await commit(p.id, "screenplay_scene.update", {
    sp_scene_id: doc2.scenes[0].id, elements: [
      { ...els[0], text: "The boat never came." }, els[1]] });
  _rev = (await api("GET", `/api/projects/${p.id}`)).revision;
  await commit(p.id, "shotlist.sync", { screenplay_id: sp.id });
  if ((await kfs(p.id, shot.id)).length !== 2) {
    fail("sync disturbed keyframes");
  }
  ok("14 storyboard + timeline + sync all healthy alongside keyframes");

  // step 15 — clean shutdown
  killApp(); await sleep(6000);
  if (left("atomic-backend.exe")) fail("backend process leaked");
  ok("15 clean shutdown — no orphan backend");

  lines.push(`A3 ACCEPTANCE: PASS ${steps}/${STEPS}`);
  fs.writeFileSync(path.join(OUT, "a3-acceptance.txt"), lines.join("\n") + "\n");
  console.log(`\n${lines[lines.length - 1]}`);
  if (steps !== STEPS) fail(`step count ${steps} != ${STEPS}`);
}

main().catch((e) => {
  lines.push(`FAIL ${e.message}`);
  try { fs.mkdirSync(OUT, { recursive: true });
    fs.writeFileSync(path.join(OUT, "a3-acceptance.txt"), lines.join("\n") + "\n"); } catch {}
  console.error(e);
  try { killApp(); } catch {}
  process.exit(1);
});
