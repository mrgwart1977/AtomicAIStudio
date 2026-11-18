import { useCallback, useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { apiFetch } from "../../api/client";
import { commitCommand, previewCommand } from "../../api/commands";
import { DialogueSection } from "./Dialogue";
import type { CommandProposal, Entity, StoryboardScene } from "../../api/types";
import { useApp } from "../../stores/appStore";

/** A1 — screenplay editor + deterministic shot-list scaffold.

 *  Left: screenplay list. Center: ordered scene cards (heading, action,
 *  location, notes, element list). Right: the linked storyboard shots.
 *  Every mutation flows preview → approve → commit; sync shows its
 *  deterministic diff with per-shot approval checkboxes for anything
 *  that would touch a manually-edited shot or remove a shot. */

interface SpElement {
  id: string;
  position: number;
  kind: "action" | "dialogue" | "note";
  character_id: string | null;
  text: string;
  parenthetical: string;
}

interface SpScene {
  id: string;
  position: number;
  heading: string;
  action: string;
  notes: string;
  location_id: string | null;
  scene_id: string | null;
  elements: SpElement[];
}

interface Screenplay {
  id: string;
  title: string;
  description: string;
  position: number;
  status: string;
  revision: number;
  scenes?: SpScene[];
}

interface SyncItem {
  shot_id?: string;
  group_key?: string;
  shot_title?: string;
  proposed?: { title?: string };
  manually_edited?: boolean;
}

interface SyncSceneDiff {
  sp_scene_id: string;
  heading: string;
  add: SyncItem[];
  update: SyncItem[];
  remove: SyncItem[];
  manual_edit: SyncItem[];
  unchanged: SyncItem[];
}

interface SyncPlan {
  scenes: SyncSceneDiff[];
  counts: Record<string, number>;
}

const KIND_LABEL: Record<string, string> = {
  action: "Action", dialogue: "Dialogue", note: "Note",
};

export function ScreenplayPage() {
  const project = useApp((s) => s.currentProject());
  const [screenplays, setScreenplays] = useState<Screenplay[]>([]);
  const [selId, setSelId] = useState<string | null>(null);
  const [doc, setDoc] = useState<Screenplay | null>(null);
  const [characters, setCharacters] = useState<Entity[]>([]);
  const [locations, setLocations] = useState<Entity[]>([]);
  const [board, setBoard] = useState<StoryboardScene[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [proposal, setProposal] = useState<CommandProposal | null>(null);
  const [plan, setPlan] = useState<SyncPlan | null>(null);
  const [approveOw, setApproveOw] = useState<Set<string>>(new Set());
  const [approveRm, setApproveRm] = useState<Set<string>>(new Set());
  const [pendingType, setPendingType] = useState<"shotlist.scaffold" | "shotlist.sync">("shotlist.scaffold");

  const pid = project?.id || "";

  const refresh = useCallback(async () => {
    if (!pid) return;
    const [sps, chars, locs, bd] = await Promise.all([
      apiFetch<Screenplay[]>(`/api/projects/${pid}/screenplays`),
      apiFetch<Entity[]>(`/api/projects/${pid}/character`),
      apiFetch<Entity[]>(`/api/projects/${pid}/location`),
      apiFetch<StoryboardScene[]>(`/api/projects/${pid}/storyboard`),
    ]);
    setScreenplays(sps); setCharacters(chars); setLocations(locs); setBoard(bd);
    if (selId) {
      try {
        setDoc(await apiFetch<Screenplay>(`/api/projects/${pid}/screenplays/${selId}`));
      } catch { setDoc(null); setSelId(null); }
    }
  }, [pid, selId]);

  useEffect(() => { refresh().catch((e) => setError(String(e))); },
    [refresh]);

  async function select(id: string) {
    setSelId(id); setPlan(null); setProposal(null);
    setDoc(await apiFetch<Screenplay>(`/api/projects/${pid}/screenplays/${id}`));
  }

  async function commit(ct: string, payload: Record<string, unknown>) {
    setBusy(true); setError("");
    try {
      await commitCommand(pid, project!.revision, ct, payload, "screenplay");
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function createScreenplay() {
    if (!newTitle.trim()) { setError("Give the screenplay a title first."); return; }
    await commit("screenplay.create", { title: newTitle.trim() });
    setNewTitle("");
    const sps = await apiFetch<Screenplay[]>(`/api/projects/${pid}/screenplays`);
    const last = sps[sps.length - 1];
    if (last) await select(last.id);
  }

  async function saveScene(sc: SpScene) {
    await commit("screenplay_scene.update", {
      sp_scene_id: sc.id,
      heading: sc.heading, action: sc.action, notes: sc.notes,
      location_id: sc.location_id,
      elements: sc.elements.map((e) => ({
        // tmp-* ids are unsaved UI rows — the backend creates them;
        // omitting id keeps identity semantics honest.
        ...(e.id.startsWith("tmp-") ? {} : { id: e.id }),
        kind: e.kind, character_id: e.character_id,
        text: e.text, parenthetical: e.parenthetical,
      })),
    });
  }

  async function runShotlist(type: "shotlist.scaffold" | "shotlist.sync") {
    setBusy(true); setError("");
    try {
      const p = await previewCommand(pid, type, { screenplay_id: doc!.id });
      setProposal(p);
      setPlan((p as { shotlist_diff?: SyncPlan }).shotlist_diff || null);
      setPendingType(type);
      setApproveOw(new Set()); setApproveRm(new Set());
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  async function approvePlan() {
    setBusy(true); setError("");
    try {
      await commitCommand(pid, project!.revision, pendingType, {
        screenplay_id: doc!.id,
        approve_overwrite: [...approveOw],
        approve_remove: [...approveRm],
      }, "screenplay");
      setProposal(null); setPlan(null);
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }

  function patchScene(id: string, patch: Partial<SpScene>) {
    setDoc((d) => d && ({
      ...d, scenes: (d.scenes || []).map((s) =>
        s.id === id ? { ...s, ...patch } : s),
    }));
  }
  function patchElement(sid: string, eid: string | undefined, patch: Partial<SpElement>) {
    setDoc((d) => d && ({
      ...d, scenes: (d.scenes || []).map((s) => s.id !== sid ? s : {
        ...s, elements: s.elements.map((e) => e.id === eid ? { ...e, ...patch } : e),
      }),
    }));
  }

  if (!project) return null;
  const shotsForScene = (sceneId: string | null) => {
    const sc = board.find((b) => b.scene.id === sceneId);
    return sc ? sc.shots : [];
  };

  return (
    <div className="flex h-full min-h-0 gap-4 p-4">
      {/* ---------- left: screenplay list ---------- */}
      <aside className="w-56 shrink-0 space-y-2" aria-label="Screenplays">
        <h2 className="text-sm font-semibold text-fg">Screenplays</h2>
        <div className="flex gap-1">
          <input
            value={newTitle} onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void createScreenplay()}
            placeholder="New screenplay…" aria-label="New screenplay title"
            className="min-w-0 flex-1 rounded border border-line bg-card px-2 py-1 text-sm" />
          <button onClick={() => void createScreenplay()} disabled={busy}
            className="rounded bg-teal px-2 py-1 text-sm text-black disabled:opacity-50">+</button>
        </div>
        <ul className="space-y-1">
          {screenplays.map((sp) => (
            <li key={sp.id}>
              <button onClick={() => void select(sp.id)}
                aria-current={selId === sp.id ? "page" : undefined}
                className={`w-full rounded px-2 py-1.5 text-left text-sm ${selId === sp.id ? "bg-teal/20 text-teal" : "text-muted hover:text-fg"}`}>
                {sp.title}
              </button>
            </li>
          ))}
        </ul>
        {doc && (
          <div className="space-y-1 border-t border-line pt-2">
            <button onClick={() => void runShotlist("shotlist.scaffold")} disabled={busy}
              className="w-full rounded bg-teal px-2 py-1.5 text-sm text-black disabled:opacity-50">
              Generate shot list</button>
            <button onClick={() => void runShotlist("shotlist.sync")} disabled={busy}
              className="w-full rounded border border-line px-2 py-1.5 text-sm text-fg disabled:opacity-50">
              Sync shot list</button>
            <div className="flex gap-1">
              <button onClick={() => void commit("shotlist.undo", {})} disabled={busy}
                className="flex-1 rounded border border-line px-2 py-1 text-xs text-muted disabled:opacity-50">Undo</button>
              <button onClick={() => void commit("shotlist.redo", {})} disabled={busy}
                className="flex-1 rounded border border-line px-2 py-1 text-xs text-muted disabled:opacity-50">Redo</button>
            </div>
          </div>
        )}
      </aside>

      {/* ---------- center: scene editor ---------- */}
      <main className="min-w-0 flex-1 space-y-3 overflow-y-auto" aria-label="Screenplay editor">
        {error && <p role="alert" className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</p>}
        {!doc && <p className="text-sm text-muted">Create or select a screenplay to begin. Everything here works without any AI models.</p>}
        {doc && (
          <>
            <header className="flex items-baseline justify-between">
              <h1 className="text-lg font-semibold text-fg">{doc.title}</h1>
              <span className="text-xs text-muted">revision {doc.revision}</span>
            </header>
            <button onClick={() => void commit("screenplay_scene.create",
              { screenplay_id: doc.id, heading: `INT. NEW SCENE` })}
              className="rounded border border-dashed border-line px-3 py-1.5 text-sm text-muted hover:text-fg">
              + Add scene
            </button>
            {(doc.scenes || []).map((sc, i) => (
              <section key={sc.id} className="rounded-lg border border-line bg-card p-3" aria-label={`Scene ${i + 1}`}>
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-xs text-muted">{i + 1}.</span>
                  <input value={sc.heading} aria-label="Scene heading"
                    onChange={(e) => patchScene(sc.id, { heading: e.target.value })}
                    className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-1 font-mono text-sm" />
                  <select value={sc.location_id || ""} aria-label="Location"
                    onChange={(e) => patchScene(sc.id, { location_id: e.target.value || null })}
                    className="rounded border border-line bg-bg px-2 py-1 text-sm">
                    <option value="">— location —</option>
                    {locations.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
                  </select>
                  <button onClick={() => void commit("screenplay_scene.move", { sp_scene_id: sc.id, to_index: i - 1 })}
                    disabled={i === 0 || busy} aria-label="Move scene up"
                    className="rounded border border-line px-1.5 text-muted disabled:opacity-40">↑</button>
                  <button onClick={() => void commit("screenplay_scene.move", { sp_scene_id: sc.id, to_index: i + 1 })}
                    disabled={i === (doc.scenes || []).length - 1 || busy} aria-label="Move scene down"
                    className="rounded border border-line px-1.5 text-muted disabled:opacity-40">↓</button>
                  <button onClick={() => void commit("screenplay_scene.delete", { sp_scene_id: sc.id })}
                    aria-label="Delete scene"
                    className="rounded border border-red-500/40 px-1.5 text-red-400">×</button>
                </div>
                <textarea value={sc.action} aria-label="Scene action"
                  onChange={(e) => patchScene(sc.id, { action: e.target.value })}
                  placeholder="Scene action — what happens here…" rows={2}
                  className="mb-2 w-full rounded border border-line bg-bg px-2 py-1 text-sm" />
                <ol className="mb-2 space-y-1.5">
                  {sc.elements.map((el, ei) => (
                    <li key={el.id} className="flex items-start gap-2">
                      <select value={el.kind} aria-label={`Element ${ei + 1} kind`}
                        onChange={(e) => patchElement(sc.id, el.id, { kind: e.target.value as SpElement["kind"], character_id: null })}
                        className="w-24 shrink-0 rounded border border-line bg-bg px-1 py-1 text-xs">
                        {Object.entries(KIND_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
                      </select>
                      {el.kind === "dialogue" && (
                        <select value={el.character_id || ""} aria-label="Speaker"
                          onChange={(e) => patchElement(sc.id, el.id, { character_id: e.target.value || null })}
                          className="w-32 shrink-0 rounded border border-line bg-bg px-1 py-1 text-xs">
                          <option value="">— speaker —</option>
                          {characters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                        </select>
                      )}
                      <div className="min-w-0 flex-1 space-y-1">
                        <input value={el.text} aria-label={KIND_LABEL[el.kind]}
                          onChange={(e) => patchElement(sc.id, el.id, { text: e.target.value })}
                          placeholder={el.kind === "dialogue" ? "What they say…" : el.kind === "action" ? "Action beat…" : "Note…"}
                          className="w-full rounded border border-line bg-bg px-2 py-1 text-sm" />
                        {el.kind === "dialogue" && (
                          <input value={el.parenthetical} aria-label="Parenthetical"
                            onChange={(e) => patchElement(sc.id, el.id, { parenthetical: e.target.value })}
                            placeholder="(how it's said — optional)"
                            className="w-full rounded border border-line bg-bg px-2 py-0.5 text-xs text-muted" />
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
                <div className="flex items-center gap-2">
                  <button onClick={() => patchScene(sc.id, {
                    elements: [...sc.elements, {
                      id: `tmp-${crypto.randomUUID()}`, position: sc.elements.length,
                      kind: "dialogue", character_id: null, text: "", parenthetical: "",
                    }],
                  })} className="rounded border border-line px-2 py-0.5 text-xs text-muted">+ dialogue</button>
                  <button onClick={() => patchScene(sc.id, {
                    elements: [...sc.elements, {
                      id: `tmp-${crypto.randomUUID()}`, position: sc.elements.length,
                      kind: "action", character_id: null, text: "", parenthetical: "",
                    }],
                  })} className="rounded border border-line px-2 py-0.5 text-xs text-muted">+ action</button>
                  <input value={sc.notes} aria-label="Scene notes"
                    onChange={(e) => patchScene(sc.id, { notes: e.target.value })}
                    placeholder="Notes…"
                    className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-1 text-xs text-muted" />
                  <button onClick={() => void saveScene(sc)} disabled={busy}
                    className="rounded bg-teal px-3 py-1 text-xs font-medium text-black disabled:opacity-50">
                    Save scene</button>
                </div>
                {/* ---- A5 dialogue segments for this screenplay scene ---- */}
                <DialogueSection
                  projectId={pid}
                  spSceneId={sc.id}
                  run={async (t, p) => { await commit(t, p); return true; }}
                />
              </section>
            ))}
          </>
        )}
      </main>

      {/* ---------- right: linked shot list ---------- */}
      {doc && (
        <aside className="w-64 shrink-0 space-y-2 overflow-y-auto" aria-label="Shot list">
          <h2 className="text-sm font-semibold text-fg">Shot list</h2>
          {(doc.scenes || []).map((sc) => {
            const shots = shotsForScene(sc.scene_id);
            return (
              <div key={sc.id} className="rounded border border-line bg-card p-2">
                <p className="mb-1 truncate font-mono text-xs text-muted">{sc.heading || "(no heading)"}</p>
                {sc.scene_id == null && <p className="text-xs text-muted">No shots yet — generate the shot list.</p>}
                {sc.scene_id != null && shots.length === 0 && <p className="text-xs text-muted">Linked scene — no shots.</p>}
                <ol className="space-y-1">
                  {shots.map((entry, si) => (
                    <li key={entry.shot.id} className="rounded bg-bg px-2 py-1 text-xs">
                      <span className="text-teal">{si + 1}.</span>{" "}
                      <span className="text-fg">{entry.shot.title || entry.shot.shot_type}</span>
                      <span className="ml-1 text-muted">[{entry.shot.shot_type}]</span>
                      {entry.shot.status === "archived" && <span className="ml-1 text-red-400">(archived)</span>}
                    </li>
                  ))}
                </ol>
              </div>
            );
          })}
        </aside>
      )}

      {/* ---------- sync approval dialog ---------- */}
      <Dialog.Root open={!!proposal} onOpenChange={(o) => !o && setProposal(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60" />
          <Dialog.Content className="fixed left-1/2 top-1/2 max-h-[80vh] w-[560px] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-line bg-card p-4" aria-describedby={undefined}>
            <Dialog.Title className="mb-2 text-sm font-semibold text-fg">
              {pendingType === "shotlist.scaffold" ? "Generate shot list" : "Sync shot list"} — review
            </Dialog.Title>
            <p className="mb-3 text-sm text-muted">{proposal?.summary}</p>
            {proposal?.warnings.map((w, i) => (
              <p key={i} className="mb-1 rounded border border-amber/40 bg-amber/10 px-2 py-1 text-xs text-amber">{w}</p>
            ))}
            {plan && plan.scenes.map((sd) => (
              <div key={sd.sp_scene_id} className="mb-2 rounded border border-line p-2">
                <p className="mb-1 font-mono text-xs text-muted">{sd.heading || "scene"}</p>
                {sd.add.map((a, i) => (
                  <p key={i} className="text-xs text-emerald-300">+ {a.proposed?.title || a.group_key} <span className="text-muted">(new)</span></p>
                ))}
                {sd.update.map((u) => (
                  <p key={u.shot_id} className="text-xs text-sky-300">~ {u.proposed?.title} <span className="text-muted">(update)</span></p>
                ))}
                {sd.manual_edit.map((m) => (
                  <label key={m.shot_id} className="flex items-center gap-2 text-xs text-amber">
                    <input type="checkbox" checked={approveOw.has(m.shot_id!)}
                      onChange={(e) => setApproveOw((s) => { const n = new Set(s); e.target.checked ? n.add(m.shot_id!) : n.delete(m.shot_id!); return n; })} />
                    ⚠ “{m.shot_title}” has manual edits — overwrite?
                  </label>
                ))}
                {sd.remove.map((r) => (
                  <label key={r.shot_id} className="flex items-center gap-2 text-xs text-red-300">
                    <input type="checkbox" checked={approveRm.has(r.shot_id!)}
                      onChange={(e) => setApproveRm((s) => { const n = new Set(s); e.target.checked ? n.add(r.shot_id!) : n.delete(r.shot_id!); return n; })} />
                    − “{r.shot_title}”{r.manually_edited ? " (edited)" : ""} — archive?
                  </label>
                ))}
              </div>
            ))}
            <div className="mt-3 flex justify-end gap-2">
              <Dialog.Close className="rounded border border-line px-3 py-1.5 text-sm text-muted">Cancel</Dialog.Close>
              <button onClick={() => void approvePlan()} disabled={busy}
                className="rounded bg-teal px-3 py-1.5 text-sm font-medium text-black disabled:opacity-50">Apply</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
