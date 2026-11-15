// N1-E performance benchmark — drives the REAL backend API.
// Usage: node scripts/n1e-bench.cjs <base-url> <token>
// Measures detail fetch, EDL v2 serialization, and command-commit
// latency at 10/100/500/1000 clips on a live project.
const [BASE = "http://127.0.0.1:8788", TOK = "dev"] =
  process.argv.slice(2);
const H = { "X-Atomic-Token": TOK, "content-type": "application/json" };
const MEDIA = process.env.N1E_MEDIA
  || "C:\\Projects\\atomic-ai-studio\\AtomicAIStudio\\docs\\n1d-evidence\\media";

async function api(method, path, body) {
  const r = await fetch(BASE + path, {
    method, headers: H, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) throw new Error(
    `${method} ${path} → ${r.status} ${(await r.text()).slice(0, 300)}`);
  return r.json();
}
const ms = () => Number(process.hrtime.bigint() / 1000n) / 1000;
const timed = async (fn) => {
  const t = ms(); const r = await fn(); return [ms() - t, r];
};

async function commit(pid, type, payload, key) {
  const prj = await api("GET", `/api/projects/${pid}`);
  return api("POST", `/api/projects/${pid}/commands/commit`, {
    command_type: type, payload, scope: "project",
    expected_revision: prj.revision,
    idempotency_key: key || `bench-${Math.random().toString(36).slice(2)}`,
  });
}
async function waitJob(pid, id) {
  for (let i = 0; i < 240; i++) {
    const jobs = await api("GET", `/api/projects/${pid}/jobs`);
    const j = jobs.find((x) => x.id === id);
    if (j && j.status === "succeeded") return j;
    if (j && (j.status === "failed" || j.status === "cancelled"))
      throw new Error(`job ${id} ${j.status}`);
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`job ${id} timeout`);
}

(async () => {
  const sizes = [10, 100, 500, 1000];
  const out = [];
  for (const n of sizes) {
    const p = await api("POST", "/api/projects", {
      name: `n1e-bench-${n}-${Date.now()}` });
    const imp = await commit(p.id, "asset.import",
      { source_path: `${MEDIA}\\base.mp4`, kind: "video" });
    await waitJob(p.id, imp.job_id);
    const list = await api("GET",
      `/api/projects/${p.id}/assets?limit=50&kind=video`);
    const asset = list.items[0];
    if (!asset) throw new Error("no video asset after import");
    await commit(p.id, "timeline.create",
      { name: `bench ${n}`, fps_num: 24, fps_den: 1 });
    const tls = await api("GET", `/api/projects/${p.id}/timelines`);
    const tl = tls.find((t) => t.name === `bench ${n}`) || tls[0];
    if (!tl) throw new Error("no timeline after create");
    const d0 = await api("GET",
      `/api/projects/${p.id}/timelines/${tl.id}`);
    const v1 = d0.tracks.find((t) => t.kind === "video");

    const tFill = ms();
    for (let i = 0; i < n; i++) {
      await commit(p.id, "timeline.clip.place", {
        timeline_id: tl.id, track_id: v1.id,
        asset_id: asset.id, start_frames: i * 48,
        allow_duplicate: true });
    }
    const fillMs = ms() - tFill;

    const [dDetail, detail] = await timed(
      () => api("GET", `/api/projects/${p.id}/timelines/${tl.id}`));
    const [dEdl] = await timed(() => api("GET",
      `/api/projects/${p.id}/timelines/${tl.id}/edl?version=2`));
    let dEdl1 = NaN;
    try {
      [dEdl1] = await timed(() => api("GET",
        `/api/projects/${p.id}/timelines/${tl.id}/edl?version=1`));
    } catch { dEdl1 = -1; } // nonrepresentable layout → documented -1
    const [dCommit] = await timed(() => commit(p.id, "timeline.update",
      { timeline_id: tl.id, name: `bench ${n}` }));
    const [dUndo] = await timed(() => commit(p.id, "timeline.undo",
      { timeline_id: tl.id }));

    const row = {
      clips: detail.clips.length,
      fill_ms_per_clip: +(fillMs / n).toFixed(1),
      detail_ms: +dDetail.toFixed(1),
      edl_v2_ms: +dEdl.toFixed(1),
      edl_v1_ms: +dEdl1.toFixed(1),
      commit_ms: +dCommit.toFixed(1),
      undo_ms: +dUndo.toFixed(1),
    };
    out.push(row);
    console.log(`n=${n} →`, row);
  }
  console.log(JSON.stringify(out, null, 2));
})().catch((e) => { console.error("FAIL:", e.message || e); process.exit(1); });
