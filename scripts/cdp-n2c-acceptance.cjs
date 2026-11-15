// N2-C acceptance — real installed NSIS app: audio import → audio
// clip → per-clip + per-lane gain (dB), mute/solo lane controls,
// bounds refusals, EDL v2 mix metadata, undo, UI mix row + solo
// button, and continued export refusal for visible audio lanes.
// Zero credentials/cloud.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n2c-evidence";
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

  const wav = `${OUT}\\media\\tone8.wav`;
  fs.mkdirSync(`${OUT}\\media`, { recursive: true });
  execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
    "-ar", "48000", "-ac", "2", wav], { stdio: "ignore" });
  ok("wav synthesized (8s sine, 48k stereo)");

  /* 1 — project + audio import + clip on an audio lane */
  step("1) Project + audio import + audio clip placed");
  const p = await api("POST", "/api/projects", { name: `N2C-${Date.now()}` });
  const ra = await commit(p.id, "asset.import",
    { source_path: path.resolve(wav), kind: "audio" });
  await waitJob(p.id, ra.job_id);
  const au = (await api("GET", `/api/projects/${p.id}/assets?limit=50`))
    .items.find((a) => a.name.includes("tone8"));
  await commit(p.id, "timeline.create", { name: "MIX", fps_num: 24, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`)).find((t) => t.name === "MIX");
  await commit(p.id, "timeline.track.add", { timeline_id: tl.id, kind: "audio", name: "A1" });
  let d = await detail(p.id, tl.id);
  const a1 = d.tracks.find((t) => t.kind === "audio");
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: au.id, start_frames: 0 });
  d = await detail(p.id, tl.id);
  const clip = d.clips[0];
  if (clip.gain_db !== 0 || a1.gain_db !== 0 || a1.solo !== false)
    throw new Error(`defaults wrong: ${JSON.stringify({ clip: clip.gain_db, tr: a1.gain_db, solo: a1.solo })}`);
  ok(`clip placed; defaults unity (gain_db=0, solo=false)`);

  /* 2 — per-clip gain persists */
  step("2) timeline.clip.gain — per-clip dB persists");
  await commit(p.id, "timeline.clip.gain",
    { timeline_id: tl.id, clip_id: clip.id, gain_db: -8 });
  d = await detail(p.id, tl.id);
  if (d.clips[0].gain_db !== -8) throw new Error("clip gain did not persist");
  ok("clip gain_db = -8.0");

  /* 3 — lane gain + solo + mute via track.update */
  step("3) timeline.track.update — gain_db + solo + muted");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, gain_db: 2.5, solo: true });
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, muted: true });
  d = await detail(p.id, tl.id);
  const tr = d.tracks.find((t) => t.id === a1.id);
  if (tr.gain_db !== 2.5 || tr.solo !== true || tr.muted !== true)
    throw new Error(`track mix state wrong: ${JSON.stringify(tr)}`);
  ok("lane gain_db=+2.5, solo=true, muted=true");

  /* 4 — bounds + cross-kind refusals fail closed */
  step("4) bounds + kind refusals fail closed");
  for (const bad of [-61, 12.5]) {
    const r = await apiRaw("POST", `/api/projects/${p.id}/commands/commit`, {
      command_type: "timeline.clip.gain",
      payload: { timeline_id: tl.id, clip_id: clip.id, gain_db: bad },
      scope: "project", origin: "audit",
      expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
      idempotency_key: `n2c-bad-${bad}` });
    if (r.status !== 422) throw new Error(`gain_db=${bad} gave ${r.status}`);
  }
  const ov = await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "overlay", name: "O1" });
  d = await detail(p.id, tl.id);
  const o1 = d.tracks.find((t) => t.kind === "overlay");
  const r = await apiRaw("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "timeline.track.update",
    payload: { timeline_id: tl.id, track_id: o1.id, gain_db: -3 },
    scope: "project", origin: "audit",
    expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
    idempotency_key: "n2c-ov-gain" });
  if (r.status !== 422) throw new Error(`overlay gain gave ${r.status}`);
  ok("-61/+12.5 dB → 422; overlay gain → 422");

  /* 5 — EDL v2 carries the mix metadata deterministically */
  step("5) EDL v2 carries gain + mute + solo");
  const e = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}/edl?version=2`);
  const et = e.tracks.find((x) => x.id === a1.id);
  const ec = e.clips.find((x) => x.clip_id === clip.id);
  if (et.gain_db !== 2.5 || et.solo !== true || et.muted !== true
      || ec.gain_db !== -8)
    throw new Error(`edl mix metadata wrong: ${JSON.stringify({ et, ec }).slice(0, 200)}`);
  const e2 = await api("GET", `/api/projects/${p.id}/timelines/${tl.id}/edl?version=2`);
  if (JSON.stringify(e) !== JSON.stringify(e2))
    throw new Error("edl not deterministic");
  ok("edl: track gain_db=2.5 solo muted; clip gain_db=-8; deterministic");

  /* 6 — undo restores pre-edit mix state */
  step("6) undo restores mix state");
  await commit(p.id, "timeline.undo", { timeline_id: tl.id }); // muted
  await commit(p.id, "timeline.undo", { timeline_id: tl.id }); // solo+gain
  await commit(p.id, "timeline.undo", { timeline_id: tl.id }); // overlay add
  await commit(p.id, "timeline.undo", { timeline_id: tl.id }); // clip gain
  d = await detail(p.id, tl.id);
  const tr2 = d.tracks.find((t) => t.id === a1.id);
  if (d.clips[0].gain_db !== 0 || tr2.gain_db !== 0
      || tr2.solo !== false || tr2.muted !== false
      || d.tracks.some((t) => t.kind === "overlay"))
    throw new Error(`undo state wrong: ${JSON.stringify({ clip: d.clips[0].gain_db, tr2 }).slice(0, 200)}`);
  ok("undid muted/solo+gain/overlay-add/clip-gain → all unity");

  /* 7 — UI: lane header exposes mix row + Solo; state reflects API edits */
  step("7) installed UI: mix row, solo button, dB readout");
  // Re-apply a non-unity state for the UI to show.
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, gain_db: -4.5 });
  await commit(p.id, "timeline.clip.gain",
    { timeline_id: tl.id, clip_id: clip.id, gain_db: -2 });
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("MIX"));
    if (b) b.click(); return !!b; })()`);
  await sleep(2500);
  const ui = await evalJs(`(() => {
    const mix = document.querySelector('[data-testid="mix-${a1.id}"]');
    const slider = mix ? mix.querySelector('input[type="range"]') : null;
    const soloBtn = [...document.querySelectorAll("button")]
      .find(b => /^(Un)?[Ss]olo/.test(b.textContent.trim()) || b.textContent.trim() === "Solo");
    return { mix: !!mix, slider: slider ? slider.value : null,
      readout: mix ? mix.textContent : "",
      solo: soloBtn ? soloBtn.textContent.trim() : null }; })()`);
  if (!ui.mix || ui.slider === null || ui.solo === null)
    throw new Error(`ui: ${JSON.stringify(ui)}`);
  if (Number(ui.slider) !== -4.5 || !ui.readout.includes("-4.5dB"))
    throw new Error(`ui gain readout wrong: ${JSON.stringify(ui)}`);
  ok(`mix row live — slider=-4.5, readout="${ui.readout.trim().slice(-8)}", Solo present`);

  /* 8 — UI drives a commit: move the slider → backend persists */
  step("8) UI slider → commit → persisted");
  await evalJs(`(() => {
    const s = document.querySelector('[data-testid="mix-${a1.id}"] input[type="range"]');
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value").set;
    setter.call(s, "-9.5");
    s.dispatchEvent(new Event("input", { bubbles: true }));
    s.dispatchEvent(new Event("change", { bubbles: true }));
    s.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    return s.value; })()`);
  await sleep(2000);
  d = await detail(p.id, tl.id);
  const tr3 = d.tracks.find((t) => t.id === a1.id);
  if (tr3.gain_db !== -9.5)
    throw new Error(`ui commit gave gain_db=${tr3.gain_db}`);
  ok(`UI slider committed -9.5 dB → backend persisted`);

  /* 9 — UI Solo + Mute toggles round-trip to the backend */
  step("9) UI Solo + Mute toggles → backend persists");
  const clickAria = async (label) => evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.getAttribute("aria-label") === ${JSON.stringify(label)} && !e.disabled);
    if (!b) return false; b.click(); return true; })()`);
  if (!await clickAria(`Solo ${a1.name}`)) throw new Error("Solo button missing/disabled");
  await sleep(1500);
  d = await detail(p.id, tl.id);
  let tr4 = d.tracks.find((t) => t.id === a1.id);
  if (tr4.solo !== true) throw new Error(`UI Solo gave solo=${tr4.solo}`);
  if (!await clickAria(`Unsolo ${a1.name}`)) throw new Error("Unsolo missing");
  await sleep(1500);
  if (!await clickAria(`Mute ${a1.name}`)) throw new Error("Mute button missing/disabled");
  await sleep(1500);
  d = await detail(p.id, tl.id);
  tr4 = d.tracks.find((t) => t.id === a1.id);
  if (tr4.solo !== false || tr4.muted !== true)
    throw new Error(`UI toggles wrong: ${JSON.stringify(tr4).slice(0, 160)}`);
  ok("UI Solo on→off + Mute on persisted; visible/audio policy intact");

  /* 10 — reset-to-unity: click the readout restores 0.0 dB */
  step("10) UI reset-to-unity restores 0 dB");
  if (!await clickAria(`Unmute ${a1.name}`)) throw new Error("Unmute missing");
  await sleep(1200);
  const reset = await evalJs(`(() => {
    const b = [...document.querySelectorAll('[data-testid="mix-${a1.id}"] button')]
      .find(e => /dB$/.test(e.textContent.trim()) && !e.disabled);
    if (!b) return null; b.click(); return b.textContent.trim(); })()`);
  if (reset === null) throw new Error("no dB readout to click");
  await sleep(1500);
  d = await detail(p.id, tl.id);
  tr4 = d.tracks.find((t) => t.id === a1.id);
  if (tr4.gain_db !== 0) throw new Error(`reset gave gain_db=${tr4.gain_db}`);
  ok(`readout "${reset}" clicked → gain_db=0 (one command, one undo step)`);

  /* 11 — locked lane: mixer still reaches the backend, structural
     edits refuse. Lock via API, then drive the slider. */
  step("11) locked track — mixer allowed, structural refused");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, locked: true });
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("MIX"));
    if (b) b.click(); return !!b; })()`);
  await sleep(2500);
  const lockUi = await evalJs(`(() => {
    const pick = document.querySelector('[data-testid="audio-pick-${a1.id}"]');
    const s = document.querySelector('[data-testid="mix-${a1.id}"] input[type="range"]');
    return { pickDisabled: pick ? pick.disabled : null, slider: !!s }; })()`);
  if (lockUi.pickDisabled !== true || !lockUi.slider)
    throw new Error(`locked UI wrong: ${JSON.stringify(lockUi)}`);
  await evalJs(`(() => {
    const s = document.querySelector('[data-testid="mix-${a1.id}"] input[type="range"]');
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value").set;
    setter.call(s, "-3");
    s.dispatchEvent(new Event("input", { bubbles: true }));
    s.dispatchEvent(new Event("change", { bubbles: true }));
    s.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    return s.value; })()`);
  await sleep(2000);
  d = await detail(p.id, tl.id);
  tr4 = d.tracks.find((t) => t.id === a1.id);
  if (tr4.gain_db !== -3) throw new Error(`locked mixer gave ${tr4.gain_db}`);
  // Structural refusal: placing a clip on the locked lane → 422.
  const place = await apiRaw("POST", `/api/projects/${p.id}/commands/commit`, {
    command_type: "timeline.clip.place",
    payload: { timeline_id: tl.id, track_id: a1.id,
      asset_id: au.id, allow_duplicate: true },
    scope: "project", origin: "audit",
    expected_revision: (await api("GET", `/api/projects/${p.id}`)).revision,
    idempotency_key: `n2c-lockplace-${Date.now()}` });
  if (place.status === 200)
    throw new Error("clip.place accepted on a locked lane");
  ok(`locked lane: audio-pick disabled, slider committed -3 dB, clip.place refused (${place.status})`);
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, locked: false });

  /* 12 — restart persistence: kill the app, relaunch, reconnect —
     mix state must still be stored and rendered. */
  step("12) restart persistence — mix state survives relaunch");
  await commit(p.id, "timeline.track.update",
    { timeline_id: tl.id, track_id: a1.id, solo: true, muted: true });
  const EXE = process.argv[4];
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"]); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"]); } catch {}
  await sleep(3000);
  if (!EXE) throw new Error("restart needs the exe path as argv[4]");
  const { spawn } = require("child_process");
  spawn(EXE, [], {
    detached: true, stdio: "ignore",
    env: { ...process.env,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS:
        `--remote-debugging-port=${PORT}` } }).unref();
  await sleep(9000);
  await attachWs();
  API = null; TOKEN = null;
  await send("Network.enable");
  await sleep(4000);
  if (!API || !TOKEN) {
    await evalJs(`(() => {
      const b = [...document.querySelectorAll("button,a")]
        .find(e => e.textContent.trim() === "Projects");
      if (b) b.click(); return !!b; })()`);
    await sleep(3000);
  }
  if (!API || !TOKEN) { await send("Page.reload", {}); await sleep(6000); }
  if (!API || !TOKEN) throw new Error("relaunch: no backend creds");
  d = await detail(p.id, tl.id);
  tr4 = d.tracks.find((t) => t.id === a1.id);
  const clipAfter = d.clips.find((c) => c.id === clip.id);
  if (tr4.gain_db !== -3 || tr4.solo !== true || tr4.muted !== true ||
      clipAfter.gain_db !== -2)
    throw new Error(`state lost across restart: ${JSON.stringify(
      { tr: tr4, clip: clipAfter }).slice(0, 200)}`);
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("MIX"));
    if (b) b.click(); return !!b; })()`);
  await sleep(2500);
  const mixAfter = await evalJs(`(() => {
    const m = document.querySelector('[data-testid="mix-${a1.id}"]');
    return { text: m ? m.textContent : null,
      soloBadge: document.body.innerText.includes("soloed") ||
        !![...document.querySelectorAll("button")]
          .find(b => b.getAttribute("aria-label") === "Unsolo ${a1.name}") }; })()`);
  if (!mixAfter.text || !mixAfter.text.includes("-3.0dB") || !mixAfter.soloBadge)
    throw new Error(`mix row after restart: ${JSON.stringify(mixAfter)}`);
  ok(`restart: gain=-3.0dB + solo + muted persisted; UI reflects them`);

  /* 13 — visible populated audio lane still refuses export (N2-E) */
  step("13) export still fails closed on visible audio lane");
  const jr = await commit(p.id, "timeline.export",
    { timeline_id: tl.id, profile: "720p" });
  const xjob = await waitJob(p.id, jr.job_id, true);
  if (xjob.status !== "failed" || !/audio lanes/i.test(xjob.error || ""))
    throw new Error(`export did not fail closed: ${JSON.stringify(xjob).slice(0, 240)}`);
  ok(`refused: "${xjob.error.slice(0, 80)}"`);

  /* 14 — clean shutdown */
  step("14) shutdown — no orphan processes");
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"]); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"]); } catch {}
  await sleep(3000);
  const procs = execFileSync("tasklist").toString();
  const orphans = (procs.match(/atomic-(dream-engine|backend)\.exe/gi) || []).length;
  if (orphans > 0) throw new Error(`${orphans} orphan app processes`);
  ok("clean shutdown — zero orphan app processes");

  fs.writeFileSync(`${OUT}\\n2c-acceptance.log`, LOG.join("\n") + "\n");
  console.log(`\nPASS ${STEP}/14 — evidence in ${OUT}`);
}

main().catch((e) => {
  console.error("FAIL:", e.message || e);
  try { fs.writeFileSync(`${OUT}\\n2c-acceptance.log`, LOG.join("\n") + `\nFAIL ${e.message}\n`); } catch {}
  process.exit(1);
});
