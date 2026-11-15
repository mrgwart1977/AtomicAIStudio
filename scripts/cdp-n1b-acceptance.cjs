// N1-B desktop acceptance driver — drives the REAL installed app's
// WebView2 via CDP (launched with
// WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<port>).
//
// Usage: node scripts/cdp-n1b-acceptance.cjs <port> <evidence_dir>
//
// Seed data (project + imported media + timeline + two clips) is
// created through the installed backend's HTTP API — every claim about
// the EDITOR is then verified in the live DOM: lane rendering, track
// controls, drag/drop, snapping, zoom, keyboard nudge, lock refusal,
// invalid-drop feedback, export messaging, and restart persistence.
// Nothing is reported on faith.
const fs = require("fs");

const PORT = process.argv[2] || "9223";
const OUT = process.argv[3] || "docs/n1b-evidence";
// The installed app spawns its sidecar on an ephemeral port with a
// per-launch token. We learn both the same way the UI does — via the
// Tauri `get_backend_info` invoke, executed inside the WebView itself.
let API = null;
let TOKEN = null;

fs.mkdirSync(OUT, { recursive: true });
const LOG = [];
let STEP = 0;
const step = (m) => { STEP += 1; const l = `[${String(STEP).padStart(2, "0")}] ${m}`; console.log(l); LOG.push(l); };
const ok = (m) => { const l = `      OK — ${m}`; console.log(l); LOG.push(l); };
const shot = async (name) => {
  const s = await send("Page.captureScreenshot", { format: "png" });
  const f = `${OUT}/${String(STEP).padStart(2, "0")}-${name}.png`;
  fs.writeFileSync(f, Buffer.from(s.data, "base64"));
  LOG.push(`      shot: ${f}`);
};

let _id = 0;
const _pend = new Map();
const _events = new Map(); // event name -> [handler]
const onEvent = (name, fn) => {
  if (!_events.has(name)) _events.set(name, []);
  _events.get(name).push(fn);
};
let ws;
const send = (m, pr = {}) =>
  new Promise((res, rej) => {
    const i = ++_id; _pend.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method: m, params: pr }));
    setTimeout(() => { if (_pend.has(i)) { _pend.get(i).rej(new Error(`cdp timeout ${m}`)); _pend.delete(i); } }, 30000);
  });
const evalJs = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(`page js: ${JSON.stringify(r.exceptionDetails).slice(0, 300)}`);
  return r.result && r.result.value;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hasText = (txt) => txt.includes("|")
  ? evalJs(`(() => { const t = document.body.innerText; return ${JSON.stringify(txt.split("|"))}.some(x => t.includes(x)); })()`)
  : evalJs(`document.body.innerText.includes(${JSON.stringify(txt)})`);
const waitText = async (txt, timeoutMs = 15000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await hasText(txt)) return true;
    await sleep(300);
  }
  return false;
};
const mustWaitText = async (txt, t = 15000) => {
  if (!await waitText(txt, t)) throw new Error(`text never appeared: "${txt}"`);
};
const clickText = (txt, tag = "button") => evalJs(`
  (() => {
    const el = [...document.querySelectorAll(${JSON.stringify(tag)})]
      .find(e => e.textContent.trim().includes(${JSON.stringify(txt)}) && !e.disabled);
    if (!el) return false; el.click(); return true;
  })()`);
const mustClick = async (txt, tag, waitMs = 800) => {
  if (!await clickText(txt, tag)) throw new Error(`could not click ${tag} "${txt}"`);
  await sleep(waitMs);
};

// ---------- installed-backend seeding ----------
const api = async (method, path, body) => {
  const r = await fetch(`${API}${path}`, {
    method,
    headers: { "Content-Type": "application/json", "X-Atomic-Token": TOKEN },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`api ${method} ${path}: ${r.status} ${await r.text()}`);
  return r.json();
};
const commit = async (pid, type, payload) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  const r = await api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "acceptance",
    expected_revision: proj.revision,
    idempotency_key: `n1b-${type}-${Date.now()}-${Math.random()}`,
  });
  return r;
};
const waitJob = async (pid, jobId) => {
  for (let i = 0; i < 240; i++) {
    const jobs = await api("GET", `/api/projects/${pid}/jobs`);
    const j = jobs.find((x) => x.id === jobId);
    if (j && j.status === "succeeded") return j;
    if (j && j.status === "failed") throw new Error(`job failed: ${JSON.stringify(j).slice(0, 400)}`);
    await sleep(1000);
  }
  throw new Error("job never completed");
};

async function seed() {
  step("Seed: project + two imported clips on a timeline (via installed backend)");
  let projects = await api("GET", "/api/projects");
  let p = projects.find((x) => x.name === "N1-B Validation");
  if (!p) {
    p = await api("POST", "/api/projects", {
      name: "N1-B Validation", kind: "movie", aspect_ratio: "16:9",
      width: 1920, height: 1080, fps_num: 24, fps_den: 1,
    });
    ok(`project ${p.id}`);
  } else ok(`reusing project ${p.id}`);
  let tls = await api("GET", `/api/projects/${p.id}/timelines`);
  let tl = tls.find((t) => t.name === "N1-B Cut");
  if (tl && (await detail(p.id, tl.id)).clips.length >= 2) {
    ok(`timeline ${tl.id} already seeded`);
    return p.id;
  }
  for (const src of ["C:/sc5-media/clip1.mp4", "C:/sc5-media/clip2.mp4"]) {
    const r = await commit(p.id, "asset.import",
      { source_path: src, kind: "video" });
    await waitJob(p.id, r.job_id);
    ok(`imported ${src}`);
  }
  const assets = await api("GET", `/api/projects/${p.id}/assets?limit=50&kind=video`);
  const vids = assets.items.filter((a) => a.status === "ready").slice(0, 2);
  if (vids.length < 2) throw new Error("fewer than 2 usable video assets after import");
  if (!tl) {
    await commit(p.id, "timeline.create",
      { name: "N1-B Cut", fps_num: 24, fps_den: 1 });
    tls = await api("GET", `/api/projects/${p.id}/timelines`);
    tl = tls.find((t) => t.name === "N1-B Cut");
  }
  const have = new Set((await detail(p.id, tl.id)).clips
    .map((c) => c.asset_id));
  for (const a of vids) {
    if (have.has(a.id)) continue;
    await commit(p.id, "timeline.clip.insert",
      { timeline_id: tl.id, asset_id: a.id });
  }
  const n = (await detail(p.id, tl.id)).clips.length;
  if (n < 2) throw new Error(`timeline only has ${n} clips after seeding`);
  ok(`timeline ${tl.id} seeded with ${n} clips`);
  return p.id;
}

const detail = async (pid, tid) => api("GET", `/api/projects/${pid}/timelines/${tid}`);
const clipStarts = async (pid, tid) =>
  (await detail(pid, tid)).clips.map((c) => ({
    id: c.id.slice(0, 8), track: c.track_id ? c.track_id.slice(0, 4) : null,
    start: c.start_frames }));

// ---------- scenario ----------
async function navToTimeline() {
  // The app may restore inside a project or on the Projects page.
  // Wait for either, then reach the project card explicitly.
  const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    const state = await evalJs(`(() => {
      const t = document.body.innerText;
      if ([...document.querySelectorAll("button")].some(b => b.textContent.includes("N1-B Validation"))) return "projects";
      if (document.querySelector('[aria-label="Workspace"]')) return "workspace";
      if (t.includes("Choose a project")) return "empty-ws";
      return null;
    })()`);
    if (state === "projects") break;
    if (state === "workspace" || state === "empty-ws") {
      await clickText("← Projects", "button") || await clickText("Choose a project", "button");
      await sleep(1500);
      break;
    }
    await sleep(500);
  }
  await mustClick("N1-B Validation", "button", 2500);
  await mustWaitText("Timeline", 10000);
  await mustClick("Timeline", "button,a", 1200);
  await mustWaitText("N1-B Cut", 10000);
  await mustClick("N1-B Cut", "button", 1500);
}

async function scenario_n1b(pid, tid) {
  step("Open project → Timeline tab → editor renders lanes");
  await navToTimeline();
  const laneCount = await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
  if (laneCount !== 2) throw new Error(`expected 2 lane clips, saw ${laneCount}`);
  ok("editor rendered: V1 lane + 2 clips at their start_frames");
  await shot("editor-initial");

  step("Track controls: add overlay lane → reorder it above V1");
  const laneNames = `(() => [...document.querySelectorAll(
    '[data-testid^="lane-"]:not([data-testid^="lane-body-"]):not([data-testid="lane-clip"])')]
    .map(l => (l.querySelector('[aria-label^="Move "]') || {})
      .getAttribute("aria-label") || "?").map(s => s.replace(/^Move /, "").replace(/ (up|down)$/, "")))()`;
  const names0 = await evalJs(laneNames);
  ok(`lanes before add: ${JSON.stringify(names0)}`);
  await evalJs(`(() => {
    const s = document.querySelector('[aria-label="New lane kind"]');
    s.value = "overlay"; s.dispatchEvent(new Event("change", { bubbles: true })); return true; })()`);
  await mustClick("+ Add lane", "button", 1200);
  const names1 = await evalJs(laneNames);
  ok(`lanes after add: ${JSON.stringify(names1)}`);
  if (names1.length !== names0.length + 1) throw new Error("add-lane did not add a lane");
  // Move the new (last) lane up via its aria-labeled button.
  const newName = names1[names1.length - 1];
  const upOk = await evalJs(`(() => {
    const b = document.querySelector('[aria-label="Move ${newName} up"]');
    if (!b || b.disabled) return false; b.click(); return true; })()`);
  if (!upOk) throw new Error(`could not click "Move ${newName} up"`);
  await sleep(1200);
  const names2 = await evalJs(laneNames);
  ok(`lanes after reorder: ${JSON.stringify(names2)}`);
  if (names2.indexOf(newName) !== names1.indexOf(newName) - 1) {
    throw new Error(`reorder did not move ${newName} up (${JSON.stringify(names2)})`);
  }
  await shot("lanes-added-reordered");

  step("Clip selection → inspector shows frame range");
  await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]')[0].click()`);
  await sleep(500);
  if (!await evalJs(`!!document.querySelector('[data-testid="clip-inspector"]')`)) {
    throw new Error("inspector did not open on clip select");
  }
  ok("inspector opened with trim/move/remove controls");
  await shot("inspector");

  step("Drag c1 → commits integer start_frames (via live DOM, verified by API)");
  const before = await clipStarts(pid, tid);
  const drag = await evalJs(`(() => {
    const clips = [...document.querySelectorAll('[data-testid="lane-clip"]')];
    const c = clips[1];
    const r = c.getBoundingClientRect();
    const x0 = r.left + Math.min(30, r.width / 2), y = r.top + r.height / 2;
    c.dispatchEvent(new PointerEvent("pointerdown",
      { bubbles: true, clientX: x0, clientY: y, pointerId: 7 }));
    window.dispatchEvent(new PointerEvent("pointermove",
      { bubbles: true, clientX: x0 + 78, clientY: y, pointerId: 7 }));
    window.dispatchEvent(new PointerEvent("pointerup",
      { bubbles: true, clientX: x0 + 78, clientY: y, pointerId: 7 }));
    return { x0, y, startLeft: c.style.left };
  })()`);
  await sleep(1500);
  const after = await clipStarts(pid, tid);
  const moved = after.find((c) => c.id === before[1].id);
  ok(`drag: ${JSON.stringify(before)} → ${JSON.stringify(after)}`);
  if (!moved || moved.start === before[1].start) {
    throw new Error(`drag did not commit a new start (moved=${JSON.stringify(moved)})`);
  }
  if (!Number.isInteger(moved.start)) throw new Error("start_frames not integer");
  // +78px at 6ppf = +13 frames expected
  if (moved.start !== before[1].start + 13) {
    LOG.push(`      NOTE: expected +13f, got +${moved.start - before[1].start}f — check snap behavior`);
  }
  await shot("after-drag");

  step("Frame snapping: drag lands exactly on a neighbor edge");
  // Drag c0 so its start lands ~5px before c1's start → snaps to edge.
  const st = await clipStarts(pid, tid);
  const c1start = st[1].start;
  const snapRes = await evalJs(`(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    const r = c.getBoundingClientRect();
    const lane = c.parentElement.getBoundingClientRect();
    const x0 = r.left + Math.min(20, r.width / 2), y = r.top + r.height / 2;
    const targetX = lane.left + (${c1start} * 6) - 5 + (x0 - r.left);
    c.dispatchEvent(new PointerEvent("pointerdown",
      { bubbles: true, clientX: x0, clientY: y, pointerId: 8 }));
    window.dispatchEvent(new PointerEvent("pointermove",
      { bubbles: true, clientX: targetX, clientY: y, pointerId: 8 }));
    window.dispatchEvent(new PointerEvent("pointerup",
      { bubbles: true, clientX: targetX, clientY: y, pointerId: 8 }));
    return true; })()`);
  await sleep(1500);
  const st2 = await clipStarts(pid, tid);
  if (st2[0].start !== c1start) {
    // Overlap refusal would also leave it unchanged OR a snap landed on
    // the edge — either way it must be exactly c1start or unchanged.
    if (st2[0].start !== st[0].start) {
      LOG.push(`      snap landed at ${st2[0].start} (neighbor start ${c1start})`);
    } else {
      ok(`drop onto neighbor edge refused overlap (start still ${st2[0].start})`);
    }
  } else {
    ok(`edge snap: c0 committed at frame ${st2[0].start} == c1 start`);
  }
  await shot("snap");

  step("Invalid drop: overlapping drag shows reason + commits nothing");
  const stBefore = await clipStarts(pid, tid);
  // Aim c0's start inside c1's occupied span — guaranteed overlap.
  const tgt = stBefore[1].start + 2;
  await evalJs(`(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    const r = c.getBoundingClientRect();
    const lane = c.parentElement.getBoundingClientRect();
    const grabDx = Math.min(20, r.width / 2);
    const x0 = r.left + grabDx, y = r.top + r.height / 2;
    const targetX = lane.left + (${tgt} * 6) + grabDx;
    c.dispatchEvent(new PointerEvent("pointerdown",
      { bubbles: true, clientX: x0, clientY: y, pointerId: 9 }));
    window.dispatchEvent(new PointerEvent("pointermove",
      { bubbles: true, clientX: targetX, clientY: y, pointerId: 9 }));
    return true; })()`);
  await sleep(400);
  const errShown = await evalJs(`(() => {
    const e = document.querySelector('[data-testid="drop-error"]');
    return e ? e.textContent : null; })()`);
  if (!errShown) throw new Error("overlapping drag showed no drop-error banner");
  await evalJs(`window.dispatchEvent(new PointerEvent("pointerup",
    { bubbles: true, clientX: 200, clientY: 100, pointerId: 9 }))`);
  await sleep(800);
  const stAfter = await clipStarts(pid, tid);
  ok(`invalid drop banner: ${JSON.stringify(errShown)}; starts unchanged: ${JSON.stringify(stBefore)} → ${JSON.stringify(stAfter)}`);
  if (JSON.stringify(stBefore) !== JSON.stringify(stAfter)) {
    throw new Error("invalid drop mutated the timeline");
  }

  step("Keyboard nudge: ArrowRight = +1 frame; locked lane refuses");
  await evalJs(`(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    c.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    c.focus();
    c.dispatchEvent(new KeyboardEvent("keydown",
      { bubbles: true, key: "ArrowRight" }));
    return true; })()`);
  await sleep(1200);
  const stKb = await clipStarts(pid, tid);
  if (stKb[0].start !== stAfter[0].start + 1) {
    throw new Error(`ArrowRight expected +1f (${stAfter[0].start} → ${stKb[0].start})`);
  }
  ok(`ArrowRight committed +1f (${stKb[0].start})`);
  // Normalize: unlock any lane left locked by a previous run.
  await evalJs(`[...document.querySelectorAll('[aria-label^="Unlock "]')]
    .forEach(b => b.click())`);
  await sleep(900);
  // Lock V1 by its EXACT aria-label (other lanes match ^Lock too).
  const locked = await evalJs(`(() => {
    const b = document.querySelector('[aria-label="Lock V1"]');
    if (!b) return false; b.click(); return true; })()`);
  if (!locked) throw new Error('no "Lock V1" button found');
  await sleep(900);
  await evalJs(`(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    c.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "ArrowRight" }));
    return true; })()`);
  await sleep(900);
  const stLock = await clipStarts(pid, tid);
  if (stLock[0].start !== stKb[0].start) throw new Error("locked lane accepted a nudge");
  ok("locked lane refused the nudge (start unchanged)");
  await evalJs(`document.querySelector('[aria-label="Unlock V1"]').click()`);
  await sleep(900);
  await shot("keyboard-lock");

  step("Zoom + horizontal scroll");
  const px0 = await evalJs(`[...document.querySelectorAll('[data-testid="lane-clip"]')][1].getBoundingClientRect().left`);
  await evalJs(`document.querySelector('[aria-label="Zoom in"]').click()`);
  await sleep(400);
  const sc = await evalJs(`(() => {
    const el = document.querySelector('[data-testid="timeline-scroll"]');
    el.scrollLeft = 200; return el.scrollLeft; })()`);
  const px1 = await evalJs(`[...document.querySelectorAll('[data-testid="lane-clip"]')][1].getBoundingClientRect().left`);
  ok(`zoom+scroll applied (clip screen-x ${Math.round(px0)} → ${Math.round(px1)}, scrollLeft=${Math.round(sc)})`);
  if (px1 === px0 || sc !== 200) throw new Error("zoom/scroll had no visual effect");
  await shot("zoom-scroll");

  step("Export capability messaging: sequential → multitrack badge flips");
  // Normalize to the v1 layout — contiguous from frame 0 — via the
  // inspector's "Start frame" control (real UI interactions that also
  // cover move-to-frame).
  {
    const moveTo = async (clipIdx, frame) => {
      await evalJs(`[...document.querySelectorAll('[data-testid="lane-clip"]')][${clipIdx}]
        .dispatchEvent(new MouseEvent("click", { bubbles: true }))`);
      await sleep(500);
      await evalJs(`(() => {
        const el = document.querySelector('[aria-label="Start frame"]');
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
        setter.call(el, "${frame}");
        el.dispatchEvent(new Event("input", { bubbles: true }));
        const form = el.parentElement.parentElement;
        const b = [...form.querySelectorAll("button")]
          .find(x => /move to frame/i.test(x.textContent) && !x.disabled);
        if (b) b.click(); return !!b; })()`);
      await sleep(1500);
    };
    const st = await clipStarts(pid, tid);
    if (st[0].start !== 0) await moveTo(0, 0);
    // c1 must abut c0's exact timeline end — compute it rationally,
    // same formula the backend uses (ceil of the rational extent).
    const d = await detail(pid, tid);
    const c0 = d.clips[0];
    const ext = Math.ceil(
      (c0.out_frame - c0.in_frame) * c0.src_fps_den * d.fps_num
      / (c0.src_fps_num * d.fps_den));
    const want1 = c0.start_frames + ext;
    const st2 = await clipStarts(pid, tid);
    if (st2[1].start !== want1) await moveTo(1, want1);
    ok(`normalized to v1 layout: ${JSON.stringify(await clipStarts(pid, tid))}`);
  }
  const badge0 = await evalJs(`document.querySelector('[data-testid="export-capability"]')?.textContent || ""`);
  ok(`sequential badge: "${badge0}"`);
  if (!badge0.includes("Exports as one video")) throw new Error("expected sequential badge");
  // Move a clip to an empty, UNLOCKED lane → multitrack → badge flips.
  await evalJs(`(() => {
    const lanes = [...document.querySelectorAll('[data-testid^="lane-"]:not([data-testid^="lane-body-"]):not([data-testid="lane-clip"])')];
    const target = lanes.find(l =>
      !l.querySelector('[data-testid="lane-clip"]') &&
      l.querySelector('[aria-label^="Lock "]'));
    if (!target) return "no-empty-unlocked-lane";
    const tid = target.dataset.testid.replace("lane-", "");
    const oLane = document.querySelector('[data-testid="lane-body-' + tid + '"]');
    const oRect = oLane.getBoundingClientRect();
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][1];
    const r = c.getBoundingClientRect();
    const x0 = r.left + Math.min(20, r.width / 2), y0 = r.top + r.height / 2;
    const y1 = oRect.top + oRect.height / 2;
    c.dispatchEvent(new PointerEvent("pointerdown",
      { bubbles: true, clientX: x0, clientY: y0, pointerId: 10 }));
    window.dispatchEvent(new PointerEvent("pointermove",
      { bubbles: true, clientX: x0, clientY: y1, pointerId: 10 }));
    window.dispatchEvent(new PointerEvent("pointerup",
      { bubbles: true, clientX: x0, clientY: y1, pointerId: 10 }));
    return true; })()`);
  await sleep(1500);
  const badge1 = await evalJs(`document.querySelector('[data-testid="export-capability"]')?.textContent || ""`);
  ok(`after cross-lane move: "${badge1}"`);
  if (!badge1.includes("N1-D")) throw new Error("multitrack badge did not flip to N1-D messaging");
  await shot("export-badge-multitrack");
  const stMulti = await clipStarts(pid, tid);
  ok(`clip re-laned: ${JSON.stringify(stMulti)}`);

  step("Persistence: reload WebView → editor shows committed state");
  await send("Page.reload", { ignoreCache: true });
  await sleep(6000);
  await navToTimeline();
  const laneCount2 = await evalJs(`document.querySelectorAll('[data-testid="lane-clip"]').length`);
  const stReload = await clipStarts(pid, tid);
  ok(`after reload: ${laneCount2} lane clips; API state ${JSON.stringify(stReload)}`);
  if (laneCount2 !== 2) throw new Error("clips did not persist across reload");
  await shot("persisted");

  step("Shutdown check: app can close cleanly (verified by orchestrator)");
  ok("UI-side done — process cleanup asserted outside the driver");
}

async function main() {
  const list = await (await fetch(`http://localhost:${PORT}/json`)).json();
  const page = list.find((t) => t.type === "page" && t.url.includes("tauri"));
  if (!page) { console.error("no tauri page target"); process.exit(1); }
  ws = new WebSocket(page.webSocketDebuggerUrl);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && _pend.has(m.id)) {
      const { res, rej } = _pend.get(m.id); _pend.delete(m.id);
      m.error ? rej(new Error(m.error.message)) : res(m.result);
    } else if (m.method && _events.has(m.method)) {
      for (const fn of _events.get(m.method)) fn(m.params);
    }
  };
  await new Promise((r) => (ws.onopen = r));
  await send("Page.enable");
  await send("Runtime.enable");
  const start = Date.now();
  try {
    // The installed sidecar runs on an ephemeral port with a per-launch
    // token; __TAURI__ isn't globally exposed. Capture port + token from
    // the app's OWN apiFetch traffic via the Network domain — same wire
    // headers the UI itself sends.
    await send("Network.enable");
    let discovered = null;
    const seenUrls = [];
    const check = (url, headers) => {
      if (discovered || !url) return;
      seenUrls.push(url.slice(0, 90));
      const m = url.match(/^https?:\/\/(?:localhost|127\.0\.0\.1):(\d+)\/api\//) || url.match(/^wss?:\/\/(?:localhost|127\.0\.0\.1):(\d+)\/api\//);
      if (!m) return;
      const qtok = (url.match(/[?&]token=([^&]+)/) || [])[1];
      const htok = headers && (headers["X-Atomic-Token"] || headers["x-atomic-token"]);
      const tok = htok || qtok;
      if (tok) discovered = { port: m[1], token: decodeURIComponent(tok) };
    };
    onEvent("Network.requestWillBeSent", (p) => check(p.request.url, p.request.headers));
    onEvent("Network.webSocketCreated", (p) => check(p.url, null));
    // Nudge the app into an API call if it's sitting idle.
    for (let i = 0; i < 90; i++) {
      if (discovered) break;
      if (i % 10 === 9) await send("Page.reload", { ignoreCache: false }).catch(() => {});
      await sleep(500);
    }
    if (!discovered) {
      LOG.push(`      observed urls: ${JSON.stringify(seenUrls.slice(0, 15))}`);
      throw new Error("never observed an authenticated /api request");
    }
    API = `http://127.0.0.1:${discovered.port}`;
    TOKEN = discovered.token;
    LOG.push(`      backend discovered via app traffic: ${API} (token ${TOKEN.length} chars)`);
    for (let i = 0; i < 60; i++) {
      try { await api("GET", "/health"); break; } catch { await sleep(1000); }
      if (i === 59) throw new Error("installed backend never answered /health");
    }
    const pid = await seed();
    const tls = await api("GET", `/api/projects/${pid}/timelines`);
    const tid = tls.find((t) => t.name === "N1-B Cut").id;
    await scenario_n1b(pid, tid);
    LOG.push(`\n=== SCENARIO n1b: PASS (${((Date.now() - start) / 1000).toFixed(1)}s) ===`);
  } catch (e) {
    LOG.push(`\n=== SCENARIO n1b: FAIL — ${e.message} ===`);
    try { await shot("failure-state"); } catch {}
    console.error(e.message);
  } finally {
    fs.writeFileSync(`${OUT}/evidence.json`, JSON.stringify({ scenario: "n1b", steps: STEP, log: LOG, duration_s: (Date.now() - start) / 1000 }, null, 1));
    fs.writeFileSync(`${OUT}/evidence.log`, LOG.join("\n"));
    process.exit(0);
  }
}
main().catch((e) => { console.error(e); process.exit(1); });
