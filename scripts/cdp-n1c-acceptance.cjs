// N1-C desktop acceptance driver — drives the REAL installed app's
// WebView2 via CDP (launched with
// WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<port>).
//
// Usage: node scripts/cdp-n1c-acceptance.cjs <port> <evidence_dir>
//
// Verifies the N1-C editorial controls in the live DOM and their
// authoritative commits via the installed backend's HTTP API:
// split-at-playhead, ripple vs non-ripple delete, gap indicators with
// click-to-close, insert-space, and trim-handle drags. Nothing is
// reported on faith — every DOM claim is backed by an API read.
const fs = require("fs");

const PORT = process.argv[2] || "9223";
const OUT = process.argv[3] || "docs/n1c-evidence";
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
const _events = new Map();
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
// The ConfirmDialog's destructive button — labeled just "Confirm" —
// inside the Radix portal (role="dialog").
const clickDialogConfirm = async (waitMs = 1200) => {
  const okd = await evalJs(`(() => {
    const dlg = document.querySelector('[role="dialog"]');
    if (!dlg) return "no-dialog";
    const b = [...dlg.querySelectorAll("button")]
      .find(x => x.textContent.trim() === "Confirm" && !x.disabled);
    if (!b) return "no-confirm";
    b.click(); return true; })()`);
  if (okd !== true) throw new Error(`dialog confirm: ${okd}`);
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
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "acceptance",
    expected_revision: proj.revision,
    idempotency_key: `n1c-${type}-${Date.now()}-${Math.random()}`,
  });
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
const detail = async (pid, tid) => api("GET", `/api/projects/${pid}/timelines/${tid}`);
const ext = (c, tl) => Math.ceil(
  (c.out_frame - c.in_frame) * c.src_fps_den * tl.fps_num
  / (c.src_fps_num * tl.fps_den));

async function seed() {
  step("Seed: project + two contiguous clips on a timeline (via installed backend)");
  let projects = await api("GET", "/api/projects");
  let p = projects.find((x) => x.name === "N1-C Validation");
  if (!p) {
    p = await api("POST", "/api/projects", {
      name: "N1-C Validation", kind: "movie", aspect_ratio: "16:9",
      width: 1920, height: 1080, fps_num: 24, fps_den: 1,
    });
    ok(`project ${p.id}`);
  } else ok(`reusing project ${p.id}`);
  let tls = await api("GET", `/api/projects/${p.id}/timelines`);
  let tl = tls.find((t) => t.name === "N1-C Cut");
  if (tl && (await detail(p.id, tl.id)).clips.length >= 2) {
    ok(`timeline ${tl.id} already seeded`);
    return { pid: p.id, tid: tl.id };
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
      { name: "N1-C Cut", fps_num: 24, fps_den: 1 });
    tls = await api("GET", `/api/projects/${p.id}/timelines`);
    tl = tls.find((t) => t.name === "N1-C Cut");
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
  return { pid: p.id, tid: tl.id };
}

// ---------- scenario ----------
async function navToTimeline() {
  const t0 = Date.now();
  while (Date.now() - t0 < 30000) {
    const state = await evalJs(`(() => {
      const t = document.body.innerText;
      if ([...document.querySelectorAll("button")].some(b => b.textContent.includes("N1-C Validation"))) return "projects";
      if (document.querySelector('[aria-label="Workspace"]')) return "workspace";
      if (t.includes("Choose a project")) return "empty-ws";
      if (t.includes("No project open") || t.includes("Create something extraordinary")) return "home";
      return null;
    })()`);
    if (state === "projects") break;
    if (state === "home") {
      // Nav rail → Projects list.
      await clickText("Projects", "button,a") || await clickText("Projects", "nav *");
      await sleep(1500);
      continue;
    }
    if (state === "workspace" || state === "empty-ws") {
      await clickText("← Projects", "button") || await clickText("Choose a project", "button");
      await sleep(1500);
      break;
    }
    await sleep(500);
  }
  await mustClick("N1-C Validation", "button", 2500);
  await mustWaitText("Timeline", 10000);
  await mustClick("Timeline", "button,a", 1200);
  await mustWaitText("N1-C Cut", 10000);
  await mustClick("N1-C Cut", "button", 1500);
}

// Set the playhead to a timeline frame by pointerdown on the ruler.
const setPlayhead = (frame) => evalJs(`(() => {
  const r = document.querySelector('[data-testid="timeline-ruler"]')
    .getBoundingClientRect();
  document.querySelector('[data-testid="timeline-ruler"]')
    .dispatchEvent(new PointerEvent("pointerdown",
      { bubbles: true, clientX: r.left + ${frame} * 6, clientY: r.top + 8 }));
  return document.querySelector('[data-testid="playhead-readout"]').textContent;
})()`);

async function scenario_n1c(pid, tid) {
  step("Open project → Timeline tab → N1-C controls render");
  await navToTimeline();
  const controls = await evalJs(`(() => ({
    split: !!document.querySelector('[data-testid="split-at-playhead"]'),
    ripple: !!document.querySelector('[data-testid="ripple-toggle"]'),
    ins: !!document.querySelector('[data-testid="insert-space"]'),
    close: !!document.querySelector('[data-testid="close-gap"]'),
    handles: document.querySelectorAll('[data-testid="trim-handle"]').length,
  }))()`);
  if (!controls.split || !controls.ripple || !controls.ins || !controls.close) {
    throw new Error(`missing N1-C controls: ${JSON.stringify(controls)}`);
  }
  if (controls.handles < 2) throw new Error(`expected ≥2 trim handles, saw ${controls.handles}`);
  ok(`controls rendered: ${JSON.stringify(controls)}`);
  await shot("n1c-controls");

  step("Split at playhead → clip.split commits two pinned fragments");
  let d0 = await detail(pid, tid);
  const c0 = d0.clips.sort((a, b) => a.start_frames - b.start_frames)[0];
  const c0Ext = ext(c0, d0);
  // The cut must land on a whole SOURCE frame — mirror the backend's
  // rule and walk down from the midpoint until it does (identity for
  // same-rate media, e.g. every 4th frame for 30fps src on a 24fps tl).
  const srcPerTl = (c0.src_fps_num * d0.fps_den)
    / (c0.src_fps_den * d0.fps_num);
  let mid = c0.start_frames + Math.floor(c0Ext / 2);
  while (mid > c0.start_frames + 1) {
    const off = (mid - c0.start_frames) * srcPerTl;
    if (Math.abs(off - Math.round(off)) < 1e-9) break;
    mid -= 1;
  }
  const readout = await setPlayhead(mid);
  ok(`playhead → ${readout} (split frame ${mid} inside c0 [${c0.start_frames}, ${c0.start_frames + c0Ext}))`);
  await mustClick("Split at playhead", "button", 1800);
  let d1 = await detail(pid, tid);
  const starts = d1.clips.map((c) => c.start_frames).sort((a, b) => a - b);
  const left = d1.clips.find((c) => c.id === c0.id);
  const right = d1.clips.find((c) => c.start_frames === mid);
  ok(`starts after split: ${JSON.stringify(starts)}`);
  if (d1.clips.length !== d0.clips.length + 1) throw new Error("split didn't add a clip");
  if (!left || left.out_frame !== c0.in_frame + (mid - c0.start_frames)) {
    throw new Error(`left fragment wrong (out=${left && left.out_frame})`);
  }
  if (!right || right.in_frame !== left.out_frame) {
    throw new Error(`right fragment wrong (in=${right && right.in_frame})`);
  }
  if (right.source_version_id !== c0.source_version_id
      || right.source_sha256 !== c0.source_sha256) {
    throw new Error("split lost the source pin");
  }
  const visSum = d1.clips.reduce((s, c) => s + (c.out_frame - c.in_frame), 0);
  const visSum0 = d0.clips.reduce((s, c) => s + (c.out_frame - c.in_frame), 0);
  if (visSum !== visSum0) throw new Error(`split changed total duration (${visSum0} → ${visSum})`);
  ok(`split conserved duration (${visSum0} src frames) and kept the pin`);
  await shot("after-split");

  step("Non-ripple delete leaves an explicit gap + indicator in the DOM");
  await evalJs(`(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    c.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    c.dispatchEvent(new KeyboardEvent("keydown",
      { bubbles: true, key: "Delete" })); return true; })()`);
  await sleep(700);
  const confirmTxt = await evalJs(`(() => {
    const dlg = [...document.querySelectorAll("body *")].find(
      e => e.textContent && /Remove this clip/.test(e.textContent)
        && e.children.length < 20);
    return dlg ? dlg.textContent.slice(0, 200) : null; })()`);
  if (confirmTxt && confirmTxt.includes("shift left")) {
    throw new Error("non-ripple confirm wrongly claims a shift");
  }
  await clickDialogConfirm();
  const d2 = await detail(pid, tid);
  if (d2.clips.length !== d1.clips.length - 1) throw new Error("delete didn't remove a clip");
  const afterStarts = d2.clips.map((c) => c.start_frames).sort((a, b) => a - b);
  if (!afterStarts.length || afterStarts[0] === 0) {
    throw new Error(`non-ripple delete should leave a gap: ${JSON.stringify(afterStarts)}`);
  }
  const gapEl = await evalJs(`(() => {
    const g = document.querySelector('[data-testid="lane-gap"]');
    return g ? { title: g.title, disabled: g.disabled } : null; })()`);
  if (!gapEl) throw new Error("no lane-gap indicator in the DOM after non-ripple delete");
  ok(`gap indicator: ${JSON.stringify(gapEl)}; starts: ${JSON.stringify(afterStarts)}`);
  await shot("gap-indicator");

  step("Click the gap indicator → gap.close abuts the next clip");
  await evalJs(`document.querySelector('[data-testid="lane-gap"]').click()`);
  await sleep(1500);
  const d3 = await detail(pid, tid);
  const s3 = d3.clips.map((c) => c.start_frames).sort((a, b) => a - b);
  ok(`after gap.close: ${JSON.stringify(s3)}`);
  if (s3[0] !== 0) throw new Error(`gap.close didn't pull the lane to 0: ${JSON.stringify(s3)}`);
  const gapGone = await evalJs(`document.querySelectorAll('[data-testid="lane-gap"]').length`);
  if (gapGone !== 0) throw new Error(`expected 0 gaps after close, saw ${gapGone}`);
  ok("gap closed — lane contiguous from frame 0, indicator gone");

  step("Insert space at the boundary → follower shifts right by N frames");
  const d4 = await detail(pid, tid);
  const ord4 = d4.clips.slice().sort((a, b) => a.start_frames - b.start_frames);
  const boundary = ord4[0].start_frames + ext(ord4[0], d4);
  await setPlayhead(boundary);
  await evalJs(`(() => {
    const el = document.querySelector('[aria-label="Insert space frames"]');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(el, "12");
    el.dispatchEvent(new Event("input", { bubbles: true })); return true; })()`);
  await mustClick("Insert space", "button", 1500);
  const d5 = await detail(pid, tid);
  const ord5 = d5.clips.slice().sort((a, b) => a.start_frames - b.start_frames);
  ok(`insert-space: ${JSON.stringify(ord4.map(c => c.start_frames))} → ${JSON.stringify(ord5.map(c => c.start_frames))}`);
  if (ord5[ord5.length - 1].start_frames !== ord4[ord4.length - 1].start_frames + 12) {
    throw new Error("follower didn't shift +12");
  }
  if (ord5[0].start_frames !== ord4[0].start_frames) {
    throw new Error("insert-space moved the clip BEFORE the point");
  }
  const gapIns = await evalJs(`document.querySelectorAll('[data-testid="lane-gap"]').length`);
  if (gapIns < 1) throw new Error("inserted gap not shown as a lane-gap");
  ok(`inserted 12f gap renders as an indicator (${gapIns} visible)`);
  await shot("inserted-gap");

  step("Close gap via the toolbar → lane contiguous again");
  await setPlayhead(boundary + 6); // inside the inserted gap
  await mustClick("Close gap", "button", 1500);
  const d6 = await detail(pid, tid);
  const s6 = d6.clips.map((c) => c.start_frames).sort((a, b) => a - b);
  if (JSON.stringify(s6) !== JSON.stringify(ord4.map((c) => c.start_frames))) {
    throw new Error(`close-gap didn't restore contiguity: ${JSON.stringify(s6)}`);
  }
  ok(`close-gap restored starts ${JSON.stringify(s6)}`);

  step("Trim-handle drag → source-frame trim commits on pointerup");
  const d7 = await detail(pid, tid);
  const t0clip = d7.clips.slice().sort((a, b) => a.start_frames - b.start_frames)[0];
  const drag = await evalJs(`(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    const h = c.querySelector('[data-testid="trim-handle"]');
    if (!h) return "no-handle";
    const r = h.getBoundingClientRect();
    const x0 = r.left + 1, y = r.top + r.height / 2;
    h.dispatchEvent(new PointerEvent("pointerdown",
      { bubbles: true, clientX: x0, clientY: y, pointerId: 11 }));
    window.dispatchEvent(new PointerEvent("pointermove",
      { bubbles: true, clientX: x0 - 24, clientY: y, pointerId: 11 }));
    window.dispatchEvent(new PointerEvent("pointerup",
      { bubbles: true, clientX: x0 - 24, clientY: y, pointerId: 11 }));
    return { x0, y }; })()`);
  if (drag === "no-handle") throw new Error("trim handle not found on first clip");
  await sleep(1500);
  const d8 = await detail(pid, tid);
  const t1clip = d8.clips.find((c) => c.id === t0clip.id);
  ok(`trim drag: out_frame ${t0clip.out_frame} → ${t1clip && t1clip.out_frame}`);
  // −24px at 6ppf ≈ −4 source frames (same-rate media); exact value
  // depends on the clip's source rate — assert a real shrink.
  if (!t1clip || t1clip.out_frame >= t0clip.out_frame) {
    throw new Error("trim-handle drag didn't shorten the clip");
  }
  // A retracted end IS an honest data gap — the indicator must appear,
  // and the follower must NOT have moved (non-ripple semantics).
  const gapAfter = await evalJs(`document.querySelectorAll('[data-testid="lane-gap"]').length`);
  if (gapAfter < 1) {
    throw new Error("retracted clip end should render as a lane-gap");
  }
  const ord8 = d8.clips.slice().sort((a, b) => a.start_frames - b.start_frames);
  const ord7 = d7.clips.slice().sort((a, b) => a.start_frames - b.start_frames);
  if (ord8.length > 1
      && ord8[ord8.length - 1].start_frames !== ord7[ord7.length - 1].start_frames) {
    throw new Error("non-ripple trim moved a follower");
  }
  ok("non-ripple trim left the follower in place");
  await shot("trim-handle");

  step("Ripple delete via the confirm dialog → follower shifts left");
  await evalJs(`(() => {
    const t = document.querySelector('[data-testid="ripple-toggle"]');
    if (!t.checked) t.click(); return true; })()`);
  await sleep(400);
  await evalJs(`(() => {
    const c = [...document.querySelectorAll('[data-testid="lane-clip"]')][0];
    c.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    c.dispatchEvent(new KeyboardEvent("keydown",
      { bubbles: true, key: "Delete" })); return true; })()`);
  await sleep(700);
  const rippleTxt = await evalJs(`document.body.innerText`);
  if (!rippleTxt.includes("shift left")) {
    throw new Error("ripple confirm didn't disclose the shift");
  }
  await clickDialogConfirm();
  const d9 = await detail(pid, tid);
  const s9 = d9.clips.map((c) => c.start_frames).sort((a, b) => a - b);
  ok(`ripple delete: ${JSON.stringify(ord8.map(c => c.start_frames))} → ${JSON.stringify(s9)}`);
  if (d9.clips.length !== ord8.length - 1) throw new Error("ripple delete didn't remove a clip");
  // The first follower shifts left by the deleted clip's integer
  // footprint — NOT to 0: the gap the trim created is preserved as a
  // leading gap (honest gap preservation).
  const delOcc = t1clip.start_frames + ext(t1clip, d8);
  const expectedFirst = ord8[1].start_frames - delOcc;
  if (s9[0] !== expectedFirst) {
    throw new Error(`ripple shift wrong: expected ${expectedFirst}, got ${s9[0]}`);
  }
  ok(`follower shifted by the deleted footprint (→ frame ${s9[0]}) — gap preserved`);

  step("Locked-track refusal: lock V1 → handles gone, edits disabled");
  const dLock = await detail(pid, tid);
  const insideF = dLock.clips[0].start_frames + 2;
  await setPlayhead(insideF); // inside the first clip
  await evalJs(`[...document.querySelectorAll('[aria-label^="Lock "]')]
    .find(b => b.getAttribute("aria-label") === "Lock V1").click()`);
  await sleep(1000);
  const lockedState = await evalJs(`(() => ({
    handles: document.querySelectorAll('[data-testid="trim-handle"]').length,
    splitDisabled: document.querySelector('[data-testid="split-at-playhead"]').disabled,
    insDisabled: document.querySelector('[data-testid="insert-space"]').disabled,
    gapDisabled: [...document.querySelectorAll('[data-testid="lane-gap"]')]
      .every(g => g.disabled),
  }))()`);
  ok(`locked state: ${JSON.stringify(lockedState)}`);
  if (lockedState.handles !== 0) throw new Error("locked lane still shows trim handles");
  if (!lockedState.splitDisabled) throw new Error("split enabled on a fully-locked timeline");
  if (!lockedState.insDisabled) throw new Error("insert-space enabled with no editable lane");
  if (!lockedState.gapDisabled) throw new Error("gap buttons enabled on locked lane");
  ok("locked lane: no handles, split/insert/gap all disabled");
  await evalJs(`document.querySelector('[aria-label="Unlock V1"]').click()`);
  await sleep(1000);

  step("Escape cancels a trim drag — no commit");
  const d10 = await detail(pid, tid);
  const escClip = d10.clips.slice()
    .sort((a, b) => a.start_frames - b.start_frames)[0];
  await evalJs(`(() => {
    const h = [...document.querySelectorAll('[data-testid="lane-clip"]')][0]
      .querySelector('[data-testid="trim-handle"]');
    const r = h.getBoundingClientRect();
    h.dispatchEvent(new PointerEvent("pointerdown",
      { bubbles: true, clientX: r.left + 1, clientY: r.top + 5, pointerId: 13 }));
    window.dispatchEvent(new PointerEvent("pointermove",
      { bubbles: true, clientX: r.left - 30, clientY: r.top + 5, pointerId: 13 }));
    window.dispatchEvent(new KeyboardEvent("keydown",
      { bubbles: true, key: "Escape" }));
    window.dispatchEvent(new PointerEvent("pointerup",
      { bubbles: true, clientX: r.left - 30, clientY: r.top + 5, pointerId: 13 }));
    return true; })()`);
  await sleep(1200);
  const d11 = await detail(pid, tid);
  const escAfter = d11.clips.find((c) => c.id === escClip.id);
  if (!escAfter || escAfter.out_frame !== escClip.out_frame) {
    throw new Error(`Escape-cancelled trim committed (${escClip.out_frame} → ${escAfter && escAfter.out_frame})`);
  }
  ok(`out_frame unchanged after Escape (${escClip.out_frame})`);

  step("Export eligibility: leading gap → non-v1 badge → close → v1 badge");
  const badgeGap = await evalJs(
    `document.querySelector('[data-testid="export-capability"]')?.textContent || ""`);
  ok(`gapped badge: "${badgeGap}"`);
  if (badgeGap.includes("Exports as one video")) {
    throw new Error("leading-gap layout wrongly claims v1 export");
  }
  // Close the leading gap via its indicator → lane contiguous from 0.
  await evalJs(`document.querySelector('[data-testid="lane-gap"]').click()`);
  await sleep(1500);
  const d12 = await detail(pid, tid);
  if (d12.clips[0].start_frames !== 0) {
    throw new Error("leading-gap close didn't pull the clip to 0");
  }
  const badgeOk = await evalJs(
    `document.querySelector('[data-testid="export-capability"]')?.textContent || ""`);
  ok(`contiguous badge: "${badgeOk}"`);
  if (!badgeOk.includes("Exports as one video")) {
    throw new Error(`expected v1 badge after close, got "${badgeOk}"`);
  }

  step("Restart persistence: reload WebView → committed state renders");
  await send("Page.reload");
  await sleep(4000);
  await navToTimeline();
  const d13 = await detail(pid, tid);
  const domCount = await evalJs(
    `document.querySelectorAll('[data-testid="lane-clip"]').length`);
  const domStart = await evalJs(`(() => {
    const c = document.querySelector('[data-testid="lane-clip"]');
    return c ? c.getAttribute("aria-label") : null; })()`);
  ok(`post-reload: ${domCount} clips in DOM vs ${d13.clips.length} via API; first clip "${domStart}"`);
  if (domCount !== d13.clips.length) {
    throw new Error("post-reload DOM clip count differs from API");
  }
  if (!domStart || !domStart.includes(`frame ${d13.clips[0].start_frames}`)) {
    throw new Error("post-reload clip position differs from API");
  }

  step("Final state dump + EDL v2 sanity (gaps/tracks honest)");
  const edl = await api("GET", `/api/projects/${pid}/timelines/${tid}/edl?version=2`);
  ok(`edl v2: ${JSON.stringify(edl).slice(0, 240)}`);
  fs.writeFileSync(`${OUT}/final-timeline.json`,
    JSON.stringify(await detail(pid, tid), null, 2));
  fs.writeFileSync(`${OUT}/final-edl-v2.json`, JSON.stringify(edl, null, 2));
  await shot("final");
}

async function main() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const page = list.find((t) => t.type === "page");
  if (!page) throw new Error("no debuggable page — is the app running with --remote-debugging-port?");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && _pend.has(m.id)) {
      const { res, rej } = _pend.get(m.id); _pend.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    } else if (m.method && _events.has(m.method)) {
      for (const fn of _events.get(m.method)) fn(m.params);
    }
  };
  await new Promise((r) => (ws.onopen = r));
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Network.enable");

  // Learn backend port+token by sniffing the app's own API traffic —
  // the same credentials the installed UI uses.
  let creds = null;
  const apiRe = /127\.0\.0\.1:(\d+)\/api\//;
  const tokRe = /[?&]token=([^&]+)/;
  onEvent("Network.requestWillBeSent", (p) => {
    const m = p.request.url.match(apiRe);
    if (m) {
      creds = creds || {};
      creds.port = m[1];
      const tok = p.request.url.match(tokRe);
      if (tok) creds.token = decodeURIComponent(tok[1]);
      if (p.request.headers && p.request.headers["X-Atomic-Token"]) {
        creds.token = creds.token || p.request.headers["X-Atomic-Token"];
      }
    }
  });
  onEvent("Network.webSocketCreated", (p) => {
    const m = p.url.match(apiRe);
    if (m) {
      creds = creds || {};
      creds.port = creds.port || m[1];
      const tok = p.url.match(tokRe);
      if (tok) creds.token = decodeURIComponent(tok[1]);
    }
  });
  // Nudge the app to talk to its backend, then harvest credentials.
  await send("Page.reload");
  const t0 = Date.now();
  while ((!creds || !creds.port || !creds.token) && Date.now() - t0 < 20000) {
    await sleep(400);
  }
  if (!creds || !creds.token) {
    // WebSocket URLs carry ?token=; if only the port was seen, try the
    // X-Atomic-Token header path by replaying a project list fetch.
    throw new Error(`could not learn backend credentials: ${JSON.stringify(creds)}`);
  }
  API = `http://127.0.0.1:${creds.port}`;
  TOKEN = creds.token;
  ok(`learned installed backend ${API}`);
  await sleep(2500); // let the reload settle before driving the DOM

  const { pid, tid } = await seed();
  await scenario_n1c(pid, tid);
  step("N1-C INSTALLED-APP ACCEPTANCE: PASS");
  fs.writeFileSync(`${OUT}/n1c-acceptance.log`, LOG.join("\n") + "\n");
  ws.close();
}

main().catch((e) => {
  LOG.push(`FAIL: ${e.message}`);
  try { fs.writeFileSync(`${OUT}/n1c-acceptance.log`, LOG.join("\n") + "\n"); } catch {}
  console.error(e);
  process.exit(1);
});
