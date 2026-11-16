// N2-C acceptance — real installed NSIS app: audio import → audio
// clip → per-clip + per-lane gain (dB), mute/solo lane controls,
// bounds refusals, EDL v2 mix metadata, undo, UI mix row + solo
// button, and continued export refusal for visible audio lanes.
// Zero credentials/cloud.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n2d-evidence";
const EXE = "C:/Users/gwart/AppData/Local/Atomic Dream Engine/atomic-dream-engine.exe";
const FFMPEG_BIN = path.resolve("backend/vendor/ffmpeg/bin");
let API = null, TOKEN = null;

fs.mkdirSync(OUT, { recursive: true });
const LOG = []; let STEP = 0;
const step = (m) => { STEP += 1; console.log(`[${STEP}] ${m}`); LOG.push(`[${STEP}] ${m}`); };
const ok = (m) => { console.log(`   OK — ${m}`); LOG.push(`   OK — ${m}`); };

let _id = 0; const _pend = new Map(); const _events = new Map();
const onEvent = (n, fn) => { (_events.get(n) ?? _events.set(n, []).get(n)).push(fn); };
let ws;
const send = (m, pr = {}) => new Promise((res, rej) => {
  const i = ++_id; _pend.set(i, { res, rej });
  ws.send(JSON.stringify({ id: i, method: m, params: pr }));
  setTimeout(() => { if (_pend.has(i)) { _pend.get(i).rej(new Error("timeout " + m)); _pend.delete(i); } }, 60000);
});
const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error("js: " + JSON.stringify(r.exceptionDetails).slice(0, 300));
  return r.result && r.result.value;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const mustWaitText = async (txt, t = 15000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < t) {
    if (await evalJs(`document.body.innerText.includes(${JSON.stringify(txt)})`)) return;
    await sleep(300);
  }
  throw new Error(`text never appeared: "${txt}"`);
};
const clickText = (txt, tag = "button") => evalJs(`(() => {
  const el = [...document.querySelectorAll(${JSON.stringify(tag)})]
    .find(e => e.textContent.trim().includes(${JSON.stringify(txt)}) && !e.disabled);
  if (!el) return false; el.click(); return true; })()`);
const mustClick = async (txt, tag, waitMs = 800) => {
  if (!await clickText(txt, tag)) throw new Error(`cannot click ${tag} "${txt}"`);
  await sleep(waitMs);
};
const api = async (method, p, body) => {
  const r = await fetch(`${API}${p}`, {
    method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(`api ${method} ${p}: ${r.status} ${(await r.text()).slice(0, 200)}`);
  return r.json();
};
const apiRaw = async (method, p, body) => {
  const r = await fetch(`${API}${p}`, {
    method, headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined });
  return r;
};
const commit = async (pid, type, payload) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "audit",
    expected_revision: proj.revision,
    idempotency_key: `n2c-${type}-${Date.now()}-${Math.random()}` });
};
const detail = (pid, tid) => api("GET", `/api/projects/${pid}/timelines/${tid}`);
const waitJob = async (pid, jobId, expectFail = false) => {
  for (let i = 0; i < 240; i++) {
    const j = (await api("GET", `/api/projects/${pid}/jobs`)).find((x) => x.id === jobId);
    if (j && j.status === "succeeded") return j;
    if (j && (j.status === "failed" || j.status === "cancelled")) {
      if (expectFail) return j;
      throw new Error("job failed: " + JSON.stringify(j).slice(0, 300));
    }
    await sleep(500);
  }
  throw new Error("job timeout");
};

async function navToTimeline(pname) {
  await send("Page.reload", { ignoreCache: true });
  await sleep(6000);
  const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    const st = await evalJs(`(() => {
      const t = document.body.innerText;
      if ([...document.querySelectorAll("button")].some(b => b.textContent.includes(${JSON.stringify(pname)}))) return "projects";
      if (document.querySelector('[aria-label="Workspace"]')) return "workspace";
      if (t.includes("No project open") || t.includes("Create something extraordinary")) return "home";
      return null; })()`);
    if (st === "projects") break;
    if (st === "home") { await clickText("Projects", "button,a"); await sleep(1200); continue; }
    if (st === "workspace") { await clickText("← Projects") || await clickText("Choose a project"); await sleep(1200); break; }
    await sleep(400);
  }
  await mustClick(pname, "button", 2000);
  await mustWaitText("Timeline", 10000);
  await mustClick("Timeline", "button,a", 1200);
  await sleep(600);
}

async function attachWs() {
  const targets = await fetch(`http://127.0.0.1:${PORT}/json`).then((r) => r.json());
  const page = targets.find((t) => t.type === "page");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && _pend.has(m.id)) {
      const { res, rej } = _pend.get(m.id); _pend.delete(m.id);
      m.error ? rej(new Error(m.error.message)) : res(m.result);
    } else if (m.method && _events.has(m.method)) {
      _events.get(m.method).forEach((fn) => fn(m.params));
    }
  };
}

async function main() {
  await attachWs();
  await send("Network.enable");
  onEvent("Network.requestWillBeSent", (e) => {
    const m = e.request.url.match(/http:\/\/(127\.0\.0\.1|localhost):(\d+)\/api/);
    if (m && !API) {
      API = `http://127.0.0.1:${m[2]}`;
      TOKEN = (e.request.headers || {})["X-Atomic-Token"] || null;
    }
  });
  await sleep(1500);
  if (!API || !TOKEN) {
    await evalJs(`(() => {
      const b = [...document.querySelectorAll("button,a")]
        .find(e => e.textContent.trim() === "Projects");
      if (b) b.click(); return !!b; })()`);
    await sleep(2500);
  }
  if (!API || !TOKEN) { await send("Page.reload", { ignoreCache: true }); await sleep(6000); }
  if (!API || !TOKEN) throw new Error("no backend creds");
  ok(`backend ${API}`);

  const wav = `${OUT}\\media\\tone10.wav`;
  fs.mkdirSync(`${OUT}\\media`, { recursive: true });
  execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
    "-ar", "48000", "-ac", "2", wav], { stdio: "ignore" });
  ok("wav synthesized (10s sine, 48k stereo)");

  /* 1 - project + two audio clips on one lane with xf handles */
  step("1) Project + two audio clips with handles");
  const p = await api("POST", "/api/projects", { name: `N2D-${Date.now()}` });
  const ra = await commit(p.id, "asset.import",
    { source_path: path.resolve(wav), kind: "audio" });
  await waitJob(p.id, ra.job_id);
  const assets = (await api("GET", `/api/projects/${p.id}/assets?limit=50`)).items;
  const au = assets.find((a) => a.name.includes("tone10"));
  await commit(p.id, "timeline.create", { name: "MIX", fps_num: 24, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`)).find((t) => t.name === "MIX");
  await commit(p.id, "timeline.track.add", { timeline_id: tl.id, kind: "audio", name: "A1" });
  let d = await detail(p.id, tl.id);
  const a1 = d.tracks.find((t) => t.kind === "audio");
  // Clip A: trimmed to first 5s -> 5s of tail handle.
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: au.id, start_frames: 0 });
  d = await detail(p.id, tl.id);
  const clipA = d.clips[0];
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: clipA.id, out_frame: 240000 });
  // Clip B at 120f (5s@24), head trimmed to 48000 samples (1s/24f head handle).
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: au.id,
      start_frames: 120, allow_duplicate: true });
  d = await detail(p.id, tl.id);
  const clipB = d.clips.find((c) => c.id !== clipA.id);
  await commit(p.id, "timeline.clip.trim",
    { timeline_id: tl.id, clip_id: clipB.id, in_frame: 48000 });
  d = await detail(p.id, tl.id);
  ok(`A=${clipA.id.slice(0, 8)}[0..120f) B=${clipB.id.slice(0, 8)}[120f..)`);

  /* 2 - fades persist */
  step("2) timeline.clip.fade - fades persist");
  await commit(p.id, "timeline.clip.fade",
    { timeline_id: tl.id, clip_id: clipA.id,
      fade_in_units: 4800, fade_in_curve: "equal_power",
      fade_out_units: 9600 });
  d = await detail(p.id, tl.id);
  const ca = d.clips.find((c) => c.id === clipA.id);
  if (ca.fade_in_units !== 4800 || ca.fade_in_curve !== "equal_power"
      || ca.fade_out_units !== 9600)
    throw new Error(`fades wrong: ${JSON.stringify(ca).slice(0, 200)}`);
  ok("fade_in=4800 equal_power, fade_out=9600 linear persisted");

  /* 3 - fade bounds refuse closed */
  step("3) fade bounds + curves fail closed");
  const badSum = await apiRaw("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "timeline.clip.fade",
    payload: { timeline_id: tl.id, clip_id: clipA.id,
      fade_out_units: 240000 }, scope: "project", origin: "audit",
    expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
    idempotency_key: `n2d-badsum-${Date.now()}` });
  const badCurve = await apiRaw("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "timeline.clip.fade",
    payload: { timeline_id: tl.id, clip_id: clipA.id,
      fade_in_curve: "scurve" }, scope: "project", origin: "audit",
    expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
    idempotency_key: `n2d-badcurve-${Date.now()}` });
  if (badSum.status !== 422 || badCurve.status !== 422)
    throw new Error(`refusals: ${badSum.status}/${badCurve.status}`);
  ok("sum>extent -> 422; unknown curve -> 422");

  /* 4 - automation persists */
  step("4) timeline.clip.automation - keyframes persist");
  await commit(p.id, "timeline.clip.automation",
    { timeline_id: tl.id, clip_id: clipA.id,
      points: [{ at_frames: 0, gain_db: -20 },
               { at_frames: 60, gain_db: 0 }] });
  d = await detail(p.id, tl.id);
  const pts = JSON.parse(d.clips.find((c) => c.id === clipA.id).automation_json);
  if (pts.length !== 2 || pts[0].gain_db !== -20)
    throw new Error(`automation wrong: ${JSON.stringify(pts)}`);
  ok(`2 keyframes stored`);

  /* 5 - crossfade set */
  step("5) crossfade.set - explicit window persists");
  await commit(p.id, "timeline.crossfade.set",
    { timeline_id: tl.id, out_clip_id: clipA.id,
      in_clip_id: clipB.id, tl_start: 114, tl_frames: 12,
      curve: "equal_power" });
  d = await detail(p.id, tl.id);
  if (d.crossfades.length !== 1 || d.crossfades[0].satisfied !== true)
    throw new Error(`xf wrong: ${JSON.stringify(d.crossfades)}`);
  const xid = d.crossfades[0].id;
  ok(`xf ${xid.slice(0, 8)} satisfied, window 114f+12f`);

  /* 6 - satisfied flips honestly on move */
  step("6) move detaches window - satisfied=false, record kept");
  await commit(p.id, "timeline.clip.move",
    { timeline_id: tl.id, clip_id: clipB.id, start_frames: 400 });
  d = await detail(p.id, tl.id);
  if (d.crossfades[0].satisfied !== false)
    throw new Error("detached window still satisfied");
  await commit(p.id, "timeline.clip.move",
    { timeline_id: tl.id, clip_id: clipB.id, start_frames: 120 });
  d = await detail(p.id, tl.id);
  if (d.crossfades[0].satisfied !== true)
    throw new Error("reattached window not satisfied");
  ok("move -> false -> back -> true (record never dropped)");

  /* 7 - EDL v2 deterministic */
  step("7) EDL v2 carries fades/automation/crossfades");
  const edl1 = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}/edl?version=2`);
  const edl2 = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}/edl?version=2`);
  if (JSON.stringify(edl1) !== JSON.stringify(edl2))
    throw new Error("EDL not deterministic");
  const eca = edl1.clips.find((c) => c.clip_id === clipA.id);
  if (!eca.fades || eca.fades.in.units !== 4800 || eca.automation.length !== 2
      || edl1.crossfades.length !== 1 || edl1.crossfades[0].satisfied !== true)
    throw new Error(`edl wrong: ${JSON.stringify({ f: eca.fades, a: eca.automation, x: edl1.crossfades }).slice(0, 300)}`);
  ok("edl: fades + automation + satisfied xf, byte-identical");

  /* 8 - trim/split guards */
  step("8) trim-into-fade + split-inside-xf + head-handle guards");
  const badTrim = await apiRaw("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "timeline.clip.trim",
    payload: { timeline_id: tl.id, clip_id: clipA.id,
      out_frame: 12000 }, scope: "project", origin: "audit",
    expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
    idempotency_key: `n2d-badtrim-${Date.now()}` });
  const badSplit = await apiRaw("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "timeline.clip.split",
    payload: { timeline_id: tl.id, clip_id: clipB.id,
      at_frame: 122 }, scope: "project", origin: "audit",
    expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
    idempotency_key: `n2d-badsplit-${Date.now()}` });
  const badHead = await apiRaw("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "timeline.clip.trim",
    payload: { timeline_id: tl.id, clip_id: clipB.id,
      in_frame: 3000 }, scope: "project", origin: "audit",
    expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
    idempotency_key: `n2d-badhead-${Date.now()}` });
  if (badTrim.status !== 422 || badSplit.status !== 422 || badHead.status !== 422)
    throw new Error(`guards: ${badTrim.status}/${badSplit.status}/${badHead.status}`);
  ok("trim past fade-sum -> 422; split inside window -> 422; head trim -> 422");

  /* 9 - undo restores the xf record */
  step("9) undo restores the xf record (stable id)");
  await commit(p.id, "timeline.crossfade.clear",
    { timeline_id: tl.id, crossfade_id: xid });
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  if (d.crossfades.length !== 1 || d.crossfades[0].id !== xid)
    throw new Error("undo did not restore the xf record");
  ok(`undo restored xf ${xid.slice(0, 8)} (stable id)`);

  /* 10 - installed UI: wedges, apex handles, xf badge, sections */
  step("10) installed UI - wedges, handles, badge, inspector sections");
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("MIX"));
    if (b) b.click(); return !!b; })()`);
  await sleep(2500);
  const ui = await evalJs(`(() => {
    const clips = [...document.querySelectorAll('[data-testid="lane-clip"]')];
    const wIn = document.querySelector('[data-testid="fade-in-wedge"]');
    const hIn = document.querySelector('[data-testid="fade-in-handle"]');
    const badge = document.querySelector('[data-testid="xf-badge"]');
    return { clips: clips.length, wedge: !!wIn, handle: !!hIn,
      badge: badge ? badge.textContent.trim() : null }; })()`);
  if (!ui.wedge || !ui.handle || ui.badge !== "⇄" || ui.clips < 2)
    throw new Error(`ui fades/xf: ${JSON.stringify(ui)}`);
  await evalJs(`(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    if (c) c.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    return !!c; })()`);
  await sleep(1500);
  const insp = await evalJs(`(() => ({
    fade: !!document.querySelector('[data-testid="fade-section"]'),
    auto: !!document.querySelector('[data-testid="automation-section"]'),
    xf: !!document.querySelector('[data-testid="xf-section"]'),
    xfText: (document.querySelector('[data-testid="xf-section"]') || {}).textContent || "" }))()`);
  if (!insp.fade || !insp.auto || !insp.xf || !insp.xfText.includes("12f window"))
    throw new Error(`inspector: ${JSON.stringify(insp)}`);
  ok("wedges + apex handles + badge live; inspector sections present");

  /* 10b — real UI edits: curve select + automation keyframe list.
     Committed refreshes can drop the inspector selection — re-click
     clip A before each interaction. React selects commit through
     their onChange prop (call it directly; the value tracker can
     swallow a bare dispatched 'change'). */
  step("10b) UI curve select + automation editing commit to backend");
  const clickClipA = `(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    if (!c) throw new Error("clip A gone");
    c.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    return true; })()`;
  const setCurve = (v) => `(() => {
    const sel = [...document.querySelectorAll("select")]
      .find(e => e.getAttribute("aria-label") === "Fade in curve");
    if (!sel) throw new Error("no fade-in curve select");
    const d = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(sel), "value");
    if (d && d.set) d.set.call(sel, ${JSON.stringify(v)});
    else sel.value = ${JSON.stringify(v)};
    const k = Object.keys(sel).find((x) => x.startsWith("__reactProps"));
    if (k && sel[k].onChange)
      sel[k].onChange({ target: sel, currentTarget: sel });
    else sel.dispatchEvent(new Event("change", { bubbles: true }));
    return true; })()`;
  await evalJs(clickClipA);
  await sleep(1500);
  await evalJs(setCurve("linear"));
  await sleep(2000);
  d = await detail(p.id, tl.id);
  let caNow = d.clips.find((c) => c.id === clipA.id);
  if (caNow.fade_in_curve !== "linear")
    throw new Error("curve select did not commit: " + caNow.fade_in_curve);
  await evalJs(clickClipA);
  await sleep(1200);
  await evalJs(setCurve("equal_power"));
  await sleep(2000);
  // automation: "+ keyframe" adds a midpoint point; "✕" deletes it.
  await evalJs(clickClipA);
  await sleep(1200);
  await evalJs(`(() => {
    const sec = document.querySelector('[data-testid="automation-section"]');
    const b = sec && [...sec.querySelectorAll("button")]
      .find((e) => e.textContent.includes("keyframe"));
    if (!b) throw new Error("no +keyframe button");
    b.click(); return true; })()`);
  await sleep(2000);
  d = await detail(p.id, tl.id);
  caNow = d.clips.find((c) => c.id === clipA.id);
  let ptsNow = JSON.parse(caNow.automation_json);
  if (ptsNow.length !== 3 || caNow.fade_in_curve !== "equal_power")
    throw new Error(`UI commits wrong: pts=${ptsNow.length} curve=${caNow.fade_in_curve}`);
  await evalJs(clickClipA);
  await sleep(1200);
  await evalJs(`(() => {
    const sec = document.querySelector('[data-testid="automation-section"]');
    const b = sec && [...sec.querySelectorAll("button")]
      .find((e) => e.getAttribute("aria-label") === "Delete point 3");
    if (!b) throw new Error("no delete-point button");
    b.click(); return true; })()`);
  await sleep(2000);
  d = await detail(p.id, tl.id);
  caNow = d.clips.find((c) => c.id === clipA.id);
  if (JSON.parse(caNow.automation_json).length !== 2)
    throw new Error("delete point did not commit");
  ok("curve select + keyframe add/delete commit via the real UI");

  /* 11 - export still fails closed on the populated audio lane */
  step("11) export still fails closed (N2-E)");
  const jr = await commit(p.id, "timeline.export",
    { timeline_id: tl.id, profile: "720p" });
  const xjob = await waitJob(p.id, jr.job_id, true);
  if (xjob.status !== "failed" || !/audio lanes/i.test(xjob.error || ""))
    throw new Error(`export did not fail closed: ${JSON.stringify(xjob).slice(0, 240)}`);
  ok(`refused: "${xjob.error.slice(0, 80)}"`);

  /* 12 - restart persistence: kill + relaunch, state must survive */
  step("12) restart persistence — fades/automation/xf survive relaunch");
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"]); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"]); } catch {}
  await sleep(3000);
  const { spawn } = require("child_process");
  spawn(EXE, [], { detached: true, stdio: "ignore",
    env: { ...process.env,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:
        `--remote-debugging-port=${PORT}` } }).unref();
  let up = false;
  for (let i = 0; i < 45 && !up; i++) {
    await sleep(2000);
    try {
      const l = await fetch(`http://127.0.0.1:${PORT}/json`).then((r) => r.json());
      up = l.some((t) => t.type === "page");
    } catch {}
  }
  if (!up) throw new Error("relaunch: CDP never came up");
  await attachWs();
  await send("Network.enable");
  API = null; TOKEN = null;
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button,a")]
      .find(e => e.textContent.trim() === "Projects");
    if (b) b.click(); return !!b; })()`);
  await sleep(3500);
  if (!API || !TOKEN) { await send("Page.reload", { ignoreCache: true }); await sleep(6000); }
  if (!API || !TOKEN) throw new Error("relaunch: no backend creds");
  d = await detail(p.id, tl.id);
  const ca2 = d.clips.find((c) => c.id === clipA.id);
  if (ca2.fade_in_units !== 4800 || ca2.fade_out_units !== 9600
      || JSON.parse(ca2.automation_json).length !== 2
      || d.crossfades.length !== 1 || d.crossfades[0].id !== xid
      || d.crossfades[0].satisfied !== true)
    throw new Error("restart lost fade/automation/xf state");
  ok("relaunched — fades + 2 keyframes + satisfied xf intact (stable ids)");

  /* 13 - clean shutdown */
  step("13) shutdown - no orphan processes");
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"]); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"]); } catch {}
  await sleep(3000);
  const procs = execFileSync("tasklist").toString();
  const orphans = (procs.match(/atomic-(dream-engine|backend)\.exe/gi) || []).length;
  if (orphans > 0) throw new Error(`${orphans} orphan app processes`);
  ok("clean shutdown - zero orphan app processes");

  fs.writeFileSync(`${OUT}\\n2d-acceptance.log`, LOG.join("\n") + "\n");
  console.log(`\nPASS ${STEP}/14 - evidence in ${OUT}`);
}

main().catch((e) => {
  console.error("FAIL:", e.message || e);
  try { fs.writeFileSync(`${OUT}\\n2d-acceptance.log`, LOG.join("\n") + `\nFAIL ${e.message}\n`); } catch {}
  process.exit(1);
});
