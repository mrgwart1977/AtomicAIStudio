/**
 * A5 — dialogue segments for a screenplay scene or storyboard shot:
 * speaker-bound lines with delivery direction, voice-profile
 * resolution, deterministic mock audio through the durable job
 * system, in-app playback, and placement on an audio timeline lane.
 * All mutations are Unified Creative Commands — generation is
 * preview → approve → commit with the backend's pins, never a
 * direct call. The mock adapter is labeled explicitly — simulated
 * audio, never presented as speech synthesis.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, fetchAssetObjectUrl } from "../../api/client";
import { commitCommand, previewCommand } from "../../api/commands";
import { useApp } from "../../stores/appStore";
import type {
  DialogueSegment, Entity, Job, Timeline, TimelineDetail,
} from "../../api/types";

const SEL = "w-full rounded border border-line bg-ink px-2 py-1 text-xs";
const INP = SEL;
const LBL = "text-[10px] uppercase tracking-wide text-faint";
const BTN = "rounded border border-line px-2 py-0.5 text-[10px] text-muted hover:text-teal disabled:opacity-40";

const LANGS = ["en", "es", "fr", "de", "it", "pt", "ja", "ko", "zh"];
const EMOTIONS = ["", "neutral", "happy", "sad", "angry", "fearful",
  "tender", "resolute", "weary"];

function jobFor(jobs: Record<string, Job>, jobId: string | null): Job | null {
  return jobId ? jobs[jobId] ?? null : null;
}

/** Voice-resolution badge — the segment never duplicates the
 * character's voice identity, only reports what it resolved to. */
function VoiceBadge({ seg }: { seg: DialogueSegment }) {
  const v = seg.resolved_voice as Record<string, unknown> | null;
  if (!seg.character_id) {
    return <span className="text-[10px] text-faint">no speaker</span>;
  }
  if (!v || v.missing) {
    return (
      <span className="text-[10px] text-amber" role="status">
        ⚠ no voice profile
      </span>
    );
  }
  const bits = [v.style, v.delivery, v.emotion]
    .filter((x): x is string => typeof x === "string" && !!x);
  return (
    <span className="text-[10px] text-teal" title={
      bits.length ? bits.join(" · ") : "character voice profile"}>
      voice{bits.length ? ` · ${bits.join(" · ")}` : ""}
    </span>
  );
}

/** Audio preview — the same authenticated fetch the asset page uses,
 * so playback is the real CAS bytes, not a re-render. */
function AudioPreview({ projectId, assetId }: {
  projectId: string; assetId: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    let dead = false; let obj: string | null = null;
    void fetchAssetObjectUrl(projectId, assetId)
      .then((u) => { if (dead) URL.revokeObjectURL(u); else { obj = u; setUrl(u); } })
      .catch(() => { if (!dead) setErr(true); });
    return () => { dead = true; if (obj) URL.revokeObjectURL(obj); };
  }, [projectId, assetId]);
  if (err) return <span className="text-[10px] text-faint">preview unavailable</span>;
  if (!url) return <span className="text-[10px] text-faint">loading…</span>;
  return (
    <audio controls preload="none" src={url}
      aria-label="Dialogue audio preview"
      className="h-6 w-44" />
  );
}

export function DialogueSection({ projectId, spSceneId, shotId, sceneId,
  createSceneId, run, onChanged }: {
  projectId: string;
  /** Exactly one of these is the list anchor — the same field the
   * create payload sets, so a new segment always lists here. */
  spSceneId?: string;
  shotId?: string;
  sceneId?: string;
  /** Extra denormalized link recorded at create (e.g. the shot's
   * parent scene) — never part of the list filter. */
  createSceneId?: string;
  run: (t: string, p: Record<string, unknown>) => Promise<boolean>;
  onChanged?: () => void;
}) {
  const [items, setItems] = useState<DialogueSegment[]>([]);
  const [characters, setCharacters] = useState<Entity[]>([]);
  const [timelines, setTimelines] = useState<Timeline[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [placeSel, setPlaceSel] = useState<Record<string, string>>({});
  const [audioLanes, setAudioLanes] = useState<
    Record<string, { id: string; name: string }[]>>({});
  const [preview, setPreview] = useState<{
    segmentId: string;
    text: string; duration_s: number; sampleRate: number;
    missingVoice: boolean;
    warnings: string[];
    expected_dialogue?: Record<string, unknown>;
  } | null>(null);
  const jobs = useApp((s) => s.jobs);

  const flt = new URLSearchParams();
  if (spSceneId) flt.set("sp_scene_id", spSceneId);
  if (shotId) flt.set("shot_id", shotId);
  if (sceneId) flt.set("scene_id", sceneId);
  const listUrl = `/api/projects/${projectId}/dialogue?${flt}`;

  const load = useCallback(async () => {
    try {
      setItems(await apiFetch<DialogueSegment[]>(listUrl));
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listUrl]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    void apiFetch<Entity[]>(`/api/projects/${projectId}/character`)
      .then(setCharacters).catch(() => setCharacters([]));
    void apiFetch<Timeline[]>(`/api/projects/${projectId}/timelines`)
      .then(setTimelines).catch(() => setTimelines([]));
  }, [projectId]);

  // A settled job lands its asset via apply_command — reload once.
  const settledRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    let newly = 0;
    for (const d of items) {
      const j = jobFor(jobs, d.generate_job_id);
      if (j && ["succeeded", "failed", "cancelled"].includes(j.status)
          && !settledRef.current.has(j.id)) {
        settledRef.current.add(j.id);
        newly += 1;
      }
    }
    if (newly > 0) void load();
  }, [jobs, items, load]);

  const dget = (d: DialogueSegment, k: string, fallback: string) =>
    draft[d.id + ":" + k] ?? fallback;
  const dset = (d: DialogueSegment, k: string, v: string) =>
    setDraft((s) => ({ ...s, [d.id + ":" + k]: v }));
  const dirty = (d: DialogueSegment) =>
    dget(d, "text", d.text) !== d.text
    || dget(d, "direction", d.direction) !== d.direction
    || dget(d, "character", d.character_id ?? "") !== (d.character_id ?? "")
    || dget(d, "language", d.language) !== d.language
    || dget(d, "emotion", String(d.voice_json?.emotion ?? ""))
      !== String(d.voice_json?.emotion ?? "")
    || dget(d, "style", String(d.voice_json?.style ?? ""))
      !== String(d.voice_json?.style ?? "")
    || dget(d, "seed", String(d.seed ?? "")) !== String(d.seed ?? "");

  async function act(t: string, p: Record<string, unknown>) {
    setErr(null);
    if (!(await run(t, p))) {
      setErr("Dialogue change failed — see the board message.");
      return;
    }
    await load();
    onChanged?.();
  }

  async function add() {
    const payload: Record<string, unknown> = {};
    if (spSceneId) payload.sp_scene_id = spSceneId;
    if (shotId) payload.shot_id = shotId;
    if (sceneId) payload.scene_id = sceneId;
    if (createSceneId && !payload.scene_id) {
      payload.scene_id = createSceneId;
    }
    await act("dialogue.create", payload);
  }

  async function save(d: DialogueSegment) {
    const seed = dget(d, "seed", String(d.seed ?? ""));
    await act("dialogue.update", {
      segment_id: d.id,
      text: dget(d, "text", d.text),
      direction: dget(d, "direction", d.direction),
      character_id: dget(d, "character", d.character_id ?? "") || null,
      language: dget(d, "language", d.language),
      seed: seed.trim() ? parseInt(seed, 10) : null,
      voice: {
        emotion: dget(d, "emotion",
          String(d.voice_json?.emotion ?? "")) || null,
        style: dget(d, "style",
          String(d.voice_json?.style ?? "")) || null,
      },
    });
  }

  async function doGenerate(d: DialogueSegment) {
    setErr(null);
    if (dirty(d)) await save(d);
    try {
      const prop = await previewCommand(
        projectId, "dialogue.generate", { segment_id: d.id });
      const ra = prop.resolved_audio;
      setPreview({
        segmentId: d.id,
        text: ra?.text ?? d.text,
        duration_s: ra?.duration_s ?? 0,
        sampleRate: ra?.sample_rate ?? 48000,
        missingVoice: !!((ra?.voice as Record<string, unknown> | null)
          ?.missing),
        warnings: prop.warnings ?? [],
        expected_dialogue: prop.resolved_dialogue ?? undefined,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function approve() {
    if (!preview) return;
    const pv = preview;
    setPreview(null);
    const rev = useApp.getState().currentProject()?.revision;
    if (rev === undefined) { setErr("Project revision unavailable."); return; }
    try {
      await commitCommand(projectId, rev, "dialogue.generate",
        { segment_id: pv.segmentId,
          ...(pv.expected_dialogue
              ? { expected_dialogue: pv.expected_dialogue } : {}) },
        "screenplay");
      await load();
      onChanged?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  // Lane options for placement — fetched lazily per timeline.
  async function lanesFor(tlId: string) {
    if (audioLanes[tlId]) return;
    try {
      const det = await apiFetch<TimelineDetail>(
        `/api/projects/${projectId}/timelines/${tlId}`);
      setAudioLanes((m) => ({
        ...m, [tlId]: det.tracks
          .filter((t) => t.kind === "audio")
          .map((t) => ({ id: t.id, name: t.name })),
      }));
    } catch { /* surfaced on place */ }
  }

  async function place(d: DialogueSegment) {
    const sel = placeSel[d.id] ?? "";
    const [tlId, trackId] = sel.split("|");
    if (!tlId || !trackId || !d.result_asset_id) return;
    await act("dialogue.place", {
      segment_id: d.id, timeline_id: tlId, track_id: trackId,
    });
  }

  return (
    <section className="mt-4 rounded-md border border-line bg-ink/40 p-3"
      aria-label="Dialogue segments">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold text-fg">Dialogue</h4>
        <button type="button" className={BTN}
          aria-label="Add dialogue segment"
          onClick={() => void add()}>
          + Dialogue
        </button>
      </div>
      <p className="mt-0.5 text-[10px] text-muted">
        Speaker lines for this {shotId ? "shot" : "scene"}. Generation is
        deterministic simulated audio — labeled, no speech synthesis.
      </p>
      {err && <p className="mt-1 text-[11px] text-danger" role="alert">{err}</p>}

      {preview && (
        <div className="mt-2 rounded border border-teal/40 bg-panel p-2"
          role="dialog" aria-label="Dialogue generation preview">
          <p className="text-[10px] uppercase tracking-wide text-teal">
            Simulated audio — deterministic mock
          </p>
          <p className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap text-[11px] text-fg">
            “{preview.text}”
          </p>
          <p className="mt-1 text-[10px] text-faint">
            ~{preview.duration_s.toFixed(2)}s · {preview.sampleRate} Hz PCM
            {preview.missingVoice && (
              <span className="text-amber"> · ⚠ speaker has no voice
                profile — flat mock voice</span>)}
          </p>
          {preview.warnings.length > 0 && (
            <ul className="mt-1 space-y-0.5">
              {preview.warnings.map((w, i) => (
                <li key={i} className="text-[10px] text-amber">⚠ {w}</li>
              ))}
            </ul>
          )}
          <div className="mt-2 flex gap-2">
            <button type="button" onClick={() => void approve()}
              className="rounded bg-teal px-2 py-1 text-[11px] font-medium text-ink">
              Approve & generate
            </button>
            <button type="button" onClick={() => setPreview(null)}
              className="rounded border border-line px-2 py-1 text-[11px] text-muted">
              Cancel
            </button>
          </div>
        </div>
      )}

      <ul className="mt-2 space-y-1.5">
        {items.map((d) => {
          const job = jobFor(jobs, d.generate_job_id);
          const running = job && ["queued", "running"].includes(job.status);
          return (
            <li key={d.id}
              className="rounded border border-line bg-panel px-2 py-1.5 text-[11px]">
              <div className="flex items-center gap-2">
                <button type="button"
                  onClick={() => setExpanded(
                    expanded === d.id ? null : d.id)}
                  className="max-w-56 truncate text-left font-medium text-fg hover:text-teal"
                  aria-expanded={expanded === d.id}>
                  {expanded === d.id ? "▾" : "▸"}{" "}
                  {d.speaker_name ? `${d.speaker_name}: ` : ""}
                  {d.text ? `“${d.text.slice(0, 48)}${d.text.length > 48 ? "…" : ""}”`
                    : "(empty line)"}
                </button>
                <VoiceBadge seg={d} />
                {running && (
                  <span className="text-[10px] text-amber">
                    generating… {Math.round((job?.progress ?? 0) * 100)}%
                  </span>
                )}
                {job && job.status === "failed" && !running && (
                  <span className="text-[10px] text-danger">failed — retry</span>)}
                {job && job.status === "cancelled" && !running && (
                  <span className="text-[10px] text-faint">cancelled</span>)}
                {d.result_asset_name && (
                  <span className={`truncate text-[10px] ${
                    d.result_asset_status === "ready"
                      ? "text-teal" : "text-amber"}`}>
                    {d.result_asset_status === "ready" ? "● " : "⚠ "}
                    {d.result_asset_name} (simulated)
                    {d.result_stale && (
                      <span className="text-amber" role="status">
                        {" "}· stale — source changed
                      </span>
                    )}
                  </span>
                )}
                <span className="ml-auto flex items-center gap-1">
                  <button type="button" className={BTN}
                    aria-label="Generate dialogue audio"
                    disabled={!!running || !d.text.trim()}
                    onClick={() => void doGenerate(d)}>
                    Generate</button>
                  {running && (
                    <button type="button" className={BTN}
                      aria-label="Cancel dialogue generation"
                      disabled={job?.cancel_requested}
                      onClick={() => void apiFetch(
                        `/api/jobs/${job.id}/cancel`,
                        { method: "POST" }).catch(() => {})}>
                      {job.cancel_requested ? "Cancelling…" : "Cancel"}
                    </button>)}
                  {d.status === "active" ? (
                    <button type="button" className={BTN}
                      aria-label="Archive dialogue segment"
                      onClick={() => void act("dialogue.archive",
                        { segment_id: d.id })}>
                      Archive</button>
                  ) : (
                    <button type="button" className={BTN}
                      aria-label="Restore dialogue segment"
                      onClick={() => void act("dialogue.unarchive",
                        { segment_id: d.id })}>
                      Restore</button>
                  )}
                </span>
              </div>
              {expanded === d.id && (
                <div className="mt-2 space-y-1.5 border-t border-line/50 pt-2">
                  <div className="grid grid-cols-2 gap-2">
                    <label className="block"><span className={LBL}>Speaker</span>
                      <select className={SEL} aria-label="Speaker"
                        value={dget(d, "character", d.character_id ?? "")}
                        onChange={(e) => dset(
                          d, "character", e.target.value)}>
                        <option value="">— none —</option>
                        {characters.map((c) => (
                          <option key={c.id} value={c.id}>{c.name}</option>))}
                      </select></label>
                    <label className="block"><span className={LBL}>Language</span>
                      <select className={SEL} aria-label="Language"
                        value={dget(d, "language", d.language)}
                        onChange={(e) => dset(
                          d, "language", e.target.value)}>
                        {LANGS.map((l) => (
                          <option key={l} value={l}>{l}</option>))}
                      </select></label>
                  </div>
                  <label className="block"><span className={LBL}>Line</span>
                    <textarea className={INP + " h-12 resize-y"}
                      aria-label="Dialogue text"
                      value={dget(d, "text", d.text)}
                      maxLength={4000}
                      onChange={(e) => dset(d, "text", e.target.value)} />
                  </label>
                  <label className="block"><span className={LBL}>
                    Delivery direction
                  </span>
                    <input className={INP} aria-label="Delivery direction"
                      placeholder="(whispered, urgent — optional)"
                      value={dget(d, "direction", d.direction)}
                      onChange={(e) => dset(
                        d, "direction", e.target.value)} />
                  </label>
                  <div className="grid grid-cols-3 gap-2">
                    <label className="block"><span className={LBL}>Emotion</span>
                      <select className={SEL} aria-label="Emotion"
                        value={dget(d, "emotion",
                          String(d.voice_json?.emotion ?? ""))}
                        onChange={(e) => dset(
                          d, "emotion", e.target.value)}>
                        {EMOTIONS.map((m) => (
                          <option key={m} value={m}>{m || "—"}</option>))}
                      </select></label>
                    <label className="block"><span className={LBL}>
                      Style override
                    </span>
                      <input className={INP} aria-label="Voice style"
                        placeholder="warm, clipped…"
                        value={dget(d, "style",
                          String(d.voice_json?.style ?? ""))}
                        onChange={(e) => dset(
                          d, "style", e.target.value)} />
                    </label>
                    <label className="block"><span className={LBL}>Seed</span>
                      <input className={INP} aria-label="Seed"
                        inputMode="numeric"
                        value={dget(d, "seed", String(d.seed ?? ""))}
                        onChange={(e) => dset(d, "seed", e.target.value)} />
                    </label>
                  </div>
                  {d.result_asset_id && (
                    <div className="flex items-center gap-2 pt-1">
                      <AudioPreview projectId={projectId}
                        assetId={d.result_asset_id} />
                      {timelines.length > 0 && (
                        <>
                          <select className="max-w-40 rounded border border-line bg-ink px-1 py-0.5 text-[10px]"
                            aria-label="Audio lane for placement"
                            value={placeSel[d.id] ?? ""}
                            onFocus={() => {
                              const first = timelines[0]?.id;
                              if (first) void lanesFor(first);
                            }}
                            onChange={(e) => {
                              setPlaceSel((p) => ({
                                ...p, [d.id]: e.target.value }));
                              const tl = e.target.value.split("|")[0];
                              if (tl) void lanesFor(tl);
                            }}>
                            <option value="">place on…</option>
                            {timelines.flatMap((t) =>
                              (audioLanes[t.id] ?? []).map((l) => (
                                <option key={t.id + "|" + l.id}
                                  value={t.id + "|" + l.id}>
                                  {t.name} · {l.name}
                                </option>)))}
                          </select>
                          <button type="button" className={BTN}
                            aria-label="Place dialogue on timeline"
                            disabled={!placeSel[d.id]}
                            onClick={() => void place(d)}>
                            Place</button>
                        </>
                      )}
                    </div>
                  )}
                  {dirty(d) && (
                    <button type="button" onClick={() => void save(d)}
                      className="rounded bg-teal px-2 py-0.5 text-[10px] font-medium text-ink">
                      Save changes</button>
                  )}
                </div>
              )}
            </li>
          );
        })}
        {items.length === 0 && (
          <li className="text-[10px] text-faint">
            No dialogue segments yet — add one to script this{" "}
            {shotId ? "shot" : "scene"}.
          </li>
        )}
      </ul>
    </section>
  );
}
