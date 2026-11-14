/**
 * C5-D — per-step bodies for the guided First Film wizard. Each step
 * keeps its own form state; advancing never creates duplicates because
 * created entity ids are stored on `state` and checked before commit.
 */
import { useEffect, useState } from "react";
import clsx from "clsx";
import { pickFile, previewCommand } from "../../api/commands";
import type {
  Asset, CommandProposal, Entity, Job, StoryboardScene, TimelineDetail,
} from "../../api/types";
import type { FirstFilmState, MethodOption } from "./wizard";
import { pollJob } from "./jobs";

type ShotInfo = Map<string, { title: string; prompt: string; sceneId: string }>;

const input = "rounded-md border border-line bg-panel px-3 py-1.5 text-sm";
const btn = "rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40";
const btnGhost = "rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-fg disabled:opacity-40";

// ---------- 1. film settings ----------

export function SetupStep({ state, busy, onSave }: {
  state: FirstFilmState; busy: boolean;
  onSave: (film: FirstFilmState["film"]) => Promise<void>;
}) {
  const [film, setFilm] = useState(state.film);
  const fps = film.fps_num / (film.fps_den || 1);
  return (
    <div>
      <h2 className="font-semibold">About your film</h2>
      <p className="mt-1 text-sm text-muted">
        Just the basics — you can change everything later in the advanced editors.
      </p>
      <label className="mt-4 block text-sm">
        Film title
        <input className={clsx(input, "mt-1 w-full")} value={film.name}
          onChange={(e) => setFilm({ ...film, name: e.target.value })}
          aria-label="Film title" />
      </label>
      <div className="mt-3 flex gap-3">
        <label className="block text-sm">
          Shape
          <select className={clsx(input, "mt-1")} value={film.aspect_ratio}
            onChange={(e) => setFilm({ ...film, aspect_ratio: e.target.value })}
            aria-label="Aspect ratio">
            <option value="16:9">16:9 — widescreen</option>
            <option value="9:16">9:16 — vertical</option>
            <option value="1:1">1:1 — square</option>
            <option value="4:3">4:3 — classic</option>
          </select>
        </label>
        <label className="block text-sm">
          Frame rate
          <select className={clsx(input, "mt-1")} value={fps}
            onChange={(e) => {
              const v = Number(e.target.value);
              setFilm({ ...film, fps_num: v === 23.976 ? 24000 : v, fps_den: v === 23.976 ? 1001 : 1 });
            }}
            aria-label="Frame rate">
            <option value={24}>24 fps — film look</option>
            <option value={23.976}>23.976 fps — broadcast</option>
            <option value={30}>30 fps — video look</option>
            <option value={60}>60 fps — smooth</option>
          </select>
        </label>
      </div>
      <button className={clsx(btn, "mt-4")} disabled={busy || !film.name.trim()}
        onClick={() => void onSave(film)}>
        {busy ? "Saving…" : "Save & continue"}
      </button>
    </div>
  );
}

// ---------- 2. characters (optional) ----------

export function CharactersStep({ state, characters, busy, onCreate, onToggle }: {
  state: FirstFilmState; characters: Entity[]; busy: boolean;
  onCreate: (name: string, summary: string) => Promise<void>;
  onToggle: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const [summary, setSummary] = useState("");
  return (
    <div>
      <h2 className="font-semibold">Who's in your film? <span className="text-faint">(optional)</span></h2>
      <p className="mt-1 text-sm text-muted">
        Characters describe who appears on screen — the prompt composer
        weaves their look into every shot they're in. Skip this if your
        film has no recurring people.
      </p>
      {characters.length > 0 && (
        <ul className="mt-3 space-y-1">
          {characters.map((c) => (
            <li key={c.id}>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox"
                  checked={state.character_ids.includes(c.id)}
                  onChange={() => onToggle(c.id)}
                  aria-label={`Use ${c.name}`} />
                <span>{c.name}</span>
                {c.summary && <span className="text-faint">— {c.summary}</span>}
              </label>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-4 rounded-md border border-line p-3">
        <p className="text-sm font-medium">New character</p>
        <input className={clsx(input, "mt-2 w-full")} placeholder="Name (e.g. Ada)" value={name}
          onChange={(e) => setName(e.target.value)} aria-label="Character name" />
        <input className={clsx(input, "mt-2 w-full")}
          placeholder="One line on how they look (e.g. red jacket, short dark hair)"
          value={summary} onChange={(e) => setSummary(e.target.value)}
          aria-label="Character description" />
        <button className={clsx(btn, "mt-2")} disabled={busy || !name.trim()}
          onClick={() => { void onCreate(name.trim(), summary.trim()); setName(""); setSummary(""); }}>
          {busy ? "Adding…" : "Add character"}
        </button>
      </div>
    </div>
  );
}

// ---------- 3. scene ----------

export function SceneStep({ state, board, busy, onSelect, onCreate }: {
  state: FirstFilmState; board: StoryboardScene[]; busy: boolean;
  onSelect: (id: string) => void;
  onCreate: (title: string, description: string) => Promise<void>;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const active = board.filter((s) => s.scene.status === "active");
  return (
    <div>
      <h2 className="font-semibold">Set the scene</h2>
      <p className="mt-1 text-sm text-muted">
        A scene is one place and time in your story — "kitchen, morning".
        Every film needs at least one.
      </p>
      {active.length > 0 && (
        <ul className="mt-3 space-y-1">
          {active.map((s) => (
            <li key={s.scene.id}>
              <label className="flex items-center gap-2 text-sm">
                <input type="radio" name="ff-scene"
                  checked={state.scene_id === s.scene.id}
                  onChange={() => onSelect(s.scene.id)}
                  aria-label={`Scene ${s.scene.title}`} />
                <span>{s.scene.title}</span>
                <span className="text-faint">{s.shots.length} shot(s)</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-4 rounded-md border border-line p-3">
        <p className="text-sm font-medium">New scene</p>
        <input className={clsx(input, "mt-2 w-full")} placeholder='e.g. "Rooftop at dusk"' value={title}
          onChange={(e) => setTitle(e.target.value)} aria-label="Scene title" />
        <input className={clsx(input, "mt-2 w-full")} placeholder="What's happening here? (optional)"
          value={description} onChange={(e) => setDescription(e.target.value)}
          aria-label="Scene description" />
        <button className={clsx(btn, "mt-2")} disabled={busy || !title.trim()}
          onClick={() => { void onCreate(title.trim(), description.trim()); setTitle(""); setDescription(""); }}>
          {busy ? "Creating…" : "Create scene"}
        </button>
      </div>
    </div>
  );
}

// ---------- 4. shots ----------

export function ShotsStep({ state, shotsById, characters, busy, onCreate, onRemove }: {
  state: FirstFilmState; shotsById: ShotInfo; characters: Entity[]; busy: boolean;
  onCreate: (title: string, prompt: string, characterIds: string[]) => Promise<void>;
  onRemove: (shotId: string) => Promise<void>;
}) {
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [cast, setCast] = useState<string[]>([]);
  const wizardChars = characters.filter((c) => state.character_ids.includes(c.id));
  return (
    <div>
      <h2 className="font-semibold">Your shots</h2>
      <p className="mt-1 text-sm text-muted">
        Each shot is one clip in the finished film. Give it a name and a
        line describing what should be on screen — that's the prompt the
        composer builds on.
      </p>
      <ol className="mt-3 space-y-2">
        {state.shot_ids.map((id, i) => {
          const sh = shotsById.get(id);
          return (
            <li key={id} className="flex items-center gap-2 rounded border border-line px-3 py-2 text-sm">
              <span className="text-faint">{i + 1}.</span>
              <span className="font-medium">{sh?.title ?? "(deleted shot)"}</span>
              {sh?.prompt && <span className="truncate text-muted">— {sh.prompt}</span>}
              <button className="ml-auto text-xs text-danger" onClick={() => void onRemove(id)}
                aria-label={`Remove shot ${sh?.title ?? id}`}>Remove</button>
            </li>
          );
        })}
      </ol>
      <div className="mt-4 rounded-md border border-line p-3">
        <p className="text-sm font-medium">New shot</p>
        <input className={clsx(input, "mt-2 w-full")} placeholder='Name — e.g. "Opening wide"' value={title}
          onChange={(e) => setTitle(e.target.value)} aria-label="Shot title" />
        <input className={clsx(input, "mt-2 w-full")}
          placeholder="What should we see? — e.g. rain on a neon-lit street"
          value={prompt} onChange={(e) => setPrompt(e.target.value)} aria-label="Shot prompt" />
        {wizardChars.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-3 text-sm">
            {wizardChars.map((c) => (
              <label key={c.id} className="flex items-center gap-1.5">
                <input type="checkbox" checked={cast.includes(c.id)}
                  onChange={() => setCast(cast.includes(c.id) ? cast.filter((x) => x !== c.id) : [...cast, c.id])}
                  aria-label={`Cast ${c.name} in this shot`} />
                {c.name}
              </label>
            ))}
          </div>
        )}
        <button className={clsx(btn, "mt-2")} disabled={busy || !title.trim()}
          onClick={() => { void onCreate(title.trim(), prompt.trim(), cast); setTitle(""); setPrompt(""); setCast([]); }}>
          {busy ? "Adding…" : "Add shot"}
        </button>
      </div>
      {state.shot_ids.length === 0 && (
        <p className="mt-2 text-xs text-amber">Add at least one shot to continue.</p>
      )}
    </div>
  );
}

// ---------- 5. method ----------

const STATUS_STYLE: Record<MethodOption["status"], string> = {
  ready: "text-teal",
  simulated: "text-sky",
  planned: "text-faint",
  unavailable: "text-faint",
  uninstalled: "text-faint",
};
const STATUS_LABEL: Record<MethodOption["status"], string> = {
  ready: "Available now",
  simulated: "Simulated",
  planned: "Planned — not operational",
  unavailable: "Unavailable",
  uninstalled: "Not installed",
};

export function MethodStep({ state, methods, onPick }: {
  state: FirstFilmState; methods: MethodOption[];
  onPick: (m: MethodOption["id"], detail: string) => void;
}) {
  return (
    <div>
      <h2 className="font-semibold">How will you fill your shots?</h2>
      <p className="mt-1 text-sm text-muted">
        Every option below is labelled with what it really is — nothing
        pretends to be a model you don't have. Importing your own clips
        always works.
      </p>
      <ul className="mt-4 space-y-2">
        {methods.map((m, i) => {
          const selectable = m.status === "ready" || m.status === "simulated";
          const selected = state.method === m.id && state.method_detail === m.detail;
          return (
            <li key={i}>
              <button
                disabled={!selectable}
                onClick={() => onPick(m.id, m.detail)}
                aria-pressed={selected}
                className={clsx(
                  "w-full rounded-md border p-3 text-left",
                  selected ? "border-teal bg-teal/10" : "border-line",
                  selectable ? "hover:border-teal" : "opacity-60",
                )}
              >
                <div className="flex items-center justify-between text-sm">
                  <span className="font-medium">{m.label}</span>
                  <span className={clsx("text-xs", STATUS_STYLE[m.status])}>{STATUS_LABEL[m.status]}</span>
                </div>
                <p className="mt-1 text-xs text-muted">{m.note}</p>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ---------- 6. preview & generate (simulated image / local video) ----------

export function GenerateStep({ state, shotsById, pid, commitFn, onGenerated, onSkip, onError }: {
  state: FirstFilmState; shotsById: ShotInfo;
  pid: string;
  commitFn: (ct: string, payload: Record<string, unknown>) => Promise<{ job_id: string | null }>;
  onGenerated: (shotId: string, assetId: string) => Promise<void>;
  onSkip: () => void;
  onError: (e: unknown) => void;
}) {
  const [previews, setPreviews] = useState<Record<string, CommandProposal>>({});
  const [running, setRunning] = useState<Record<string, string>>({}); // shotId -> jobId

  if (state.method === "import") {
    return (
      <div>
        <h2 className="font-semibold">Nothing to generate</h2>
        <p className="mt-1 text-sm text-muted">
          You chose to import your own clips — the next step is where you
          attach a video file to each shot.
        </p>
        <button className={clsx(btn, "mt-4")} onClick={onSkip}>Continue to imports</button>
      </div>
    );
  }

  async function preview(shotId: string) {
    try {
      const p = state.method === "video"
        ? await previewCommand(pid, "shot.video", { shot_id: shotId, model_key: state.method_detail })
        : await previewCommand(pid, "shot.generate", {
            shot_id: shotId,
            provider_key: (state.method_detail ?? "").split("|")[0],
            model_id: (state.method_detail ?? "").split("|")[1],
            execution_target: "cloud",
          });
      setPreviews((x) => ({ ...x, [shotId]: p }));
    } catch (e) { onError(e); }
  }

  async function generate(shotId: string) {
    try {
      // Always preview first so every pin is present — the approval
      // contract (expected_prompt/expected_shot/expected_params)
      // refuses a stale approval rather than guessing.
      let p = previews[shotId];
      if (!p) {
        p = state.method === "video"
          ? await previewCommand(pid, "shot.video", { shot_id: shotId, model_key: state.method_detail })
          : await previewCommand(pid, "shot.generate", {
              shot_id: shotId,
              provider_key: (state.method_detail ?? "").split("|")[0],
              model_id: (state.method_detail ?? "").split("|")[1],
              execution_target: "cloud",
            });
        setPreviews((x) => ({ ...x, [shotId]: p }));
      }
      const r = await commitFn(
        state.method === "video" ? "shot.video" : "shot.generate",
        state.method === "video"
          ? {
              shot_id: shotId, model_key: state.method_detail,
              expected_prompt: p.resolved_prompt?.prompt_sha256,
              expected_shot: p.resolved_shot,
              expected_params: p.resolved_params,
            }
          : {
              shot_id: shotId,
              provider_key: (state.method_detail ?? "").split("|")[0],
              model_id: (state.method_detail ?? "").split("|")[1],
              execution_target: "cloud",
              expected_prompt: p.resolved_prompt?.prompt_sha256,
              expected_shot: p.resolved_shot,
              expected_references: p.resolved_references,
              expected_set: p.resolved_set,
            });
      if (!r.job_id) throw new Error("no generation job was created");
      setRunning((x) => ({ ...x, [shotId]: r.job_id! }));
      const done = await pollJob(pid, r.job_id);
      if (!done.output_asset_id) {
        throw new Error("Generation finished but produced no asset — see the jobs panel.");
      }
      await onGenerated(shotId, done.output_asset_id);
    } catch (e) { onError(e); }
  }

  const allDone = state.shot_ids.every((id) => state.shot_assets[id]);
  return (
    <div>
      <h2 className="font-semibold">Preview & generate</h2>
      <p className="mt-1 text-sm text-muted">
        For each shot you can preview the full prompt — your words first,
        then the character/scene/continuity context the composer adds.
        Approving generates the shot.
      </p>
      <ul className="mt-4 space-y-4">
        {state.shot_ids.map((id) => {
          const sh = shotsById.get(id);
          const p = previews[id];
          const rp = p?.resolved_prompt;
          const doneAsset = state.shot_assets[id];
          const runningJob = running[id];
          return (
            <li key={id} className="rounded-md border border-line p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{sh?.title ?? id}</span>
                {doneAsset ? (
                  <span className="text-xs text-teal">Generated ✓</span>
                ) : runningJob ? (
                  <span className="text-xs text-sky">Generating…</span>
                ) : (
                  <span className="flex gap-2">
                    <button className={btnGhost} onClick={() => void preview(id)}>Preview prompt</button>
                    <button className={btnGhost} onClick={() => void generate(id)}>
                      {p ? "Approve & generate" : "Generate"}
                    </button>
                  </span>
                )}
              </div>
              {rp && !doneAsset && (
                <div className="mt-2 rounded bg-panel p-2 text-xs">
                  <p className="whitespace-pre-wrap">{rp.prompt}</p>
                  <p className="mt-1 text-faint">
                    Budget: {rp.budget.used}/{rp.budget.limit} chars
                  </p>
                  {(rp.conflicts?.length ?? 0) > 0 && (
                    <p className="mt-1 text-amber">Conflicts: {rp.conflicts.join(" · ")}</p>
                  )}
                  {(rp.omitted?.length ?? 0) > 0 && (
                    <p className="mt-1 text-amber">Left out by the budget: {rp.omitted.join(", ")}</p>
                  )}
                  {(rp.warnings?.length ?? 0) > 0 && (
                    <p className="mt-1 text-amber">{rp.warnings.join(" · ")}</p>
                  )}
                  <p className="mt-1 text-faint">
                    Guidance improves consistency — it doesn't guarantee the
                    generated image or video will match.
                  </p>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <p className="mt-3 text-xs text-faint">
        {allDone ? "All shots generated — continue below." : "Generate each shot, or continue once every shot has media."}
      </p>
    </div>
  );
}

// ---------- 7. shot media (import) ----------

export function MediaStep({ state, shotsById, assets, busy, importJobs, onCancel, onImport, onPickAsset }: {
  state: FirstFilmState; shotsById: ShotInfo; assets: Asset[]; busy: string | null;
  importJobs?: Record<string, { jobId: string; message: string }>;
  onCancel?: (shotId: string) => Promise<void>;
  onImport: (shotId: string, path: string) => Promise<void>;
  onPickAsset: (shotId: string, assetId: string) => void;
}) {
  const [paths, setPaths] = useState<Record<string, string>>({});
  const videos = assets.filter((a) => a.kind === "video" && a.status === "ready");
  return (
    <div>
      <h2 className="font-semibold">Attach media to each shot</h2>
      <p className="mt-1 text-sm text-muted">
        Give each shot a video file from this computer, or pick one
        already in the project. MP4 and MOV work best.
      </p>
      <ul className="mt-4 space-y-4">
        {state.shot_ids.map((id) => {
          const sh = shotsById.get(id);
          const attached = state.shot_assets[id];
          return (
            <li key={id} className="rounded-md border border-line p-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{sh?.title ?? id}</span>
                {attached && <span className="text-xs text-teal">Media attached ✓</span>}
              </div>
              {!attached && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <input className={clsx(input, "min-w-0 flex-1")}
                    placeholder="File path — e.g. C:\Videos\clip.mp4"
                    value={paths[id] ?? ""}
                    onChange={(e) => setPaths({ ...paths, [id]: e.target.value })}
                    aria-label={`File path for ${sh?.title ?? id}`} />
                  <button className={btnGhost} onClick={async () => {
                    const f = await pickFile("Choose a video clip",
                      [{ name: "Video", extensions: ["mp4", "mov", "mkv", "webm"] }]);
                    if (f) setPaths({ ...paths, [id]: f });
                  }}>Browse…</button>
                  <button className={btnGhost}
                    disabled={busy === `imp-${id}` || !(paths[id] ?? "").trim()}
                    onClick={() => void onImport(id, (paths[id] ?? "").trim())}>
                    {busy === `imp-${id}` ? "Importing…" : "Import"}
                  </button>
                  {busy === `imp-${id}` && importJobs?.[id] && (
                    <span className="text-xs text-sky">
                      {importJobs[id].message}
                      {onCancel && (
                        <button className="ml-2 underline text-muted hover:text-fg"
                          onClick={() => void onCancel(id)}>
                          Cancel
                        </button>
                      )}
                    </span>
                  )}
                  {videos.length > 0 && (
                    <select className={input} defaultValue=""
                      onChange={(e) => e.target.value && onPickAsset(id, e.target.value)}
                      aria-label={`Use existing clip for ${sh?.title ?? id}`}>
                      <option value="">or use an existing clip…</option>
                      {videos.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                    </select>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ---------- 8. build timeline ----------

export function TimelineStep({ state, shotsById, assets, busy, onBuild }: {
  state: FirstFilmState; shotsById: ShotInfo; assets: Asset[]; busy: boolean;
  onBuild: () => Promise<void>;
}) {
  const byId = new Map(assets.map((a) => [a.id, a]));
  return (
    <div>
      <h2 className="font-semibold">Build the timeline</h2>
      <p className="mt-1 text-sm text-muted">
        This places your shot clips onto a timeline in the order you
        made them — the same timeline the advanced editor uses.
      </p>
      <ol className="mt-3 space-y-1 text-sm">
        {state.shot_ids.map((id, i) => {
          const a = byId.get(state.shot_assets[id] ?? "");
          return (
            <li key={id} className="flex gap-2">
              <span className="text-faint">{i + 1}.</span>
              <span>{shotsById.get(id)?.title ?? id}</span>
              <span className="text-faint">→ {a?.name ?? "no media"}</span>
            </li>
          );
        })}
      </ol>
      <button className={clsx(btn, "mt-4")} disabled={busy}
        onClick={() => void onBuild()}>
        {busy ? "Building…" : state.timeline_id ? "Rebuild timeline" : "Build timeline"}
      </button>
      {state.timeline_id && <p className="mt-2 text-xs text-teal">Timeline ready ✓</p>}
    </div>
  );
}

// ---------- 9. arrange & trim ----------

export function ArrangeStep({ detail, assets, busy, onMove, onTrim }: {
  detail: TimelineDetail | null; assets: Asset[]; busy: boolean;
  onMove: (clipId: string, toIndex: number) => Promise<void>;
  onTrim: (clipId: string, inFrame: number, outFrame: number) => Promise<void>;
}) {
  const byId = new Map(assets.map((a) => [a.id, a]));
  if (!detail) return <p className="text-sm text-muted">Loading timeline…</p>;
  return (
    <div>
      <h2 className="font-semibold">Arrange & trim <span className="text-faint">(optional)</span></h2>
      <p className="mt-1 text-sm text-muted">
        Reorder clips or cut frames off the start/end. Trim numbers are
        in frames — {detail.clips[0]?.src_fps_num ?? 24} frames ≈ 1 second here.
      </p>
      <ol className="mt-3 space-y-2">
        {detail.clips.map((c, i) => (
          <li key={c.id} className="flex flex-wrap items-center gap-2 rounded border border-line px-3 py-2 text-sm">
            <span className="text-faint">{i + 1}.</span>
            <span className="min-w-0 truncate font-medium">{byId.get(c.source_asset_id)?.name ?? c.source_asset_id}</span>
            <span className="text-faint">{c.out_frame - c.in_frame} frames</span>
            <span className="ml-auto flex items-center gap-1">
              <button className={btnGhost} disabled={busy || i === 0}
                onClick={() => void onMove(c.id, i - 1)} aria-label="Move clip up">↑</button>
              <button className={btnGhost} disabled={busy || i === detail.clips.length - 1}
                onClick={() => void onMove(c.id, i + 1)} aria-label="Move clip down">↓</button>
              <TrimInline clip={c} busy={busy} onTrim={onTrim} />
            </span>
          </li>
        ))}
      </ol>
      <p className="mt-3 text-xs text-faint">
        Total: {detail.total_duration_s.toFixed(1)}s. Skip this if the order is already right.
      </p>
    </div>
  );
}

function TrimInline({ clip, busy, onTrim }: {
  clip: TimelineDetail["clips"][number]; busy: boolean;
  onTrim: (clipId: string, inFrame: number, outFrame: number) => Promise<void>;
}) {
  const [inF, setInF] = useState(String(clip.in_frame));
  const [outF, setOutF] = useState(String(clip.out_frame));
  useEffect(() => { setInF(String(clip.in_frame)); setOutF(String(clip.out_frame)); },
    [clip.in_frame, clip.out_frame]);
  return (
    <span className="flex items-center gap-1 text-xs text-muted">
      trim
      <input className={clsx(input, "w-16 px-1 py-0.5 text-xs")} value={inF}
        onChange={(e) => setInF(e.target.value)} aria-label="Trim start frame" />
      –
      <input className={clsx(input, "w-16 px-1 py-0.5 text-xs")} value={outF}
        onChange={(e) => setOutF(e.target.value)} aria-label="Trim end frame" />
      <button className={btnGhost} disabled={busy}
        onClick={() => void onTrim(clip.id, Math.floor(Number(inF) || 0), Math.floor(Number(outF) || 0))}>
        Apply
      </button>
    </span>
  );
}

// ---------- 10. export profile ----------

export function ProfileStep({ state, onPick }: {
  state: FirstFilmState; onPick: (profile: string) => void;
}) {
  const opts = [
    { id: "source", label: "Match my clips", note: "Keeps each clip's own size and frame rate where possible (fastest — copies when it can)." },
    { id: "720p", label: "720p HD", note: "Good quality, small file — great for sharing." },
    { id: "1080p", label: "1080p Full HD", note: "Sharper, bigger file." },
  ];
  return (
    <div>
      <h2 className="font-semibold">Export settings</h2>
      <p className="mt-1 text-sm text-muted">How should the finished film look?</p>
      <ul className="mt-4 space-y-2">
        {opts.map((o) => (
          <li key={o.id}>
            <button onClick={() => onPick(o.id)} aria-pressed={state.profile === o.id}
              className={clsx("w-full rounded-md border p-3 text-left",
                state.profile === o.id ? "border-teal bg-teal/10" : "border-line hover:border-teal")}>
              <div className="text-sm font-medium">{o.label}</div>
              <p className="mt-1 text-xs text-muted">{o.note}</p>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------- 11. export ----------

export function ExportStep({ state, job, busy, onExport, onFinish, onOpenTimeline, onOpenAssets }: {
  state: FirstFilmState; job: Job | undefined; busy: boolean;
  onExport: () => Promise<void>; onFinish: () => Promise<void>;
  onOpenTimeline: () => void; onOpenAssets: () => void;
}) {
  const terminal = job && ["succeeded", "failed", "cancelled"].includes(job.status);
  return (
    <div>
      <h2 className="font-semibold">Export your film</h2>
      <p className="mt-1 text-sm text-muted">
        This renders the timeline to a single MP4 you can find in the
        project's assets.
      </p>
      {!job && (
        <button className={clsx(btn, "mt-4")} disabled={busy} onClick={() => void onExport()}>
          {busy ? "Starting…" : `Export as ${state.profile}`}
        </button>
      )}
      {job && !terminal && (
        <div className="mt-4">
          <p className="text-sm text-sky">Exporting… {Math.round((job.progress ?? 0) * 100)}%</p>
          <div className="mt-2 h-2 rounded-full bg-card-hi">
            <div className="h-2 rounded-full bg-sky" style={{ width: `${Math.round((job.progress ?? 0) * 100)}%` }} />
          </div>
          <p className="mt-1 text-xs text-muted">{job.message}</p>
        </div>
      )}
      {job?.status === "succeeded" && (
        <div className="mt-4 rounded border border-teal/40 bg-teal/10 p-3 text-sm">
          <p className="text-teal">Your film is ready!</p>
          <div className="mt-2 flex gap-3 text-xs">
            <button className="underline text-muted hover:text-fg" onClick={onOpenAssets}>
              See it in assets
            </button>
            <button className="underline text-muted hover:text-fg" onClick={onOpenTimeline}>
              Open the timeline editor
            </button>
          </div>
          {!state.done && (
            <button className={clsx(btn, "mt-3")} onClick={() => void onFinish()}>
              Finish — mark my first film done
            </button>
          )}
          {state.done && <p className="mt-2 text-xs text-teal">First film complete 🎬 — the wizard is done; everything stays editable in the workspace.</p>}
        </div>
      )}
      {terminal && job.status !== "succeeded" && (
        <div className="mt-4 rounded border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
          Export {job.status === "cancelled" ? "was cancelled" : "failed"}: {job.error ?? "see the jobs panel"}
          <button className={clsx(btnGhost, "ml-3")} onClick={() => void onExport()}>Try again</button>
        </div>
      )}
    </div>
  );
}
