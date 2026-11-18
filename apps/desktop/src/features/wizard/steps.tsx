/**
 * A8 — wizard stage bodies. Every control here drives real commands
 * against real project rows; nothing is wizard-private. `mode` only
 * changes how much is shown — Beginner gets sensible defaults and the
 * required fields, Advanced exposes the full validated surface plus
 * deep links into the workspace inspectors. Honesty badges come from
 * the backend-derived state, never assumed.
 */
import { useEffect, useState } from "react";
import clsx from "clsx";
import { apiFetch } from "../../api/client";
import { previewCommand } from "../../api/commands";
import type {
  AssemblyPlan, CastMember, CommandProposal, DialogueSegment, Entity,
  Job, Keyframe, MotionClip, StoryboardScene, Timeline, TimelineDetail,
  WizardDerived, WizardEntityRow,
} from "../../api/types";
import type { WorkspaceTab } from "../../stores/appStore";
import {
  GEN_STATE_CLASS, GEN_STATE_LABEL, parseScriptText,
  type ParsedScene, type WizardState,
} from "./wizard";
import { pollJob } from "./jobs";

export const input = "rounded-md border border-line bg-panel px-3 py-1.5 text-sm";
export const btn = "rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40";
export const btnGhost = "rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-fg disabled:opacity-40";
export const btnWarn = "rounded-md border border-amber/50 bg-amber/10 px-3 py-1.5 text-sm text-amber hover:bg-amber/20 disabled:opacity-40";

/** Shared stage context — everything a step needs from the page. */
export interface Ctx {
  pid: string;
  state: WizardState;
  derived: WizardDerived;
  mode: "beginner" | "advanced";
  board: StoryboardScene[];
  characters: Entity[];
  locations: Entity[];
  timelines: Timeline[];
  jobs: Record<string, Job>;
  busy: string | null;
  commit: (ct: string, payload: Record<string, unknown>) => Promise<{ job_id?: string | null }>;
  save: (next: WizardState) => Promise<void>;
  refresh: () => Promise<void>;
  fail: (e: unknown) => void;
  setBusy: (b: string | null) => void;
  openTab: (t: WorkspaceTab) => void;
}

export function Badge({ state }: { state: string }) {
  return (
    <span className={clsx("rounded border px-1.5 py-0.5 text-[10px] font-medium",
      GEN_STATE_CLASS[state] ?? "border-line bg-panel text-muted")}>
      {GEN_STATE_LABEL[state] ?? state}
    </span>
  );
}

export function WarnList({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <div className="mt-3 rounded border border-amber/40 bg-amber/10 p-2 text-sm text-amber" role="status">
      {items.map((w, i) => <p key={i}>{w}</p>)}
    </div>
  );
}

export function Advanced({ mode, children }: {
  mode: "beginner" | "advanced"; children: React.ReactNode;
}) {
  if (mode !== "advanced") return null;
  return <div className="mt-3 rounded border border-line bg-panel/50 p-3">{children}</div>;
}

export function InspectorLink({ openTab, tab, children }: {
  openTab: (t: WorkspaceTab) => void; tab: WorkspaceTab; children: React.ReactNode;
}) {
  return (
    <button onClick={() => openTab(tab)}
      className="text-xs text-teal underline underline-offset-2 hover:text-teal-hi">
      {children}
    </button>
  );
}

export function shotTitle(board: StoryboardScene[], id: string | null): string {
  for (const sc of board) for (const sh of sc.shots) {
    if (sh.shot.id === id) return sh.shot.title;
  }
  return "(unknown shot)";
}

// ---------- 1. project ----------

export function ProjectStage({ ctx }: { ctx: Ctx }) {
  const projectStage = ctx.derived.stages.find((s) => s.id === "project");
  return (
    <div>
      <h2 className="font-semibold">Your project</h2>
      <p className="mt-1 text-sm text-muted">
        This wizard works inside one project — everything you make is
        saved here and only here.
      </p>
      <div className="mt-4 rounded-md border border-line bg-panel p-3 text-sm">
        {projectStage?.summary}
      </div>
      <p className="mt-3 text-xs text-muted">
        Working on a different film?{" "}
        <InspectorLink openTab={ctx.openTab} tab="overview">
          Open the project overview
        </InspectorLink>{" "}
        or go back to the Projects screen with ← Projects above.
      </p>
    </div>
  );
}

// ---------- 2. film settings ----------

const ASPECTS: Record<string, [number, number][]> = {
  "16:9": [[1920, 1080], [1280, 720], [3840, 2160]],
  "9:16": [[1080, 1920], [720, 1280]],
  "1:1": [[1080, 1080], [720, 720]],
  "4:3": [[1440, 1080], [960, 720]],
  "2.39:1": [[2048, 858]],
};
const FPS_PRESETS: [string, number, number][] = [
  ["24 fps — film look", 24, 1], ["23.976 fps — broadcast", 24000, 1001],
  ["25 fps — PAL", 25, 1], ["30 fps — video look", 30, 1],
  ["29.97 fps — NTSC", 30000, 1001], ["60 fps — smooth", 60, 1],
];

export function FilmSettingsStage({ ctx }: { ctx: Ctx }) {
  const p = ctx.derived.stages.find((s) => s.id === "film_settings");
  const c = (p?.counts ?? {}) as Record<string, unknown>;
  const [aspect, setAspect] = useState(String(c.aspect_ratio ?? "16:9"));
  const [dim, setDim] = useState(`${c.width ?? 1920}x${c.height ?? 1080}`);
  const [fps, setFps] = useState(`${c.fps_num ?? 24}/${c.fps_den ?? 1}`);
  const dims = ASPECTS[aspect] ?? ASPECTS["16:9"];
  return (
    <div>
      <h2 className="font-semibold">Film settings</h2>
      <p className="mt-1 text-sm text-muted">
        Shape, size, and frame rate. These set the timeline math —
        changing them later keeps everything already made.
      </p>
      <div className="mt-4 flex flex-wrap gap-3">
        <label className="block text-sm">Shape
          <select className={clsx(input, "mt-1 block")} value={aspect} aria-label="Aspect ratio"
            onChange={(e) => { setAspect(e.target.value); setDim(`${(ASPECTS[e.target.value] ?? dims)[0][0]}x${(ASPECTS[e.target.value] ?? dims)[0][1]}`); }}>
            {Object.keys(ASPECTS).map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        <label className="block text-sm">Size
          <select className={clsx(input, "mt-1 block")} value={dim} aria-label="Resolution"
            onChange={(e) => setDim(e.target.value)}>
            {dims.map(([w, h]) => <option key={`${w}x${h}`} value={`${w}x${h}`}>{w} × {h}</option>)}
          </select>
        </label>
        <label className="block text-sm">Frame rate
          <select className={clsx(input, "mt-1 block")} value={fps} aria-label="Frame rate"
            onChange={(e) => setFps(e.target.value)}>
            {FPS_PRESETS.map(([label, n, d]) => (
              <option key={label} value={`${n}/${d}`}>{label}</option>))}
          </select>
        </label>
      </div>
      <button className={clsx(btn, "mt-4")} disabled={ctx.busy === "film"}
        onClick={async () => {
          ctx.setBusy("film");
          try {
            const [w, h] = dim.split("x").map(Number);
            const [n, d] = fps.split("/").map(Number);
            await ctx.commit("project.update_settings",
              { aspect_ratio: aspect, width: w, height: h, fps_num: n, fps_den: d });
            await ctx.refresh();
          } catch (e) { ctx.fail(e); return; }
          ctx.setBusy(null);
        }}>
        {ctx.busy === "film" ? "Saving…" : "Save settings"}
      </button>
      <Advanced mode={ctx.mode}>
        <p className="text-xs text-muted">
          Kind, style, and storage options live in{" "}
          <InspectorLink openTab={ctx.openTab} tab="settings">project settings</InspectorLink>.
        </p>
      </Advanced>
    </div>
  );
}

// ---------- 3. screenplay ----------

interface SpDocScene {
  id: string; position: number; heading: string; action: string; notes: string;
  elements: { id: string; kind: string; character_id: string | null; text: string; parenthetical: string }[];
}
interface SpDoc { id: string; title: string; revision: number; scenes: SpDocScene[] }

export function ScreenplayStage({ ctx, screenplays, doc, setDoc }: {
  ctx: Ctx;
  screenplays: { id: string; title: string }[];
  doc: SpDoc | null;
  setDoc: (d: SpDoc | null) => void;
}) {
  const [title, setTitle] = useState("");
  const [pasted, setPasted] = useState("");
  const [parsed, setParsed] = useState<ParsedScene[] | null>(null);

  const loadDoc = async (id: string) => {
    setDoc(await apiFetch<SpDoc>(`/api/projects/${ctx.pid}/screenplays/${id}`));
    await ctx.save({ ...ctx.state, screenplay_id: id });
  };

  return (
    <div>
      <h2 className="font-semibold">Screenplay</h2>
      <p className="mt-1 text-sm text-muted">
        {ctx.mode === "beginner"
          ? "Create a screenplay, or paste a script in the simple format below. Scenes and dialogue here drive the whole film."
          : "Screenplay documents hold scenes (heading + action + notes) and ordered elements (action / dialogue / note)."}
      </p>

      {screenplays.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2" role="list" aria-label="Screenplays">
          {screenplays.map((sp) => (
            <button key={sp.id} onClick={() => void loadDoc(sp.id)}
              className={clsx("rounded border px-3 py-1.5 text-sm",
                ctx.state.screenplay_id === sp.id
                  ? "border-teal bg-teal/10 text-teal"
                  : "border-line text-muted hover:text-fg")}>
              {sp.title}
            </button>
          ))}
        </div>
      )}

      <div className="mt-3 flex gap-2">
        <input className={clsx(input, "flex-1")} placeholder="New screenplay title…"
          value={title} onChange={(e) => setTitle(e.target.value)}
          aria-label="New screenplay title" />
        <button className={btn} disabled={ctx.busy === "sp" || !title.trim()}
          onClick={async () => {
            ctx.setBusy("sp");
            try {
              await ctx.commit("screenplay.create", { title: title.trim() });
              const sps = await apiFetch<{ id: string; title: string }[]>(
                `/api/projects/${ctx.pid}/screenplays`);
              const sp = sps.find((s) => s.title === title.trim());
              if (sp) await loadDoc(sp.id);
              setTitle("");
              await ctx.refresh();
            } catch (e) { ctx.fail(e); return; }
            ctx.setBusy(null);
          }}>
          Create
        </button>
      </div>

      {/* paste import — the one supported text format */}
      <details className="mt-4 rounded-md border border-line bg-panel/40 p-3">
        <summary className="cursor-pointer text-sm font-medium">
          Paste a script (plain-text format)
        </summary>
        <p className="mt-2 text-xs text-muted">
          Headings start with <code>INT.</code> / <code>EXT.</code> /{" "}
          <code>#</code>. Dialogue is <code>NAME: their line</code>.
          Bracketed lines become notes; everything else is action.
          Character names are matched or created on the next step.
        </p>
        <textarea className={clsx(input, "mt-2 h-32 w-full font-mono text-xs")}
          placeholder={"INT. DOCK — NIGHT\nAnna waits by the crane.\nANNA: The tide is coming in."}
          value={pasted} onChange={(e) => { setPasted(e.target.value); setParsed(null); }}
          aria-label="Paste script text" />
        <div className="mt-2 flex gap-2">
          <button className={btnGhost} disabled={!pasted.trim()}
            onClick={() => setParsed(parseScriptText(pasted))}>
            Preview parsed scenes
          </button>
          {parsed && (
            <button className={btn} disabled={ctx.busy === "spimp"}
              onClick={async () => {
                ctx.setBusy("spimp");
                try {
                  const name = `Imported script ${new Date().toLocaleDateString()}`;
                  await ctx.commit("screenplay.create", { title: name });
                  const sps = await apiFetch<{ id: string; title: string }[]>(
                    `/api/projects/${ctx.pid}/screenplays`);
                  const sp = sps.find((s) => s.title === name);
                  if (!sp) throw new Error("import created no screenplay");
                  for (const sc of parsed) {
                    await ctx.commit("screenplay_scene.create",
                      { screenplay_id: sp.id, heading: sc.heading });
                    const d = await apiFetch<SpDoc>(
                      `/api/projects/${ctx.pid}/screenplays/${sp.id}`);
                    const target = d.scenes[d.scenes.length - 1];
                    if (sc.elements.length) {
                      // character names resolve on the cast step —
                      // dialogue text keeps the speaker prefix readable.
                      await ctx.commit("screenplay_scene.update", {
                        sp_scene_id: target.id,
                        elements: sc.elements.map((el) => ({
                          kind: el.kind,
                          text: el.kind === "dialogue" && el.character
                            ? `${el.character}: ${el.text}` : el.text,
                        })),
                      });
                    }
                  }
                  await loadDoc(sp.id);
                  setPasted(""); setParsed(null);
                  await ctx.refresh();
                } catch (e) { ctx.fail(e); return; }
                ctx.setBusy(null);
              }}>
              Import {parsed.length} scene(s)
            </button>
          )}
        </div>
        {parsed && (
          <div className="mt-2 max-h-40 overflow-y-auto rounded border border-line p-2 text-xs">
            {parsed.map((s, i) => (
              <p key={i}><b>{s.heading || "(no heading)"}</b> — {s.elements.length} element(s)</p>
            ))}
          </div>
        )}
      </details>

      {doc && (
        <p className="mt-3 text-sm text-muted">
          <b className="text-fg">{doc.title}</b> — {doc.scenes?.length ?? 0}{" "}
          scene(s). Organize them on the next step.
        </p>
      )}
      <Advanced mode={ctx.mode}>
        <InspectorLink openTab={ctx.openTab} tab="screenplay">
          Open the full screenplay editor
        </InspectorLink>
      </Advanced>
    </div>
  );
}

// ---------- 4. scenes & dialogue ----------

export function ScenesStage({ ctx, doc, setDoc }: {
  ctx: Ctx; doc: SpDoc | null; setDoc: (d: SpDoc | null) => void;
}) {
  const reload = async () => {
    if (ctx.state.screenplay_id) {
      setDoc(await apiFetch<SpDoc>(
        `/api/projects/${ctx.pid}/screenplays/${ctx.state.screenplay_id}`));
    }
    await ctx.refresh();
  };
  if (!doc) {
    return (
      <div>
        <h2 className="font-semibold">Scenes & dialogue</h2>
        <p className="mt-1 text-sm text-muted">
          Pick a screenplay on the previous step first — or add a scene
          here and a screenplay will be created for it.
        </p>
        <button className={clsx(btn, "mt-3")} disabled={ctx.busy === "mksc"}
          onClick={async () => {
            ctx.setBusy("mksc");
            try {
              await ctx.commit("screenplay.create", { title: "Screenplay 1" });
              const sps = await apiFetch<{ id: string }[]>(
                `/api/projects/${ctx.pid}/screenplays`);
              const id = sps[0]?.id;
              if (id) {
                await ctx.save({ ...ctx.state, screenplay_id: id });
                await ctx.commit("screenplay_scene.create",
                  { screenplay_id: id, heading: "INT. SCENE 1" });
                setDoc(await apiFetch<SpDoc>(
                  `/api/projects/${ctx.pid}/screenplays/${id}`));
              }
              await ctx.refresh();
            } catch (e) { ctx.fail(e); return; }
            ctx.setBusy(null);
          }}>
          Create a first scene
        </button>
      </div>
    );
  }
  return (
    <div>
      <h2 className="font-semibold">Scenes & dialogue</h2>
      <p className="mt-1 text-sm text-muted">
        {doc.scenes.length} scene(s) in <b>{doc.title}</b>. Dialogue
        lines need a character — assign speakers here or on the cast step.
      </p>
      <div className="mt-3 space-y-3">
        {doc.scenes.map((sc, i) => (
          <SceneCard key={sc.id} ctx={ctx} sc={sc} index={i}
            count={doc.scenes.length} reload={reload} />
        ))}
      </div>
      <button className={clsx(btnGhost, "mt-3")} disabled={ctx.busy === "addsc"}
        onClick={async () => {
          ctx.setBusy("addsc");
          try {
            await ctx.commit("screenplay_scene.create",
              { screenplay_id: doc.id, heading: `INT. SCENE ${doc.scenes.length + 1}` });
            await reload();
          } catch (e) { ctx.fail(e); return; }
          ctx.setBusy(null);
        }}>
        + Add scene
      </button>
    </div>
  );
}

function SceneCard({ ctx, sc, index, count, reload }: {
  ctx: Ctx; sc: SpDocScene; index: number; count: number;
  reload: () => Promise<void>;
}) {
  const [heading, setHeading] = useState(sc.heading);
  const [action, setAction] = useState(sc.action);
  const [elements, setElements] = useState(sc.elements);
  useEffect(() => {
    setHeading(sc.heading); setAction(sc.action); setElements(sc.elements);
  }, [sc.id, sc.heading, sc.action, sc.elements]);
  const dirty = heading !== sc.heading || action !== sc.action;

  const saveElements = async (els: { kind: string; character_id: string | null; text: string; id?: string }[]) => {
    await ctx.commit("screenplay_scene.update",
      { sp_scene_id: sc.id, elements: els.map((e) => ({ ...e, id: e.id })) });
    await reload();
  };

  return (
    <div className="rounded-md border border-line bg-card p-3">
      <div className="flex items-center gap-2">
        <input className={clsx(input, "flex-1 font-medium")} value={heading}
          onChange={(e) => setHeading(e.target.value)}
          aria-label={`Scene ${index + 1} heading`} />
        <button className={btnGhost} disabled={index === 0 || ctx.busy === `mv-${sc.id}`}
          aria-label="Move scene up"
          onClick={async () => {
            ctx.setBusy(`mv-${sc.id}`);
            try { await ctx.commit("screenplay_scene.move", { sp_scene_id: sc.id, to_index: index - 1 }); await reload(); }
            catch (e) { ctx.fail(e); return; } ctx.setBusy(null);
          }}>↑</button>
        <button className={btnGhost} disabled={index === count - 1 || ctx.busy === `mv-${sc.id}`}
          aria-label="Move scene down"
          onClick={async () => {
            ctx.setBusy(`mv-${sc.id}`);
            try { await ctx.commit("screenplay_scene.move", { sp_scene_id: sc.id, to_index: index + 1 }); await reload(); }
            catch (e) { ctx.fail(e); return; } ctx.setBusy(null);
          }}>↓</button>
        <Advanced mode={ctx.mode}>
          <button className={btnGhost} disabled={ctx.busy === `del-${sc.id}`}
            onClick={async () => {
              ctx.setBusy(`del-${sc.id}`);
              try { await ctx.commit("screenplay_scene.delete", { sp_scene_id: sc.id }); await reload(); }
              catch (e) { ctx.fail(e); return; } ctx.setBusy(null);
            }}>Delete</button>
        </Advanced>
      </div>
      <textarea className={clsx(input, "mt-2 w-full")} rows={2} value={action}
        onChange={(e) => setAction(e.target.value)}
        placeholder="Scene action — what happens here"
        aria-label={`Scene ${index + 1} action`} />
      {(dirty) && (
        <button className={clsx(btnGhost, "mt-1 text-xs")} disabled={ctx.busy === `sv-${sc.id}`}
          onClick={async () => {
            ctx.setBusy(`sv-${sc.id}`);
            try {
              await ctx.commit("screenplay_scene.update",
                { sp_scene_id: sc.id, heading, action });
              await reload();
            } catch (e) { ctx.fail(e); return; }
            ctx.setBusy(null);
          }}>Save heading/action</button>
      )}
      <div className="mt-2 space-y-1">
        {elements.map((el) => (
          <div key={el.id} className="flex items-center gap-2">
            <span className={clsx("w-16 shrink-0 text-[10px] uppercase",
              el.kind === "dialogue" ? "text-teal" : "text-faint")}>
              {el.kind}
            </span>
            {el.kind === "dialogue" && (
              <select className={clsx(input, "w-32 py-0.5 text-xs")}
                value={el.character_id ?? ""}
                aria-label="Speaker"
                onChange={async (e) => {
                  try {
                    await saveElements(elements.map((x) => x.id === el.id
                      ? { ...x, character_id: e.target.value || null } : x));
                  } catch (err) { ctx.fail(err); }
                }}>
                <option value="">(no speaker)</option>
                {ctx.characters.map((c) => (
                  <option key={c.id} value={c.id}>{c.name}</option>))}
              </select>
            )}
            <span className="flex-1 truncate text-xs">{el.text}</span>
            <button className="text-xs text-faint hover:text-danger"
              aria-label="Remove line"
              onClick={async () => {
                try { await saveElements(elements.filter((x) => x.id !== el.id)); }
                catch (err) { ctx.fail(err); }
              }}>✕</button>
          </div>
        ))}
      </div>
      <AddElement ctx={ctx} sc={sc} elements={elements} saveElements={saveElements} />
    </div>
  );
}

function AddElement({ ctx, sc, elements, saveElements }: {
  ctx: Ctx; sc: SpDocScene; elements: SpDocScene["elements"];
  saveElements: (els: { kind: string; character_id: string | null; text: string }[]) => Promise<void>;
}) {
  const [kind, setKind] = useState<"action" | "dialogue" | "note">("action");
  const [text, setText] = useState("");
  const [charId, setCharId] = useState("");
  return (
    <div className="mt-2 flex items-center gap-2">
      <select className={clsx(input, "py-1 text-xs")} value={kind}
        onChange={(e) => setKind(e.target.value as typeof kind)} aria-label="Line kind">
        <option value="action">Action</option>
        <option value="dialogue">Dialogue</option>
        <option value="note">Note</option>
      </select>
      {kind === "dialogue" && (
        <select className={clsx(input, "w-28 py-1 text-xs")} value={charId}
          onChange={(e) => setCharId(e.target.value)} aria-label="Speaker">
          <option value="">(speaker)</option>
          {ctx.characters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      )}
      <input className={clsx(input, "flex-1 py-1 text-xs")} value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={kind === "dialogue" ? "The line they say" : "What happens"}
        aria-label="New line text" />
      <button className={btnGhost} disabled={!text.trim() || ctx.busy === `el-${sc.id}`}
        onClick={async () => {
          ctx.setBusy(`el-${sc.id}`);
          try {
            await saveElements([...elements,
              { kind, character_id: kind === "dialogue" ? charId || null : null, text: text.trim() }]);
            setText("");
          } catch (e) { ctx.fail(e); return; }
          ctx.setBusy(null);
        }}>+ Add</button>
    </div>
  );
}

// ---------- 5. cast & continuity ----------

export function CastStage({ ctx }: { ctx: Ctx }) {
  const [name, setName] = useState("");
  const [loc, setLoc] = useState("");
  const castStage = ctx.derived.stages.find((s) => s.id === "cast");
  const sceneCast = (sc: StoryboardScene) => sc.cast;
  return (
    <div>
      <h2 className="font-semibold">Cast & continuity</h2>
      <p className="mt-1 text-sm text-muted">
        Characters carry appearance, wardrobe, and voice continuity
        into prompts. Assign them to scenes so the shots know who's in frame.
      </p>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div className="rounded-md border border-line bg-card p-3">
          <h3 className="text-sm font-medium">Characters</h3>
          <div className="mt-2 flex gap-2">
            <input className={clsx(input, "flex-1")} value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Character name" aria-label="Character name" />
            <button className={btn} disabled={!name.trim() || ctx.busy === "char"}
              onClick={async () => {
                ctx.setBusy("char");
                try {
                  await ctx.commit("character.create", { name: name.trim() });
                  setName(""); await ctx.refresh();
                } catch (e) { ctx.fail(e); return; }
                ctx.setBusy(null);
              }}>Add</button>
          </div>
          <ul className="mt-2 space-y-1 text-sm">
            {ctx.characters.map((c) => <li key={c.id}>• {c.name}</li>)}
            {!ctx.characters.length && <li className="text-muted">None yet</li>}
          </ul>
        </div>
        <div className="rounded-md border border-line bg-card p-3">
          <h3 className="text-sm font-medium">Locations</h3>
          <div className="mt-2 flex gap-2">
            <input className={clsx(input, "flex-1")} value={loc}
              onChange={(e) => setLoc(e.target.value)}
              placeholder="Location name" aria-label="Location name" />
            <button className={btn} disabled={!loc.trim() || ctx.busy === "loc"}
              onClick={async () => {
                ctx.setBusy("loc");
                try {
                  await ctx.commit("location.create", { name: loc.trim() });
                  setLoc(""); await ctx.refresh();
                } catch (e) { ctx.fail(e); return; }
                ctx.setBusy(null);
              }}>Add</button>
          </div>
          <ul className="mt-2 space-y-1 text-sm">
            {ctx.locations.map((l) => <li key={l.id}>• {l.name}</li>)}
            {!ctx.locations.length && <li className="text-muted">None yet</li>}
          </ul>
        </div>
      </div>

      {/* per-scene casting */}
      <div className="mt-4 space-y-2">
        {ctx.board.map((sc) => (
          <div key={sc.scene.id} className="rounded-md border border-line bg-card p-3">
            <p className="text-sm font-medium">{sc.scene.title}</p>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {sceneCast(sc).map((m: CastMember) => (
                <span key={m.character_id}
                  className="rounded border border-teal/40 bg-teal/10 px-2 py-0.5 text-xs text-teal">
                  {m.character?.name ?? m.character_id}
                  <button className="ml-1 text-faint hover:text-danger"
                    aria-label={`Remove ${m.character?.name ?? "cast member"} from scene`}
                    onClick={async () => {
                      try {
                        await ctx.commit("scene.cast.remove",
                          { scene_id: sc.scene.id, character_id: m.character_id });
                        await ctx.refresh();
                      } catch (e) { ctx.fail(e); }
                    }}>✕</button>
                </span>
              ))}
              <select className={clsx(input, "py-0.5 text-xs")} value=""
                aria-label={`Add cast to ${sc.scene.title}`}
                onChange={async (e) => {
                  if (!e.target.value) return;
                  try {
                    await ctx.commit("scene.cast.assign",
                      { scene_id: sc.scene.id, character_id: e.target.value });
                    await ctx.refresh();
                  } catch (err) { ctx.fail(err); }
                }}>
                <option value="">+ cast…</option>
                {ctx.characters
                  .filter((c) => !sceneCast(sc).some((m) => m.character_id === c.id))
                  .map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </div>
          </div>
        ))}
      </div>
      <WarnList items={castStage?.warnings ?? []} />
      <Advanced mode={ctx.mode}>
        <p className="text-xs text-muted">
          Per-shot blocking, wardrobe/expression overrides, voice
          profiles, and reference-asset links live in{" "}
          <InspectorLink openTab={ctx.openTab} tab="cast">Cast</InspectorLink>,{" "}
          <InspectorLink openTab={ctx.openTab} tab="storyboard">Storyboard</InspectorLink>, and{" "}
          <InspectorLink openTab={ctx.openTab} tab="assets">Assets</InspectorLink>.
        </p>
      </Advanced>
    </div>
  );
}

// ---------- 6. storyboard shots ----------

export function ShotsStage({ ctx }: { ctx: Ctx }) {
  const [proposal, setProposal] = useState<CommandProposal | null>(null);
  const [shotTitleIn, setShotTitleIn] = useState("");
  const shotsStage = ctx.derived.stages.find((s) => s.id === "shots");
  const shotCount = (shotsStage?.counts?.shots as number) ?? 0;

  return (
    <div>
      <h2 className="font-semibold">Storyboard shots</h2>
      <p className="mt-1 text-sm text-muted">
        {shotCount
          ? `${shotCount} shot(s) on the board. Scaffold adds shots from the screenplay; manual edits are always preserved.`
          : "Scaffold builds a suggested shot list from your screenplay — you review it before anything is created."}
      </p>
      <div className="mt-3 flex gap-2">
        <button className={btn} disabled={ctx.busy === "scaffold" || !ctx.state.screenplay_id}
          onClick={async () => {
            ctx.setBusy("scaffold");
            try {
              const p = await previewCommand(ctx.pid, "shotlist.scaffold",
                { screenplay_id: ctx.state.screenplay_id });
              setProposal(p);
            } catch (e) { ctx.fail(e); }
            ctx.setBusy(null);
          }}>
          {ctx.busy === "scaffold" ? "Planning…" : "Preview shot scaffold"}
        </button>
        {proposal && (
          <button className={btnWarn} disabled={ctx.busy === "scaffold"}
            onClick={async () => {
              ctx.setBusy("scaffold");
              try {
                await ctx.commit("shotlist.scaffold",
                  { screenplay_id: ctx.state.screenplay_id });
                setProposal(null);
                await ctx.refresh();
              } catch (e) { ctx.fail(e); return; }
              ctx.setBusy(null);
            }}>
            Apply scaffold
          </button>
        )}
      </div>
      {proposal && (
        <div className="mt-2 max-h-48 overflow-y-auto rounded border border-line bg-panel p-2 text-xs">
          <pre className="whitespace-pre-wrap">
            {JSON.stringify(proposal.summary ?? proposal, null, 2).slice(0, 3000)}
          </pre>
        </div>
      )}
      {!ctx.state.screenplay_id && (
        <p className="mt-2 text-xs text-amber">
          No screenplay selected — create shots manually below or go back to step 3.
        </p>
      )}

      <div className="mt-4 space-y-2">
        {ctx.board.map((sc) => (
          <div key={sc.scene.id} className="rounded-md border border-line bg-card p-3">
            <p className="text-sm font-medium">{sc.scene.title}</p>
            <ul className="mt-1 space-y-1 text-xs text-muted">
              {sc.shots.map((sh) => (
                <li key={sh.shot.id}>
                  • {sh.shot.title}{" "}
                  <span className="text-faint">({sh.shot.shot_type})</span>
                </li>
              ))}
              {!sc.shots.length && <li>No shots yet</li>}
            </ul>
            <div className="mt-2 flex gap-2">
              <input className={clsx(input, "flex-1 py-1 text-xs")}
                placeholder="New shot title" value={shotTitleIn}
                onChange={(e) => setShotTitleIn(e.target.value)}
                aria-label={`New shot in ${sc.scene.title}`} />
              <button className={btnGhost} disabled={!shotTitleIn.trim() || ctx.busy === `shot-${sc.scene.id}`}
                onClick={async () => {
                  ctx.setBusy(`shot-${sc.scene.id}`);
                  try {
                    await ctx.commit("shot.create",
                      { scene_id: sc.scene.id, title: shotTitleIn.trim(), shot_type: "medium" });
                    setShotTitleIn("");
                    await ctx.refresh();
                  } catch (e) { ctx.fail(e); return; }
                  ctx.setBusy(null);
                }}>+ Shot</button>
            </div>
          </div>
        ))}
      </div>
      <Advanced mode={ctx.mode}>
        <InspectorLink openTab={ctx.openTab} tab="storyboard">
          Open the full storyboard
        </InspectorLink>
      </Advanced>
    </div>
  );
}

// ---------- 7. shot planning ----------

const FOLLOW_MODES = [
  "follow_behind", "follow_front", "follow_side", "follow_shoulder",
  "orbit_follow", "drone_follow", "vehicle_chase", "lead_and_follow",
  "group_follow", "target_switch",
];
const ANGLES = ["eye_level", "low", "high", "top_down", "dutch"];
const MOVEMENTS = ["static", "pan", "tilt", "dolly_in", "dolly_out",
  "truck_left", "truck_right", "crane_up", "crane_down", "handheld", "zoom"];
const FRAMINGS = ["extreme_close_up", "close_up", "medium_close_up", "medium",
  "medium_wide", "wide", "extreme_wide"];

export function PlanningStage({ ctx }: { ctx: Ctx }) {
  const shots = ctx.board.flatMap((sc) => sc.shots.map((sh) => ({ sc, sh })));
  if (!shots.length) {
    return (
      <div>
        <h2 className="font-semibold">Shot planning</h2>
        <p className="mt-1 text-sm text-muted">Add shots first — planning attaches to them.</p>
      </div>
    );
  }
  return (
    <div>
      <h2 className="font-semibold">Shot planning</h2>
      <p className="mt-1 text-sm text-muted">
        {ctx.mode === "beginner"
          ? "Pick a camera move and lighting note for each shot — you can refine everything later."
          : "Full planning surface: angle, movement, all ten follow modes, framing, placement, fps, continuity refs, lighting, and notes."}
      </p>
      <div className="mt-3 space-y-3">
        {shots.map(({ sc, sh }) => (
          <PlanCard key={sh.shot.id} ctx={ctx} sc={sc} sh={sh} />
        ))}
      </div>
    </div>
  );
}

function PlanCard({ ctx, sc, sh }: {
  ctx: Ctx; sc: StoryboardScene; sh: StoryboardScene["shots"][number];
}) {
  const existing = sh.shot.planning_json ?? {};
  const cam = existing.camera ?? {};
  const [movement, setMovement] = useState(cam.movement ?? "static");
  const [angle, setAngle] = useState(cam.angle ?? "eye_level");
  const [follow, setFollow] = useState(cam.follow_mode ?? "");
  const [framing, setFraming] = useState(existing.framing ?? "medium");
  const [lighting, setLighting] = useState(existing.lighting ?? "");
  const [targets, setTargets] = useState<string[]>(
    (cam.follow_targets ?? []).flatMap((t) =>
      t.kind === "character" ? [t.id] : []));
  const hasPlan = Object.keys(existing).length > 0;

  const save = async () => {
    ctx.setBusy(`plan-${sh.shot.id}`);
    try {
      await ctx.commit("shot.update", {
        shot_id: sh.shot.id,
        planning: {
          camera: {
            angle, movement,
            follow_mode: follow || undefined,
            follow_targets: targets.map((id) => ({ kind: "character", id })),
          },
          framing,
          lighting: lighting || undefined,
        },
      });
      await ctx.refresh();
    } catch (e) { ctx.fail(e); return; }
    ctx.setBusy(null);
  };

  return (
    <div className="rounded-md border border-line bg-card p-3">
      <div className="flex items-center gap-2">
        <p className="flex-1 text-sm font-medium">
          {sh.shot.title} <span className="text-faint">· {sc.scene.title}</span>
        </p>
        {hasPlan && <Badge state="approved" />}
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <label className="text-xs">Camera move
          <select className={clsx(input, "mt-0.5 block py-1 text-xs")} value={movement}
            onChange={(e) => setMovement(e.target.value)}>
            {MOVEMENTS.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <label className="text-xs">Framing
          <select className={clsx(input, "mt-0.5 block py-1 text-xs")} value={framing}
            onChange={(e) => setFraming(e.target.value)}>
            {FRAMINGS.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
        </label>
        <label className="flex-1 text-xs">Lighting note
          <input className={clsx(input, "mt-0.5 block w-full py-1 text-xs")} value={lighting}
            onChange={(e) => setLighting(e.target.value)} placeholder="e.g. low-key, hard rim" />
        </label>
      </div>
      <Advanced mode={ctx.mode}>
        <div className="flex flex-wrap gap-2">
          <label className="text-xs">Angle
            <select className={clsx(input, "mt-0.5 block py-1 text-xs")} value={angle}
              onChange={(e) => setAngle(e.target.value)}>
              {ANGLES.map((a) => <option key={a} value={a}>{a}</option>)}
            </select>
          </label>
          <label className="text-xs">Follow mode
            <select className={clsx(input, "mt-0.5 block py-1 text-xs")} value={follow}
              onChange={(e) => setFollow(e.target.value)}>
              <option value="">—</option>
              {FOLLOW_MODES.map((f) => <option key={f} value={f}>{f}</option>)}
            </select>
          </label>
          {follow && (
            <fieldset className="text-xs">
              <legend className="text-faint">Follow targets</legend>
              <div className="mt-0.5 flex flex-wrap gap-2">
                {ctx.characters.map((c) => (
                  <label key={c.id} className="flex items-center gap-1">
                    <input type="checkbox" checked={targets.includes(c.id)}
                      onChange={(e) => setTargets(e.target.checked
                        ? [...targets, c.id] : targets.filter((x) => x !== c.id))} />
                    {c.name}
                  </label>
                ))}
              </div>
            </fieldset>
          )}
        </div>
      </Advanced>
      <button className={clsx(btnGhost, "mt-2 text-xs")} disabled={ctx.busy === `plan-${sh.shot.id}`}
        onClick={() => void save()}>
        {ctx.busy === `plan-${sh.shot.id}` ? "Saving…" : hasPlan ? "Update plan" : "Save plan"}
      </button>
    </div>
  );
}

// ---------- shared generation-stage helpers ----------

function GenBadge({ row }: { row: WizardEntityRow | undefined }) {
  if (!row) return <Badge state="draft" />;
  return <Badge state={row.state} />;
}

// ---------- 8. keyframes ----------

export function KeyframesStage({ ctx }: { ctx: Ctx }) {
  const shots = ctx.board.flatMap((sc) => sc.shots.map((sh) => ({ sc, sh })));
  const rows = ctx.derived.entities.keyframes;
  const stage = ctx.derived.stages.find((s) => s.id === "keyframes");

  if (!shots.length) {
    return (<div><h2 className="font-semibold">Keyframes</h2>
      <p className="mt-1 text-sm text-muted">Add shots first.</p></div>);
  }
  return (
    <div>
      <h2 className="font-semibold">Keyframes</h2>
      <p className="mt-1 text-sm text-muted">
        Start and end stills per shot — prompts stay yours; the
        continuity-aware draft only fills on request. Generation here
        is <b>simulated</b> (mock adapters) and labeled as such.
      </p>
      <WarnList items={stage?.warnings ?? []} />
      <div className="mt-3 space-y-3">
        {shots.map(({ sc, sh }) => (
          <ShotKeyframes key={sh.shot.id} ctx={ctx} sc={sc} sh={sh}
            rows={rows.filter((r) => r.shot_id === sh.shot.id)} />
        ))}
      </div>
    </div>
  );
}

function ShotKeyframes({ ctx, sc, sh, rows }: {
  ctx: Ctx; sc: StoryboardScene; sh: StoryboardScene["shots"][number];
  rows: WizardEntityRow[];
}) {
  const [kfs, setKfs] = useState<Keyframe[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const reload = async () => {
    setKfs(await apiFetch<Keyframe[]>(
      `/api/projects/${ctx.pid}/shots/${sh.shot.id}/keyframes`));
  };
  useEffect(() => { void reload(); // eslint-disable-line react-hooks/exhaustive-deps
  }, [sh.shot.id, rows.length]);

  const create = async (role: "start" | "end") => {
    ctx.setBusy(`kf-${sh.shot.id}`);
    try {
      await ctx.commit("keyframe.create", { shot_id: sh.shot.id, role });
      await reload(); await ctx.refresh();
    } catch (e) { ctx.fail(e); return; }
    ctx.setBusy(null);
  };

  const generate = async (kf: Keyframe) => {
    ctx.setBusy(`gen-${kf.id}`);
    try {
      const p = await previewCommand(ctx.pid, "keyframe.generate",
        { keyframe_id: kf.id });
      const r = await ctx.commit("keyframe.generate", {
        keyframe_id: kf.id,
        expected_keyframe: p.resolved_keyframe,
        expected_prompt: p.resolved_prompt?.prompt_sha256,
      });
      if (r.job_id) await pollJob(ctx.pid, r.job_id, 120_000);
      await reload(); await ctx.refresh();
    } catch (e) { ctx.fail(e); return; }
    ctx.setBusy(null);
  };

  return (
    <div className="rounded-md border border-line bg-card p-3">
      <div className="flex items-center gap-2">
        <p className="flex-1 text-sm font-medium">
          {sh.shot.title} <span className="text-faint">· {sc.scene.title}</span>
        </p>
        <button className={btnGhost} disabled={ctx.busy === `kf-${sh.shot.id}`}
          onClick={() => void create("start")}>+ start</button>
        <button className={btnGhost} disabled={ctx.busy === `kf-${sh.shot.id}`}
          onClick={() => void create("end")}>+ end</button>
      </div>
      <div className="mt-2 space-y-2">
        {kfs.filter((k) => k.status === "active").map((kf) => {
          const row = rows.find((r) => r.id === kf.id);
          return (
            <div key={kf.id} className="rounded border border-line bg-panel p-2">
              <div className="flex items-center gap-2">
                <span className="text-[10px] uppercase text-faint">{kf.role}</span>
                <GenBadge row={row} />
                {row?.job_status === "running" && (
                  <span className="text-xs text-info">generating…</span>)}
                {kf.prompt_origin === "compose" && (
                  <span className="text-[10px] text-faint">composed draft</span>)}
                <span className="flex-1" />
                <Advanced mode={ctx.mode}>
                  {kf.result_asset_id && (
                    <button className={btnGhost} disabled={ctx.busy === `pin-${kf.id}`}
                      onClick={async () => {
                        ctx.setBusy(`pin-${kf.id}`);
                        try { await ctx.commit("keyframe.pin", { keyframe_id: kf.id }); await reload(); }
                        catch (e) { ctx.fail(e); return; } ctx.setBusy(null);
                      }}>Pin result</button>
                  )}
                </Advanced>
                <button className={btnGhost} disabled={ctx.busy === `gen-${kf.id}` || !kf.prompt.trim()}
                  title={kf.prompt.trim() ? "" : "Write a prompt first"}
                  onClick={() => void generate(kf)}>
                  {ctx.busy === `gen-${kf.id}` ? "Generating…" : "Generate (simulated)"}
                </button>
              </div>
              <textarea className={clsx(input, "mt-1 w-full text-xs")} rows={2}
                value={draft[kf.id] ?? kf.prompt}
                placeholder="Describe this frame"
                aria-label={`${kf.role} keyframe prompt`}
                onChange={(e) => setDraft({ ...draft, [kf.id]: e.target.value })} />
              {draft[kf.id] !== undefined && draft[kf.id] !== kf.prompt && (
                <div className="mt-1 flex gap-2">
                  <button className={clsx(btnGhost, "text-xs")} disabled={ctx.busy === `kfup-${kf.id}`}
                    onClick={async () => {
                      ctx.setBusy(`kfup-${kf.id}`);
                      try {
                        await ctx.commit("keyframe.update",
                          { keyframe_id: kf.id, prompt: draft[kf.id] });
                        const { [kf.id]: _d, ...rest } = draft; setDraft(rest);
                        await reload(); await ctx.refresh();
                      } catch (e) { ctx.fail(e); return; }
                      ctx.setBusy(null);
                    }}>Save prompt</button>
                  <button className="text-xs text-faint underline"
                    onClick={() => { const { [kf.id]: _d, ...rest } = draft; setDraft(rest); }}>
                    discard
                  </button>
                </div>
              )}
              <Advanced mode={ctx.mode}>
                <button className={clsx(btnGhost, "mt-1 text-xs")} disabled={ctx.busy === `cmp-${kf.id}`}
                  onClick={async () => {
                    ctx.setBusy(`cmp-${kf.id}`);
                    try {
                      const d = await apiFetch<{ prompt: string }>(
                        `/api/projects/${ctx.pid}/keyframes/${kf.id}/draft`);
                      setDraft({ ...draft, [kf.id]: d.prompt ?? "" });
                    } catch (e) { ctx.fail(e); return; }
                    ctx.setBusy(null);
                  }}>
                  Compose draft from scene & cast
                </button>
                {kf.negative_prompt !== undefined && (
                  <input className={clsx(input, "mt-1 w-full text-xs")}
                    defaultValue={kf.negative_prompt}
                    placeholder="Negative prompt (what to avoid)"
                    aria-label="Negative prompt"
                    onBlur={async (e) => {
                      if (e.target.value === (kf.negative_prompt ?? "")) return;
                      try {
                        await ctx.commit("keyframe.update",
                          { keyframe_id: kf.id, negative_prompt: e.target.value });
                        await reload();
                      } catch (err) { ctx.fail(err); }
                    }} />
                )}
              </Advanced>
            </div>
          );
        })}
        {!kfs.filter((k) => k.status === "active").length && (
          <p className="text-xs text-muted">No keyframes yet — add start/end above.</p>)}
      </div>
    </div>
  );
}

// ---------- 9. motion clips ----------

export function MotionStage({ ctx }: { ctx: Ctx }) {
  const shots = ctx.board.flatMap((sc) => sc.shots.map((sh) => ({ sc, sh })));
  const rows = ctx.derived.entities.clips;
  const stage = ctx.derived.stages.find((s) => s.id === "motion");
  if (!shots.length) {
    return (<div><h2 className="font-semibold">Motion clips</h2>
      <p className="mt-1 text-sm text-muted">Add shots first.</p></div>);
  }
  return (
    <div>
      <h2 className="font-semibold">Motion clips</h2>
      <p className="mt-1 text-sm text-muted">
        A clip turns your keyframes (or a prompt) into a moving shot.
        Output is <b>simulated</b> — mock video, clearly labeled.
      </p>
      <WarnList items={stage?.warnings ?? []} />
      <div className="mt-3 space-y-3">
        {shots.map(({ sc, sh }) => (
          <ShotClips key={sh.shot.id} ctx={ctx} sc={sc} sh={sh}
            rows={rows.filter((r) => r.shot_id === sh.shot.id)} />
        ))}
      </div>
    </div>
  );
}

function ShotClips({ ctx, sc, sh, rows }: {
  ctx: Ctx; sc: StoryboardScene; sh: StoryboardScene["shots"][number];
  rows: WizardEntityRow[];
}) {
  const [clips, setClips] = useState<MotionClip[]>([]);
  const [kfs, setKfs] = useState<Keyframe[]>([]);
  const [prompt, setPrompt] = useState("");
  const [frames, setFrames] = useState(48);
  const [neg, setNeg] = useState("");
  const reload = async () => {
    const [c, k] = await Promise.all([
      apiFetch<MotionClip[]>(`/api/projects/${ctx.pid}/shots/${sh.shot.id}/clips`),
      apiFetch<Keyframe[]>(`/api/projects/${ctx.pid}/shots/${sh.shot.id}/keyframes`),
    ]);
    setClips(c); setKfs(k);
  };
  useEffect(() => { void reload(); // eslint-disable-line react-hooks/exhaustive-deps
  }, [sh.shot.id, rows.length]);

  const start = kfs.find((k) => k.role === "start" && k.status === "active");
  const end = kfs.find((k) => k.role === "end" && k.status === "active");

  return (
    <div className="rounded-md border border-line bg-card p-3">
      <p className="text-sm font-medium">
        {sh.shot.title} <span className="text-faint">· {sc.scene.title}</span>
      </p>
      <div className="mt-2 space-y-2">
        {clips.filter((c) => c.status === "active").map((c) => {
          const row = rows.find((r) => r.id === c.id);
          return (
            <div key={c.id} className="flex items-center gap-2 rounded border border-line bg-panel p-2">
              <GenBadge row={row} />
              <span className="flex-1 truncate text-xs">{c.prompt || "(no prompt)"}</span>
              <span className="text-[10px] text-faint">
                {c.duration_frames ?? "?"}f @ {c.fps_num}/{c.fps_den}
              </span>
              <button className={btnGhost} disabled={ctx.busy === `mc-${c.id}` || !c.prompt.trim()}
                onClick={async () => {
                  ctx.setBusy(`mc-${c.id}`);
                  try {
                    const p = await previewCommand(ctx.pid, "clip.generate", { clip_id: c.id });
                    const r = await ctx.commit("clip.generate", {
                      clip_id: c.id,
                      expected_clip: p.resolved_clip,
                      expected_prompt: p.resolved_prompt?.prompt_sha256,
                    });
                    if (r.job_id) await pollJob(ctx.pid, r.job_id, 120_000);
                    await reload(); await ctx.refresh();
                  } catch (e) { ctx.fail(e); return; }
                  ctx.setBusy(null);
                }}>
                {ctx.busy === `mc-${c.id}` ? "Generating…" : "Generate (simulated)"}
              </button>
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="flex-1 text-xs">Motion prompt
          <input className={clsx(input, "mt-0.5 block w-full py-1 text-xs")} value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="e.g. camera drifts toward the window" />
        </label>
        <label className="text-xs">Frames
          <input type="number" className={clsx(input, "mt-0.5 block w-20 py-1 text-xs")}
            value={frames} min={1} onChange={(e) => setFrames(Number(e.target.value))} />
        </label>
        <button className={btnGhost} disabled={ctx.busy === `mcc-${sh.shot.id}`}
          onClick={async () => {
            ctx.setBusy(`mcc-${sh.shot.id}`);
            try {
              await ctx.commit("clip.create", {
                shot_id: sh.shot.id,
                start_keyframe_id: start?.id, end_keyframe_id: end?.id,
                prompt: prompt.trim() || `${sh.shot.title} motion`,
                duration_frames: frames,
                negative_prompt: neg || undefined,
              });
              setPrompt(""); setNeg("");
              await reload(); await ctx.refresh();
            } catch (e) { ctx.fail(e); return; }
            ctx.setBusy(null);
          }}>+ Clip</button>
      </div>
      <Advanced mode={ctx.mode}>
        <input className={clsx(input, "mt-1 w-full text-xs")} value={neg}
          onChange={(e) => setNeg(e.target.value)}
          placeholder="Negative prompt (optional)" aria-label="Clip negative prompt" />
        <p className="mt-1 text-[10px] text-faint">
          Keyframes bound: start {start ? "✓" : "—"}, end {end ? "✓" : "—"}.
          Camera/motion JSON fields and seeds live in{" "}
          <InspectorLink openTab={ctx.openTab} tab="storyboard">Storyboard</InspectorLink>.
        </p>
      </Advanced>
    </div>
  );
}

// ---------- 10. dialogue & audio ----------

export function DialogueStage({ ctx, doc }: { ctx: Ctx; doc: SpDoc | null }) {
  const [segs, setSegs] = useState<DialogueSegment[]>([]);
  const rows = ctx.derived.entities.dialogue;
  const stage = ctx.derived.stages.find((s) => s.id === "dialogue");
  const reload = async () => {
    setSegs(await apiFetch<DialogueSegment[]>(`/api/projects/${ctx.pid}/dialogue`));
    await ctx.refresh();
  };
  useEffect(() => { void reload(); // eslint-disable-line react-hooks/exhaustive-deps
  }, [ctx.pid]);

  const dialogElements = (doc?.scenes ?? []).flatMap((sc) =>
    sc.elements.filter((e) => e.kind === "dialogue").map((e) => ({ sc, e })));
  const unbound = dialogElements.filter(({ e }) =>
    !segs.some((s) => s.element_id === e.id));

  return (
    <div>
      <h2 className="font-semibold">Dialogue & audio</h2>
      <p className="mt-1 text-sm text-muted">
        Spoken lines become audio segments placed on the timeline.
        Voices are <b>simulated</b> tone — never spoken dialogue.
      </p>
      <WarnList items={stage?.warnings ?? []} />
      {unbound.length > 0 && (
        <div className="mt-3 rounded-md border border-line bg-card p-3">
          <p className="text-sm font-medium">
            {unbound.length} screenplay line(s) have no audio segment yet
          </p>
          <button className={clsx(btnGhost, "mt-2 text-xs")} disabled={ctx.busy === "segall"}
            onClick={async () => {
              ctx.setBusy("segall");
              try {
                for (const { sc, e } of unbound) {
                  await ctx.commit("dialogue.create", {
                    sp_scene_id: sc.id, element_id: e.id,
                    character_id: e.character_id, text: e.text,
                  });
                }
                await reload();
              } catch (err) { ctx.fail(err); return; }
              ctx.setBusy(null);
            }}>
            Create segments for all
          </button>
        </div>
      )}
      <div className="mt-3 space-y-2">
        {segs.filter((s) => s.status === "active").map((s) => {
          const row = rows.find((r) => r.id === s.id);
          return (
            <div key={s.id} className="rounded-md border border-line bg-card p-3">
              <div className="flex items-center gap-2">
                <GenBadge row={row} />
                <span className="text-xs font-medium">
                  {s.character_id
                    ? ctx.characters.find((c) => c.id === s.character_id)?.name ?? "(unknown)"
                    : "(no speaker)"}
                </span>
                <span className="flex-1 truncate text-xs text-muted">“{s.text}”</span>
                <button className={btnGhost}
                  disabled={ctx.busy === `dg-${s.id}` || !s.character_id}
                  title={s.character_id ? "" : "Assign a speaker first"}
                  onClick={async () => {
                    ctx.setBusy(`dg-${s.id}`);
                    try {
                      const p = await previewCommand(ctx.pid, "dialogue.generate", { segment_id: s.id });
                      const r = await ctx.commit("dialogue.generate", {
                        segment_id: s.id,
                        expected_dialogue: p.resolved_dialogue,
                      });
                      if (r.job_id) await pollJob(ctx.pid, r.job_id, 120_000);
                      await reload();
                    } catch (e) { ctx.fail(e); return; }
                    ctx.setBusy(null);
                  }}>
                  {ctx.busy === `dg-${s.id}` ? "Generating…" : "Generate (simulated)"}
                </button>
              </div>
              <Advanced mode={ctx.mode}>
                <div className="mt-2 flex gap-2">
                  <input className={clsx(input, "flex-1 text-xs")} defaultValue={s.direction}
                    placeholder="Delivery direction (e.g. whispered)"
                    aria-label="Delivery direction"
                    onBlur={async (e) => {
                      if (e.target.value === s.direction) return;
                      try {
                        await ctx.commit("dialogue.update",
                          { segment_id: s.id, direction: e.target.value });
                        await reload();
                      } catch (err) { ctx.fail(err); }
                    }} />
                  <select className={clsx(input, "w-40 text-xs")} value={s.character_id ?? ""}
                    aria-label="Speaker"
                    onChange={async (e) => {
                      try {
                        await ctx.commit("dialogue.update",
                          { segment_id: s.id, character_id: e.target.value || null });
                        await reload();
                      } catch (err) { ctx.fail(err); }
                    }}>
                    <option value="">(no speaker)</option>
                    {ctx.characters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                  </select>
                </div>
              </Advanced>
            </div>
          );
        })}
        {!segs.filter((s) => s.status === "active").length && !unbound.length && (
          <p className="text-sm text-muted">No dialogue yet — add dialogue lines in the screenplay or create segments in the Dialogue section of the workspace.</p>)}
      </div>
      <Advanced mode={ctx.mode}>
        <InspectorLink openTab={ctx.openTab} tab="screenplay">
          Screenplay dialogue editor
        </InspectorLink>
      </Advanced>
    </div>
  );
}

// ---------- 11. first-cut plan ----------

export function PlanStage({ ctx }: { ctx: Ctx }) {
  const [proposal, setProposal] = useState<CommandProposal | null>(null);
  const [name, setName] = useState("First cut");
  const planStage = ctx.derived.stages.find((s) => s.id === "assembly_plan");
  const latest = ctx.derived.plans[0];

  const ensureTimeline = async (): Promise<string> => {
    if (ctx.state.timeline_id) return ctx.state.timeline_id;
    let t = ctx.timelines.find((x) => x.status === "active");
    if (!t) {
      await ctx.commit("timeline.create", { name: "First cut" });
      const tls = await apiFetch<Timeline[]>(`/api/projects/${ctx.pid}/timelines`);
      t = tls.find((x) => x.status === "active");
    }
    if (!t) throw new Error("no timeline available");
    await ctx.save({ ...ctx.state, timeline_id: t.id });
    return t.id;
  };

  return (
    <div>
      <h2 className="font-semibold">First-cut plan</h2>
      <p className="mt-1 text-sm text-muted">
        A deterministic proposal lining scenes, shots, clips, and audio
        onto the timeline. Nothing changes until you review and apply.
      </p>
      {latest && (
        <p className="mt-2 text-sm">
          Latest plan: <b>{latest.name}</b> — {latest.items_total} item(s),{" "}
          {latest.item_counts.missing ?? 0} flagged missing,{" "}
          {latest.applied ? "applied" : "not applied"}.
        </p>
      )}
      <div className="mt-3 flex items-center gap-2">
        <input className={clsx(input, "w-48")} value={name}
          onChange={(e) => setName(e.target.value)} aria-label="Plan name" />
        <button className={btn} disabled={ctx.busy === "plan"}
          onClick={async () => {
            ctx.setBusy("plan");
            try {
              const tid = await ensureTimeline();
              const p = await previewCommand(ctx.pid, "assembly.plan",
                { timeline_id: tid, name });
              setProposal(p);
            } catch (e) { ctx.fail(e); }
            ctx.setBusy(null);
          }}>
          {ctx.busy === "plan" ? "Computing…" : "Preview plan"}
        </button>
        {proposal && (
          <button className={btnWarn} disabled={ctx.busy === "plan"}
            onClick={async () => {
              ctx.setBusy("plan");
              try {
                const tid = await ensureTimeline();
                await ctx.commit("assembly.plan", { timeline_id: tid, name });
                const plans = await apiFetch<AssemblyPlan[]>(
                  `/api/projects/${ctx.pid}/assemblies?timeline_id=${tid}`);
                const p = plans[0];
                if (p) await ctx.save({ ...ctx.state, timeline_id: tid, plan_id: p.id });
                setProposal(null);
                await ctx.refresh();
              } catch (e) { ctx.fail(e); return; }
              ctx.setBusy(null);
            }}>
            Create plan
          </button>
        )}
      </div>
      {proposal && (
        <div className="mt-2 max-h-56 overflow-y-auto rounded border border-line bg-panel p-2 text-xs">
          <pre className="whitespace-pre-wrap">
            {JSON.stringify(proposal.resolved_assembly ?? proposal.summary ?? proposal, null, 2).slice(0, 4000)}
          </pre>
        </div>
      )}
      <WarnList items={planStage?.warnings ?? []} />
    </div>
  );
}

// ---------- 12. review & apply ----------

export function ReviewStage({ ctx }: { ctx: Ctx }) {
  const [plan, setPlan] = useState<AssemblyPlan | null>(null);
  const stage = ctx.derived.stages.find((s) => s.id === "assembly_apply");
  const planId = ctx.state.plan_id ?? ctx.derived.plans[0]?.id;

  const reload = async () => {
    if (planId) {
      setPlan(await apiFetch<AssemblyPlan>(
        `/api/projects/${ctx.pid}/assemblies/${planId}`));
    }
    await ctx.refresh();
  };
  useEffect(() => { void reload(); // eslint-disable-line react-hooks/exhaustive-deps
  }, [planId]);

  if (!planId) {
    return (<div><h2 className="font-semibold">Review & apply</h2>
      <p className="mt-1 text-sm text-muted">Create a first-cut plan on the previous step.</p></div>);
  }
  if (!plan) return <p className="text-sm text-muted">Loading plan…</p>;
  const items = plan.items ?? [];
  const pending = items.filter((i) => i.review_status === "pending");

  const review = async (itemIds: string[], status: string) => {
    try {
      await ctx.commit("assembly.review",
        { plan_id: plan.id, item_ids: itemIds, status });
      await reload();
    } catch (e) { ctx.fail(e); }
  };

  return (
    <div>
      <h2 className="font-semibold">Review & apply</h2>
      <p className="mt-1 text-sm text-muted">
        Approve or reject each proposed item. Applying builds the
        timeline non-destructively — your manual edits are preserved
        and broad replacement always asks first.
      </p>
      <div className="mt-3 space-y-1">
        {items.map((it) => (
          <div key={it.id} className="flex items-center gap-2 rounded border border-line bg-panel p-2 text-xs">
            <Badge state={it.review_status === "approved" ? "approved" : it.review_status} />
            <span className="w-16 text-faint">{it.kind}</span>
            <span className="flex-1 truncate">
              {it.shot_id ? shotTitle(ctx.board, it.shot_id) : it.key}
              {it.flags?.missing ? ` — missing: ${it.flags.missing}` : ""}
            </span>
            {it.review_status === "pending" ? (
              <>
                <button className={btnGhost} onClick={() => void review([it.id], "approved")}>Approve</button>
                <button className={btnGhost} onClick={() => void review([it.id], "rejected")}>Reject</button>
                <button className={btnGhost} onClick={() => void review([it.id], "skipped")}>Skip</button>
              </>
            ) : (
              <button className="text-xs text-faint underline"
                onClick={() => void review([it.id], "pending")}>reset</button>
            )}
          </div>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button className={btn} disabled={ctx.busy === "apply" || pending.length > 0}
          title={pending.length ? `${pending.length} item(s) still pending review` : ""}
          onClick={async () => {
            ctx.setBusy("apply");
            try {
              await ctx.commit("assembly.apply", { timeline_id: plan.timeline_id });
              await reload();
            } catch (e) { ctx.fail(e); return; }
            ctx.setBusy(null);
          }}>
          {ctx.busy === "apply" ? "Applying…" : `Apply ${items.length - pending.length} approved item(s)`}
        </button>
        {pending.length > 0 && (
          <button className={btnGhost}
            onClick={() => void review(pending.map((i) => i.id), "approved")}>
            Approve all {pending.length}
          </button>
        )}
      </div>
      <WarnList items={stage?.warnings ?? []} />
    </div>
  );
}

// ---------- 13. edit ----------

export function EditStage({ ctx }: { ctx: Ctx }) {
  const [detail, setDetail] = useState<TimelineDetail | null>(null);
  const stage = ctx.derived.stages.find((s) => s.id === "edit");
  const tid = ctx.state.timeline_id ?? ctx.timelines[0]?.id;
  useEffect(() => {
    if (tid) {
      void apiFetch<TimelineDetail>(`/api/projects/${ctx.pid}/timelines/${tid}`)
        .then(setDetail).catch(() => setDetail(null));
    }
  }, [tid, ctx.pid]);
  return (
    <div>
      <h2 className="font-semibold">Edit film</h2>
      <p className="mt-1 text-sm text-muted">
        {stage?.summary ?? "The full timeline editor has every editing control — trims, moves, lanes, fades, mixing."}
      </p>
      {detail && (
        <div className="mt-3 rounded-md border border-line bg-card p-3 text-sm">
          <p><b>{detail.name}</b> — {detail.clips.length} clip(s), {detail.tracks.length} lane(s)</p>
          <p className="mt-1 text-xs text-muted">
            Undo: {detail.undo_label ?? "nothing"} · Redo: {detail.redo_label ?? "nothing"}
          </p>
        </div>
      )}
      <button className={clsx(btn, "mt-3")} onClick={() => ctx.openTab("timeline")}>
        Open timeline editor
      </button>
      <Advanced mode={ctx.mode}>
        <p className="text-xs text-muted">
          Audio lanes, gain, automation, fades and undo/redo are all in
          the editor — the wizard never replaces it.
        </p>
      </Advanced>
    </div>
  );
}

// ---------- 14. export settings ----------

const PROFILES = [
  { id: "720p", label: "720p — smaller file, quick share" },
  { id: "1080p", label: "1080p — full HD" },
  { id: "source", label: "Source — native resolution/fps" },
];

export function ExportConfigStage({ ctx }: { ctx: Ctx }) {
  const [profile, setProfile] = useState("720p");
  const stage = ctx.derived.stages.find((s) => s.id === "export_config");
  return (
    <div>
      <h2 className="font-semibold">Export settings</h2>
      <p className="mt-1 text-sm text-muted">
        Pick the output profile. The export renders through the
        project's own FFmpeg pipeline — real video, from your clips.
      </p>
      <div className="mt-3 space-y-1">
        {PROFILES.map((p) => (
          <label key={p.id} className="flex items-center gap-2 rounded border border-line bg-card p-2 text-sm">
            <input type="radio" name="profile" checked={profile === p.id}
              onChange={() => setProfile(p.id)} />
            {p.label}
          </label>
        ))}
      </div>
      <button className={clsx(btn, "mt-3")}
        onClick={async () => {
          await ctx.save({ ...ctx.state, export_profile: profile });
        }}>
        Use {profile}
      </button>
      <WarnList items={stage?.warnings ?? []} />
    </div>
  );
}

// ---------- 15. export & verify ----------

export function ExportStage({ ctx }: { ctx: Ctx }) {
  const [job, setJob] = useState<Job | null>(null);
  const [probe, setProbe] = useState<Record<string, unknown> | null>(null);
  const stage = ctx.derived.stages.find((s) => s.id === "export");
  const tid = ctx.state.timeline_id ?? ctx.timelines[0]?.id;
  const profile = ctx.state.export_profile || "720p";
  const lastJob = stage?.counts?.job_id
    ? ctx.jobs[stage.counts.job_id as string] ?? null : null;
  const active = job ?? lastJob;

  const verify = async (assetId: string) => {
    const items = await apiFetch<{ items: { id: string; media_json?: Record<string, unknown>; name: string }[] }>(
      `/api/projects/${ctx.pid}/assets?limit=500`);
    const a = items.items.find((x) => x.id === assetId);
    setProbe(a ? { name: a.name, ...(a.media_json ?? {}) } : { missing: true });
  };

  useEffect(() => {
    const out = stage?.counts?.output_asset_id as string | undefined;
    if (out) void verify(out); // eslint-disable-line react-hooks/exhaustive-deps
  }, [stage?.counts?.output_asset_id]);

  return (
    <div>
      <h2 className="font-semibold">Export & verify</h2>
      <p className="mt-1 text-sm text-muted">
        Renders <b>{profile}</b> from the timeline and checks the
        output file's streams, duration, and frame rate.
      </p>
      {!tid && <p className="mt-2 text-sm text-amber">Build the film first — there is no timeline to export.</p>}
      {tid && (
        <button className={clsx(btn, "mt-3")} disabled={ctx.busy === "export"}
          onClick={async () => {
            ctx.setBusy("export");
            try {
              const p = await previewCommand(ctx.pid, "timeline.export",
                { timeline_id: tid, profile });
              const rt = p.resolved_timeline;
              const r = await ctx.commit("timeline.export", {
                timeline_id: tid, profile,
                expected_timeline: rt ? { id: rt.id, revision: rt.revision } : undefined,
                expected_edl_sha: rt?.edl_sha256,
              });
              if (r.job_id) {
                await ctx.save({ ...ctx.state, export_job_id: r.job_id });
                const done = await pollJob(ctx.pid, r.job_id, 600_000, setJob);
                setJob(done);
                if (done.output_asset_id) await verify(done.output_asset_id);
                await ctx.save({ ...ctx.state, export_job_id: r.job_id, done: true });
              }
              await ctx.refresh();
            } catch (e) { ctx.fail(e); return; }
            ctx.setBusy(null);
          }}>
          {ctx.busy === "export" ? "Exporting…" : `Export ${profile}`}
        </button>
      )}
      {active && (
        <div className="mt-3 rounded-md border border-line bg-card p-3 text-sm">
          <div className="flex items-center gap-2">
            <Badge state={active.status === "succeeded" ? "approved" : active.status} />
            <span>{active.message || active.status}</span>
            {active.status === "running" && (
              <button className={btnGhost}
                onClick={() => void apiFetch(`/api/jobs/${active.id}/cancel`, { method: "POST" }).catch(() => {})}>
                Cancel
              </button>
            )}
          </div>
        </div>
      )}
      {probe && (
        <div className="mt-3 rounded-md border border-line bg-card p-3 text-xs">
          <p className="font-medium">Output check</p>
          <pre className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap text-faint">
            {JSON.stringify(probe, null, 2).slice(0, 3000)}
          </pre>
        </div>
      )}
      <WarnList items={stage?.warnings ?? []} />
      {stage?.status === "done" && (
        <p className="mt-3 text-sm text-teal">
          Your film exported. You can keep editing and export again any time.
        </p>
      )}
    </div>
  );
}
