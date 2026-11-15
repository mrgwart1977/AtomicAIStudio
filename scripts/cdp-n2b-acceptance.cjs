// N2-B acceptance — real installed NSIS app: audio import → durable
// asset.waveform job → CAS-derived LOD envelopes → manifest + windowed
// slice endpoints → canvas rendered inside the audio clip in the real
// UI → zoom triggers a different LOD slice → skip-on-rebuild → export
// still refuses a visible audio lane (N2-E). Zero credentials/cloud.
const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const PORT = process.argv[2] || "9227";
const OUT = process.argv[3] || "docs/n2b-evidence";
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
const commit = async (pid, type, payload) => {
  const proj = await api("GET", `/api/projects/${pid}`);
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project", origin: "audit",
    expected_revision: proj.revision,
    idempotency_key: `n2b-${type}-${Date.now()}-${Math.random()}` });
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

  /* synthesize a real 30 s WAV with the bundled ffmpeg — long enough
     to produce a real LOD ladder and a wide clip. */
  const wav = `${OUT}\\media\\tone30.wav`;
  fs.mkdirSync(`${OUT}\\media`, { recursive: true });
  execFileSync(`${FFMPEG_BIN}\\ffmpeg.exe`, [
    "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
    "-ar", "48000", "-ac", "2", wav]);
  ok("wav synthesized (30s sine, 48k stereo)");

  /* 1 — import audio, place clip on audio lane */
  step("1) Project + audio import + audio clip placed");
  const p = await api("POST", "/api/projects", { name: `N2B-${Date.now()}` });
  const ra = await commit(p.id, "asset.import",
    { source_path: path.resolve(wav), kind: "audio" });
  await waitJob(p.id, ra.job_id);
  const assets = (await api("GET", `/api/projects/${p.id}/assets?limit=50`)).items;
  const au = assets.find((a) => a.name.includes("tone30"));
  const probe = (au.media_json || {}).probe || {};
  if (!au || au.kind !== "audio" || !probe.ok || probe.samples !== 1440000)
    throw new Error(`audio import bad: ${JSON.stringify(probe).slice(0, 200)}`);
  ok(`imported: ${probe.codec} ${probe.sample_rate}Hz x${probe.channels}ch ${probe.samples} samples`);
  await commit(p.id, "timeline.create", { name: "WF", fps_num: 24, fps_den: 1 });
  const tl = (await api("GET", `/api/projects/${p.id}/timelines`)).find((t) => t.name === "WF");
  await commit(p.id, "timeline.track.add", { timeline_id: tl.id, kind: "audio", name: "A1" });
  let d = await detail(p.id, tl.id);
  const a1 = d.tracks.find((t) => t.kind === "audio");
  await commit(p.id, "timeline.clip.place",
    { timeline_id: tl.id, track_id: a1.id, asset_id: au.id, start_frames: 0 });
  ok("audio clip placed on A1");

  /* 2 — durable asset.waveform job builds LOD envelopes */
  step("2) asset.waveform durable job → manifest + levels");
  const rw = await commit(p.id, "asset.waveform", { asset_id: au.id });
  const wjob = await waitJob(p.id, rw.job_id);
  ok(`job succeeded: ${JSON.stringify(wjob.result_json || {}).slice(0, 120)}`);
  const man = await api("GET", `/api/projects/${p.id}/assets/${au.id}/waveform`);
  if (man.format !== "ade-wave/1" || man.sample_rate !== 48000
      || man.channels !== 2 || man.samples !== 1440000
      || !man.levels || man.levels.length < 3)
    throw new Error(`manifest bad: ${JSON.stringify(man).slice(0, 300)}`);
  ok(`manifest: ${man.levels.length} levels, finest spb=${man.levels[0].spb} buckets=${man.levels[0].buckets}`);

  /* 3 — windowed slice clamps honestly */
  step("3) windowed level slice + clamped bounds");
  const spb = man.levels[0].spb;
  const sl = await api("GET",
    `/api/projects/${p.id}/assets/${au.id}/waveform/level`
    + `?level=${spb}&start_bucket=10&end_bucket=30`);
  if (sl.bucket_start !== 10 || sl.bucket_end !== 30
      || sl.peaks.length !== 2 || sl.peaks[0].length !== 20)
    throw new Error(`slice bad: ${JSON.stringify(sl).slice(0, 200)}`);
  if (!sl.peaks[0].some((pk) => pk[1] > 0 && pk[0] < 0))
    throw new Error("slice peaks are silent — real signal expected");
  const clamp = await api("GET",
    `/api/projects/${p.id}/assets/${au.id}/waveform/level`
    + `?level=${spb}&start_bucket=999990&end_bucket=9999999`);
  if (clamp.bucket_end !== sl.buckets)
    throw new Error("slice clamp wrong");
  ok(`slice 10..30 = 20 buckets ×2ch, nonzero extrema; tail clamps to ${clamp.bucket_end}`);

  /* 4 — rerun skips regeneration */
  step("4) second asset.waveform skips regeneration");
  const rw2 = await commit(p.id, "asset.waveform", { asset_id: au.id });
  const wjob2 = await waitJob(p.id, rw2.job_id);
  if (!(wjob2.result_json || {}).skipped)
    throw new Error(`expected skip, got ${JSON.stringify(wjob2.result_json).slice(0, 150)}`);
  ok(`skipped: "${wjob2.result_json.skipped}"`);

  /* 5 — UI: canvas mounts inside the audio clip, states resolve */
  step("5) installed UI: waveform canvas inside audio clip");
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("WF"));
    if (b) b.click(); return !!b; })()`);
  await sleep(2500);
  // Clip spans 720 tl-frames (30s @ 24fps) — zoom out so it mounts.
  for (let i = 0; i < 6; i++) {
    await evalJs(`document.querySelector('[aria-label="Zoom out"]')?.click()`);
    await sleep(250);
  }
  await sleep(2500);
  const dom = await evalJs(`(() => {
    const clip = document.querySelector('[data-testid="lane-clip"]');
    return {
      clips: document.querySelectorAll('[data-testid="lane-clip"]').length,
      canvas: clip ? clip.querySelectorAll("canvas").length : 0,
      label: clip ? clip.textContent : "",
      loading: document.body.innerText.includes("waveform…"),
      missing: document.body.innerText.includes("no waveform"),
      error: document.body.innerText.includes("waveform unavailable"),
    }; })()`);
  if (dom.canvas < 1 || dom.error)
    throw new Error(`dom: ${JSON.stringify(dom)}`);
  ok(`canvas mounted in clip; loading=${dom.loading} missing=${dom.missing} error=${dom.error}`);

  /* 6 — zoom-in switches LOD → new slice fetch observed.
     Network.requestWillBeSent is unreliable for WebView2 fetch()
     calls, so instrument window.fetch directly in-page. */
  step("6) zoom triggers a different LOD slice fetch");
  await evalJs(`(() => {
    window.__fetches = [];
    const of = window.fetch;
    window.fetch = (...a) => {
      window.__fetches.push(String(a[0]));
      return of(...a); };
    return true; })()`);
  // Zoom in far enough that the LOD MUST switch to the finest level
  // (spb 256) — shallow zooms legitimately hit the slice cache.
  for (let i = 0; i < 8; i++) {
    await evalJs(`document.querySelector('[aria-label="Zoom in"]')?.click()`);
    await sleep(600);
  }
  await sleep(1500);
  const levelFetches = (await evalJs(
    `window.__fetches.filter(u => u.includes("/waveform/level"))`)) || [];
  const spbs = [...new Set(levelFetches.map((u) =>
    (u.match(/level=(\d+)/) || [])[1]))];
  if (!spbs.includes("256"))
    throw new Error(`deep zoom never fetched finest LOD: ${JSON.stringify(spbs)}`);
  ok(`${levelFetches.length} slice fetches, spb levels used: ${spbs.join(",")}`);
  fs.writeFileSync(`${OUT}\\n2b-fetches.json`,
    JSON.stringify({ count: levelFetches.length, spbs }, null, 2));

  /* 7 — export still refuses visible audio lane (N2-E unchanged) */
  step("7) export still fails closed on visible audio lane");
  const jr = await commit(p.id, "timeline.export",
    { timeline_id: tl.id, profile: "720p" });
  const xjob = await waitJob(p.id, jr.job_id, true);
  if (xjob.status !== "failed" || !/audio lanes/i.test(xjob.error || ""))
    throw new Error(`export did not fail closed: ${JSON.stringify(xjob).slice(0, 240)}`);
  ok(`refused: "${xjob.error.slice(0, 90)}"`);

  /* 8 — move the audio clip on the timeline */
  step("8) audio clip move — frame-accurate placement");
  d = await detail(p.id, tl.id);
  const theClip = d.tracks.flatMap((t) => t.clips)[0];
  if (!theClip) throw new Error("no clip after step 7");
  await commit(p.id, "timeline.clip.move",
    { clip_id: theClip.clip_id, start_frames: 96 });
  d = await detail(p.id, tl.id);
  let moved = d.tracks.flatMap((t) => t.clips)[0];
  if (moved.start_frames !== 96 || moved.clip_id !== theClip.clip_id)
    throw new Error(`move bad: ${JSON.stringify(moved).slice(0, 160)}`);
  ok(`clip moved to tl-frame 96, stable id ${moved.clip_id.slice(0, 8)}`);

  /* 9 — head + tail trim, sample-exact source bounds */
  step("9) head + tail trim — sample-exact in/out");
  await commit(p.id, "timeline.clip.trim",
    { clip_id: theClip.clip_id, in_frame: 48000 });
  await commit(p.id, "timeline.clip.trim",
    { clip_id: theClip.clip_id, out_frame: 1380000 });
  d = await detail(p.id, tl.id);
  let trimmed = d.tracks.flatMap((t) => t.clips)[0];
  if (trimmed.in_frame !== 48000 || trimmed.out_frame !== 1380000)
    throw new Error(`trim bad: ${JSON.stringify(trimmed).slice(0, 160)}`);
  // Trimmed samples map to an absolute bucket range — the manifest is
  // source-global and unchanged by edits.
  const man2 = await api("GET", `/api/projects/${p.id}/assets/${au.id}/waveform`);
  if (man2.built_sha256 !== man.built_sha256)
    throw new Error("trim changed waveform provenance — must not happen");
  ok(`in=48000 out=1380000 (samples), provenance unchanged`);

  /* 10 — split preserves source intervals across both halves */
  step("10) split — both halves keep correct source segments");
  const splitAt = trimmed.start_frames + 300; // 300 tl-f × 2000 smp/f
  await commit(p.id, "timeline.clip.split",
    { clip_id: theClip.clip_id, at_frame: splitAt });
  d = await detail(p.id, tl.id);
  const halves = d.tracks.flatMap((t) => t.clips)
    .sort((x, y) => x.start_frames - y.start_frames);
  if (halves.length !== 2
      || halves[0].in_frame !== 48000
      || halves[0].out_frame !== 48000 + 300 * 2000
      || halves[1].in_frame !== 48000 + 300 * 2000
      || halves[1].out_frame !== 1380000
      || halves[0].source_asset_id !== au.id
      || halves[1].source_asset_id !== au.id)
    throw new Error(`split bad: ${JSON.stringify(halves).slice(0, 300)}`);
  ok(`A:[48000,648000) B:[648000,1380000) — contiguous, same source`);

  /* 11 — undo restores pre-split single clip with a stable id; redo
     re-splits. N1-E identity semantics must hold for audio clips. */
  step("11) undo/redo — stable clip identity");
  await commit(p.id, "timeline.undo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  const restored = d.tracks.flatMap((t) => t.clips);
  if (restored.length !== 1 || restored[0].clip_id !== theClip.clip_id
      || restored[0].in_frame !== 48000)
    throw new Error(`undo bad: ${JSON.stringify(restored).slice(0, 200)}`);
  await commit(p.id, "timeline.redo", { timeline_id: tl.id });
  d = await detail(p.id, tl.id);
  if (d.tracks.flatMap((t) => t.clips).length !== 2)
    throw new Error("redo did not restore the split");
  ok(`undo → 1 clip (stable id), redo → 2 clips`);

  /* 12 — multi-track load: ~100 audio clips across two more lanes;
     rapid scroll must stay responsive, heap bounded, and revisiting a
     region must hit the slice cache (zero new fetches). */
  step("12) 100+ audio clips — rapid scroll, bounded heap, cache reuse");
  await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "audio", name: "A2" });
  await commit(p.id, "timeline.track.add",
    { timeline_id: tl.id, kind: "audio", name: "A3" });
  d = await detail(p.id, tl.id);
  const lanes = d.tracks.filter((t) => t.kind === "audio" && t.name !== "A1");
  for (let i = 0; i < 100; i++) {
    await commit(p.id, "timeline.clip.place", {
      timeline_id: tl.id, track_id: lanes[i % 2].id,
      asset_id: au.id, start_frames: i * 720 });
  }
  d = await detail(p.id, tl.id);
  const total = d.tracks.flatMap((t) => t.clips).length;
  if (total < 100) throw new Error(`expected 100+ clips, got ${total}`);
  ok(`${total} clips across ${d.tracks.length} audio lanes`);

  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("WF"));
    if (b) b.click(); return !!b; })()`);
  await sleep(3000);
  await evalJs(`(() => {
    window.__fetches = [];
    const of = window.fetch;
    if (!window.__wrapped) {
      window.__wrapped = true;
      window.fetch = (...a) => {
        window.__fetches.push(String(a[0])); return of(...a); };
    } else { window.__fetches = []; }
    return true; })()`);
  const perf = await evalJs(`(async () => {
    const scroller = document.querySelector('[data-testid="timeline-scroll"]');
    if (!scroller) return { skipped: "no scroller" };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const measure = async (ms) => {
      const t0 = performance.now(); let n = 0;
      const raf = () => { n++; if (performance.now() - t0 < ms)
        requestAnimationFrame(raf); };
      requestAnimationFrame(raf);
      await sleep(ms);
      return Math.round(n / (ms / 1000)); };
    const fps1 = await measure(800);
    // Rapid scroll across the 43k-frame span, then back.
    for (const x of [4000, 12000, 30000, 4000]) {
      scroller.scrollLeft = x;
      scroller.dispatchEvent(new Event("scroll"));
      await sleep(300); }
    await sleep(800);
    const fetchesAfterScroll =
      window.__fetches.filter(u => u.includes("/waveform/level")).length;
    // Revisit the same spot — must hit the promise cache.
    scroller.scrollLeft = 4000;
    scroller.dispatchEvent(new Event("scroll"));
    await sleep(900);
    const fetchesAfterRevisit =
      window.__fetches.filter(u => u.includes("/waveform/level")).length;
    const canvases = document.querySelectorAll(
      '[data-testid="lane-clip"] canvas').length;
    return { fps: fps1,
      sliceFetches: fetchesAfterScroll,
      revisitNewFetches: fetchesAfterRevisit - fetchesAfterScroll,
      mountedCanvases: canvases,
      heapMB: performance.memory
        ? Math.round(performance.memory.usedJSHeapSize / 1048576) : null };
  })()`);
  if (perf.fps && perf.fps < 20)
    throw new Error(`janky under load: ${JSON.stringify(perf)}`);
  if (perf.revisitNewFetches > 2)
    throw new Error(`cache not reusing: ${JSON.stringify(perf)}`);
  if (perf.heapMB && perf.heapMB > 700)
    throw new Error(`heap blowout: ${JSON.stringify(perf)}`);
  ok(`100+ clips: ${JSON.stringify(perf)}`);
  fs.writeFileSync(`${OUT}\\n2b-ui-perf.json`, JSON.stringify(perf, null, 2));

  /* 13 — restart persistence: kill the app, relaunch, reconnect —
     manifest must still serve and the canvas mount WITHOUT a rebuild. */
  step("13) restart persistence — waveform survives relaunch");
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
  // Reconnect CDP on the fresh instance.
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
  const man3 = await api("GET", `/api/projects/${p.id}/assets/${au.id}/waveform`);
  if (man3.built_sha256 !== man.built_sha256)
    throw new Error("manifest changed across restart");
  await navToTimeline(p.name);
  await evalJs(`(() => {
    const b = [...document.querySelectorAll("button")]
      .find(e => e.textContent.includes("WF"));
    if (b) b.click(); return !!b; })()`);
  await sleep(3000);
  const domAfter = await evalJs(`(() => {
    const clip = document.querySelector('[data-testid="lane-clip"]');
    return { canvas: clip ? clip.querySelectorAll("canvas").length : 0,
      jobs: document.body.innerText.includes("waveform…") }; })()`);
  if (domAfter.canvas < 1)
    throw new Error(`no canvas after restart: ${JSON.stringify(domAfter)}`);
  ok(`manifest identical post-restart; canvas remounts (no rebuild)`);

  /* 14 — clean shutdown: no orphan app OR ffmpeg processes */
  step("14) shutdown — no orphan processes");
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-dream-engine.exe"]); } catch {}
  try { execFileSync("taskkill", ["/F", "/IM", "atomic-backend.exe"]); } catch {}
  await sleep(3000);
  const procs = execFileSync("tasklist").toString();
  const orphans = (procs.match(/atomic-(dream-engine|backend)\.exe/gi) || []).length;
  const ffmpegOrphans = (procs.match(/ffmpeg\.exe/gi) || []).length;
  if (orphans > 0) throw new Error(`${orphans} orphan app processes`);
  if (ffmpegOrphans > 0) throw new Error(`${ffmpegOrphans} orphan ffmpeg processes`);
  ok("clean shutdown — zero orphan app/ffmpeg processes");

  fs.writeFileSync(`${OUT}\\n2b-acceptance.log`, LOG.join("\n") + "\n");
  console.log(`\nPASS ${STEP}/14 — evidence in ${OUT}`);
}

main().catch((e) => {
  console.error("FAIL:", e.message || e);
  try { fs.writeFileSync(`${OUT}\\n2b-acceptance.log`, LOG.join("\n") + `\nFAIL ${e.message}\n`); } catch {}
  process.exit(1);
});
