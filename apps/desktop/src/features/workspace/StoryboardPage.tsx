import { useEffect, useMemo, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { apiFetch, listReferenceSets } from "../../api/client";
import { AuthImage } from "../../components/AuthImage";
import { commitCommand, previewCommand } from "../../api/commands";
import type {
  CastMember,
  CommandProposal,
  EntityRevision,
  Entity,
  ProviderDetail,
  ProviderSummary,
  ReferenceSet,
  StoryboardScene,
} from "../../api/types";
import { SimBadge } from "./cloudShared";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useApp } from "../../stores/appStore";

const SHOT_TYPES = [
  "establishing", "wide", "medium", "medium_close", "close", "extreme_close",
  "over_shoulder", "pov", "aerial", "insert", "two_shot", "group", "other",
];

const SHOT_LABEL: Record<string, string> = {
  establishing: "EST", wide: "WIDE", medium: "MED", medium_close: "MCU",
  close: "CU", extreme_close: "ECU", over_shoulder: "OTS", pov: "POV",
  aerial: "AIR", insert: "INS", two_shot: "2S", group: "GRP", other: "—",
};

const CHIP_COLORS = [
  "bg-teal/20 text-teal", "bg-violet-500/20 text-violet-300",
  "bg-amber/20 text-amber", "bg-sky-500/20 text-sky-300",
  "bg-rose-500/20 text-rose-300", "bg-emerald-500/20 text-emerald-300",
];
const chipColor = (id: string) => {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return CHIP_COLORS[Math.abs(h) % CHIP_COLORS.length];
};
const initials = (name: string) =>
  name.split(/\s+/).map((w) => w[0]).filter(Boolean).slice(0, 2).join("").toUpperCase();

// Per-cast-row editable state (mirrors shot_characters columns).
interface CastState {
  wardrobe_variant: string;
  blocking: string;
  expression: string;
  emotion: string;
  action_instructions: string;
  dialogue: string;
  voice_override: string;
  model_ref: string;
  loras: string; // comma-separated → lora_json
}
const castStateOf = (c?: Partial<CastMember>): CastState => ({
  wardrobe_variant: c?.wardrobe_variant ?? "",
  blocking: c?.blocking ?? "",
  expression: c?.expression ?? "",
  emotion: c?.emotion ?? "",
  action_instructions: c?.action_instructions ?? "",
  dialogue: c?.dialogue ?? "",
  voice_override: c?.voice_override ?? "",
  model_ref: c?.model_ref ?? "",
  loras: (c?.lora_json ?? []).join(", "),
});
const castPayload = (s: CastState) => ({
  schema_version: 1,
  wardrobe_variant: s.wardrobe_variant, blocking: s.blocking,
  expression: s.expression, emotion: s.emotion,
  action_instructions: s.action_instructions, dialogue: s.dialogue,
  voice_override: s.voice_override, model_ref: s.model_ref,
  loras: s.loras.split(",").map((x) => x.trim()).filter(Boolean),
});

export function StoryboardPage() {
  const project = useApp((s) => s.currentProject());
  const board = useApp((s) => s.storyboard);
  const refresh = useApp((s) => s.refreshStoryboard);
  const refreshProject = useApp((s) => s.refreshProjectData);
  const characters = useApp((s) => s.entities.character ?? []);
  const locations = useApp((s) => s.entities.location ?? []);
  const worlds = useApp((s) => s.entities.world ?? []);
  const assets = useApp((s) => s.assetsPage?.items ?? []);
  const refreshAssets = useApp((s) => s.refreshAssets);
  const setGenerateShotId = useApp((s) => s.setGenerateShotId);
  const setWorkspaceTab = useApp((s) => s.setWorkspaceTab);

  const [error, setError] = useState<string | null>(null);
  const [sceneDlg, setSceneDlg] = useState<{ mode: "new" } | { mode: "edit"; row: StoryboardScene } | null>(null);
  const [shotDlg, setShotDlg] = useState<{ scene: StoryboardScene; entry: StoryboardScene["shots"][number] | null } | null>(null);
  const [confirm, setConfirm] = useState<{ title: string; body: string; run: () => void } | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const drag = useRef<{ shotId: string; sceneId: string } | null>(null);
  const [dropAt, setDropAt] = useState<{ sceneId: string; index: number } | null>(null);

  useEffect(() => {
    void refreshAssets();
  }, [refreshAssets]);

  const shown = useMemo(
    () => board.filter((s) => showArchived || s.scene.status === "active"),
    [board, showArchived],
  );
  const imageAssets = useMemo(() => assets.filter((a) => a.kind === "image"), [assets]);

  async function run(commandType: string, payload: Record<string, unknown>) {
    if (!project) return false;
    setError(null);
    try {
      // Read the live revision — back-to-back commands (batch saves) each
      // bump it, so a stale read would hit a revision conflict.
      const rev = useApp.getState().currentProject()?.revision ?? project.revision;
      await commitCommand(project.id, rev, commandType, payload, "storyboard");
      await Promise.all([refresh(), refreshProject()]);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    }
  }

  async function dropShot(sceneId: string, index: number) {
    const d = drag.current;
    setDropAt(null);
    if (!d) return;
    drag.current = null;
    const fromScene = board.find((s) => s.scene.id === d.sceneId);
    const fromIdx = fromScene?.shots.findIndex((x) => x.shot.id === d.shotId) ?? -1;
    // Adjust index when moving within the same scene (removal shifts slots).
    let toIndex = index;
    if (d.sceneId === sceneId && fromIdx >= 0 && fromIdx < index) toIndex = index - 1;
    if (d.sceneId === sceneId && fromIdx === toIndex) return;
    await run("shot.move", {
      shot_id: d.shotId, to_index: toIndex,
      ...(d.sceneId !== sceneId ? { to_scene_id: sceneId } : {}),
    });
  }

  if (!project) return null;

  return (
    <div className="mx-auto max-w-6xl p-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Storyboard</h2>
          <p className="mt-0.5 text-sm text-muted">
            Scenes and shots in story order — drag shot cards to rearrange. Every change is versioned.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-muted">
            <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
            Show archived
          </label>
          <button
            onClick={() => setSceneDlg({ mode: "new" })}
            className="rounded-md bg-action px-3.5 py-1.5 text-sm font-medium text-white hover:bg-action-hi"
          >
            + New scene
          </button>
        </div>
      </div>

      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}

      <div className="mt-5 space-y-5">
        {shown.map((row, i) => (
          <section
            key={row.scene.id}
            className={`rounded-lg border bg-card ${row.scene.status === "archived" ? "border-line opacity-60" : "border-line"}`}
          >
            {/* scene header */}
            <header className="flex items-center gap-3 border-b border-line px-4 py-2.5">
              <span className="flex h-7 w-7 items-center justify-center rounded bg-panel text-xs font-bold text-teal">
                {i + 1}
              </span>
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h3 className="truncate text-sm font-semibold">{row.scene.title}</h3>
                  {row.scene.status === "archived" && (
                    <span className="text-[10px] uppercase text-amber">archived</span>
                  )}
                </div>
                <p className="truncate text-xs text-muted">
                  {row.scene.intent || row.scene.description || "—"}
                  {row.location_name ? ` · ${row.location_name}` : ""}
                </p>
              </div>
              <div className="ml-2 flex items-center gap-1">
                {row.cast.slice(0, 5).map((c) => (
                  <span key={c.id} title={c.character.name + (c.role ? ` — ${c.role}` : "")}
                    className={`flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold ${chipColor(c.character_id)}`}>
                    {initials(c.character.name)}
                  </span>
                ))}
                {row.cast.length > 5 && <span className="text-[10px] text-muted">+{row.cast.length - 5}</span>}
              </div>
              <div className="ml-auto flex items-center gap-1">
                <button title="Move scene earlier" disabled={i === 0}
                  onClick={() => void run("scene.move", { scene_id: row.scene.id, to_index: i - 1 })}
                  className="rounded border border-line px-1.5 py-0.5 text-xs text-muted hover:text-fg disabled:opacity-30">◀</button>
                <button title="Move scene later" disabled={i === shown.length - 1}
                  onClick={() => void run("scene.move", { scene_id: row.scene.id, to_index: i + 1 })}
                  className="rounded border border-line px-1.5 py-0.5 text-xs text-muted hover:text-fg disabled:opacity-30">▶</button>
                <button onClick={() => setShotDlg({ scene: row, entry: null })}
                  className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg">+ Shot</button>
                <button onClick={() => setSceneDlg({ mode: "edit", row })}
                  className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg">Edit</button>
                <button
                  onClick={() => setConfirm({
                    title: `Duplicate "${row.scene.title}"?`,
                    body: "Creates a copy with all its shots and cast right after this scene.",
                    run: () => void run("scene.duplicate", { scene_id: row.scene.id }),
                  })}
                  className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg">Duplicate</button>
                {row.scene.status === "active" ? (
                  <button
                    onClick={() => setConfirm({
                      title: `Archive "${row.scene.title}"?`,
                      body: "Hidden from the board but kept — unarchive anytime.",
                      run: () => void run("scene.archive", { scene_id: row.scene.id }),
                    })}
                    className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg">Archive</button>
                ) : (
                  <button onClick={() => void run("scene.unarchive", { scene_id: row.scene.id })}
                    className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg">Unarchive</button>
                )}
              </div>
            </header>

            {/* shot strip */}
            <div className="flex flex-wrap items-stretch gap-2 p-3">
              {row.shots.map((entry, j) => (
                <div key={entry.shot.id} className="flex items-stretch gap-2">
                  <DropZone
                    active={dropAt?.sceneId === row.scene.id && dropAt.index === j}
                    onDragOver={(e) => { e.preventDefault(); setDropAt({ sceneId: row.scene.id, index: j }); }}
                    onLeave={() => setDropAt((d) => (d?.sceneId === row.scene.id && d.index === j ? null : d))}
                    onDrop={() => void dropShot(row.scene.id, j)}
                  />
                  <ShotCard
                    entry={entry}
                    projectId={project.id}
                    onDragStart={() => { drag.current = { shotId: entry.shot.id, sceneId: row.scene.id }; }}
                    onDragEnd={() => { drag.current = null; setDropAt(null); }}
                    onClick={() => setShotDlg({ scene: row, entry })}
                    onDuplicate={() => void run("shot.duplicate", { shot_id: entry.shot.id })}
                    onGenerate={() => {
                      setGenerateShotId(entry.shot.id);
                      setWorkspaceTab("generate");
                    }}
                  />
                </div>
              ))}
              <DropZone
                tail
                active={dropAt?.sceneId === row.scene.id && dropAt.index === row.shots.length}
                onDragOver={(e) => { e.preventDefault(); setDropAt({ sceneId: row.scene.id, index: row.shots.length }); }}
                onLeave={() => setDropAt((d) => (d?.sceneId === row.scene.id && d.index === row.shots.length ? null : d))}
                onDrop={() => void dropShot(row.scene.id, row.shots.length)}
              />
              {row.shots.length === 0 && (
                <button
                  onClick={() => setShotDlg({ scene: row, entry: null })}
                  className="flex h-28 w-40 items-center justify-center rounded-md border border-dashed border-line text-xs text-muted hover:border-teal hover:text-teal"
                >
                  + Add first shot
                </button>
              )}
            </div>
          </section>
        ))}
        {shown.length === 0 && (
          <div className="rounded-lg border border-dashed border-line p-10 text-center">
            <p className="text-sm text-muted">Your story starts here.</p>
            <button onClick={() => setSceneDlg({ mode: "new" })}
              className="mt-3 rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi">
              + Create the first scene
            </button>
          </div>
        )}
      </div>

      {sceneDlg && (
        <SceneDialog
          dlg={sceneDlg}
          onClose={() => setSceneDlg(null)}
          characters={characters}
          locations={locations}
          worlds={worlds}
          run={run}
        />
      )}
      {shotDlg && (
        <ShotDialog
          scene={shotDlg.scene}
          entry={shotDlg.entry}
          onClose={() => setShotDlg(null)}
          characters={characters}
          locations={locations}
          imageAssets={imageAssets}
          projectId={project.id}
          run={run}
        />
      )}
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={confirm?.title ?? ""}
        body={confirm?.body ?? ""}
        onConfirm={() => confirm?.run()}
      />
    </div>
  );
}

function DropZone({ active, tail, onDragOver, onLeave, onDrop }: {
  active: boolean; tail?: boolean;
  onDragOver: (e: React.DragEvent) => void; onLeave: () => void; onDrop: () => void;
}) {
  return (
    <div
      onDragOver={onDragOver}
      onDragLeave={onLeave}
      onDrop={(e) => { e.preventDefault(); onDrop(); }}
      className={`self-stretch rounded transition-all ${
        active ? "w-3 bg-teal/50" : tail ? "w-6 border border-dashed border-transparent" : "w-1.5"
      }`}
      aria-hidden
    />
  );
}

function ShotCard({ entry, projectId, onDragStart, onDragEnd, onClick, onDuplicate, onGenerate }: {
  entry: StoryboardScene["shots"][number];
  projectId: string;
  onDragStart: () => void; onDragEnd: () => void; onClick: () => void; onDuplicate: () => void;
  onGenerate: () => void;
}) {
  const s = entry.shot;
  return (
    <div
      draggable
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onClick={onClick}
      title={s.description || s.title || "Edit shot"}
      className="group w-44 cursor-grab rounded-md border border-line bg-panel transition-colors hover:border-teal active:cursor-grabbing"
    >
      <div className="relative h-20 overflow-hidden rounded-t-md bg-ink">
        <AuthImage
          pid={projectId}
          assetId={s.thumbnail_asset_id}
          alt=""
          className="h-full w-full object-cover"
          draggable={false}
          fallback={
            <div className="flex h-full items-center justify-center text-2xl font-bold text-faint">
              {SHOT_LABEL[s.shot_type] ?? "—"}
            </div>
          }
        />
        <span className="absolute left-1 top-1 rounded bg-black/60 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-white">
          {s.shot_type.replace("_", " ")}
        </span>
        {s.duration_sec > 0 && (
          <span className="absolute right-1 top-1 rounded bg-black/60 px-1.5 py-0.5 text-[9px] text-white">
            {s.duration_sec}s
          </span>
        )}
        <button
          onClick={(e) => { e.stopPropagation(); onDuplicate(); }}
          title="Duplicate shot"
          className="absolute bottom-1 right-1 hidden rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-white group-hover:block"
        >
          ⧉
        </button>
        <button
          onClick={(e) => { e.stopPropagation(); onGenerate(); }}
          title="Generate an image for this shot"
          className="absolute bottom-1 left-1 hidden rounded bg-black/70 px-1.5 py-0.5 text-[10px] text-white group-hover:block"
        >
          ✦
        </button>
      </div>
      <div className="p-2">
        <div className="truncate text-xs font-medium">
          {s.title || <span className="text-muted">Untitled shot</span>}
        </div>
        {s.prompt && <div className="mt-0.5 line-clamp-2 text-[10px] text-muted">{s.prompt}</div>}
        <div className="mt-1.5 flex items-center gap-1">
          {entry.cast.slice(0, 4).map((c) => (
            <span key={c.id} title={`${c.character.name}${c.expression ? ` · ${c.expression}` : ""} (shot)`}
              className={`flex h-5 w-5 items-center justify-center rounded-full text-[9px] font-bold ${chipColor(c.character_id)}`}>
              {initials(c.character.name)}
            </span>
          ))}
          {entry.cast.length > 4 && <span className="text-[9px] text-muted">+{entry.cast.length - 4}</span>}
          {(entry.inherited ?? []).slice(0, Math.max(0, 4 - entry.cast.length)).map((c) => (
            <span key={c.id} title={`${c.character.name} — inherited from scene cast`}
              className={`flex h-5 w-5 items-center justify-center rounded-full border border-dashed border-current text-[9px] font-bold opacity-55 ${chipColor(c.character_id)}`}>
              {initials(c.character.name)}
            </span>
          ))}
          {entry.location_name && (
            <span className="ml-auto truncate text-[9px] text-faint">{entry.location_name}</span>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------- scene dialog ----------

function SceneDialog({ dlg, onClose, characters, locations, worlds, run }: {
  dlg: { mode: "new" } | { mode: "edit"; row: StoryboardScene };
  onClose: () => void;
  characters: Entity[]; locations: Entity[]; worlds: Entity[];
  run: (t: string, p: Record<string, unknown>) => Promise<boolean>;
}) {
  const editing = dlg.mode === "edit" ? dlg.row.scene : null;
  const [form, setForm] = useState({
    title: editing?.title ?? "",
    description: editing?.description ?? "",
    intent: editing?.intent ?? "",
    world_id: editing?.world_id ?? "",
    location_id: editing?.location_id ?? "",
    continuity_notes: String(editing?.continuity_json?.notes ?? ""),
  });
  const [busy, setBusy] = useState(false);
  const [revs, setRevs] = useState<EntityRevision[]>([]);
  const [revertTo, setRevertTo] = useState<number | null>(null);
  const pid = editing?.project_id;

  useEffect(() => {
    if (editing && pid) {
      void apiFetch<EntityRevision[]>(`/api/projects/${pid}/scenes/${editing.id}/versions`)
        .then(setRevs).catch(() => {});
    }
  }, [editing, pid]);

  async function save() {
    setBusy(true);
    const payload: Record<string, unknown> = {
      title: form.title.trim(), description: form.description, intent: form.intent,
      world_id: form.world_id || null, location_id: form.location_id || null,
      continuity: { notes: form.continuity_notes },
    };
    const ok = editing
      ? await run("scene.update", { scene_id: editing.id, ...payload })
      : await run("scene.create", payload);
    setBusy(false);
    if (ok) onClose();
  }

  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60" />
        <Dialog.Content className="fixed left-1/2 top-1/2 max-h-[85vh] w-[560px] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-line bg-panel p-5 shadow-xl">
          <Dialog.Title className="text-lg font-semibold">
            {editing ? `Scene — ${editing.title}` : "New scene"}
          </Dialog.Title>
          <label className="mt-4 block text-sm text-muted">Title
            <input autoFocus value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })}
              className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
          </label>
          <label className="mt-3 block text-sm text-muted">What happens (description)
            <textarea rows={2} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
              className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
          </label>
          <label className="mt-3 block text-sm text-muted">Cinematic intent
            <textarea rows={2} value={form.intent} onChange={(e) => setForm({ ...form, intent: e.target.value })}
              placeholder="Mood, pacing, what the audience should feel…"
              className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
          </label>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <label className="block text-sm text-muted">World
              <select value={form.world_id} onChange={(e) => setForm({ ...form, world_id: e.target.value })}
                className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg">
                <option value="">— none —</option>
                {worlds.filter((w) => w.status === "active").map((w) => (
                  <option key={w.id} value={w.id}>{w.name}</option>
                ))}
              </select>
            </label>
            <label className="block text-sm text-muted">Location
              <select value={form.location_id} onChange={(e) => setForm({ ...form, location_id: e.target.value })}
                className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg">
                <option value="">— none —</option>
                {locations.filter((l) => l.status === "active").map((l) => (
                  <option key={l.id} value={l.id}>{l.name}</option>
                ))}
              </select>
            </label>
          </div>
          <label className="mt-3 block text-sm text-muted">Continuity notes
            <textarea rows={2} value={form.continuity_notes}
              onChange={(e) => setForm({ ...form, continuity_notes: e.target.value })}
              placeholder="What must stay consistent entering/leaving this scene…"
              className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
          </label>

          {editing && dlg.mode === "edit" && (
            <SceneCast row={dlg.row} characters={characters} run={run} />
          )}

          <div className="mt-5 flex items-center justify-between">
            <span className="text-xs text-faint">{editing ? `rev ${editing.revision}` : ""}</span>
            <div className="flex gap-2">
              <Dialog.Close className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg">Cancel</Dialog.Close>
              <button onClick={() => void save()} disabled={busy || !form.title.trim()}
                className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40">
                {busy ? "Saving…" : editing ? "Save" : "Create scene"}
              </button>
            </div>
          </div>

          {revs.length > 0 && (
            <div className="mt-4 border-t border-line pt-3">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Version history</h4>
              <ul className="mt-2 max-h-40 space-y-1 overflow-y-auto">
                {revs.map((r) => (
                  <li key={r.id} className="flex items-center justify-between rounded px-2 py-1 text-xs hover:bg-card">
                    <span className="text-muted">rev {r.revision} — {r.summary} · {new Date(r.created_at).toLocaleString()}</span>
                    {r.revision !== editing?.revision && (
                      <button onClick={() => setRevertTo(r.revision)}
                        className="rounded border border-line px-2 py-0.5 text-muted hover:text-fg">Revert</button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <ConfirmDialog
            open={revertTo !== null}
            onOpenChange={(o) => !o && setRevertTo(null)}
            title={`Revert scene to revision ${revertTo}?`}
            body="Fields and scene cast return to that revision; the current state stays in history."
            confirmLabel="Revert"
            onConfirm={() => {
              if (editing) void run("scene.revert", { scene_id: editing.id, to_revision: revertTo });
              onClose();
            }}
          />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function SceneCast({ row, characters, run }: {
  row: StoryboardScene; characters: Entity[];
  run: (t: string, p: Record<string, unknown>) => Promise<boolean>;
}) {
  const [pick, setPick] = useState("");
  const [removeConfirm, setRemoveConfirm] = useState<{ cid: string; name: string; heirs: number } | null>(null);
  const assigned = new Set(row.cast.map((c) => c.character_id));
  const avail = characters.filter((c) => c.status === "active" && !assigned.has(c.id));
  // How many shots currently inherit each scene-cast member (no own row).
  const heirCount = (cid: string) =>
    row.shots.filter((sh) => (sh.inherited ?? []).some((x) => x.character_id === cid)).length;
  return (
    <div className="mt-4 rounded-md border border-line p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Scene cast</h4>
        <span className="text-[10px] text-faint">flows into every shot · shots may override</span>
      </div>
      <ul className="mt-2 space-y-1.5">
        {row.cast.map((c) => {
          const heirs = heirCount(c.character_id);
          return (
          <li key={c.id} className="rounded border border-line bg-card px-2 py-1.5">
            <div className="flex items-center gap-2 text-sm">
              <span className={`flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold ${chipColor(c.character_id)}`}>
                {initials(c.character.name)}
              </span>
              <span className="font-medium">{c.character.name}</span>
              {heirs > 0 && (
                <span title={`Inherited by ${heirs} shot${heirs === 1 ? "" : "s"}`}
                  className="rounded bg-panel px-1.5 py-0.5 text-[10px] text-faint">
                  → {heirs} shot{heirs === 1 ? "" : "s"}
                </span>
              )}
              <button title="Remove from scene cast"
                onClick={() => heirs > 0
                  ? setRemoveConfirm({ cid: c.character_id, name: c.character.name, heirs })
                  : void run("scene.cast.remove", { scene_id: row.scene.id, character_id: c.character_id })}
                className="ml-auto rounded border border-line px-1.5 py-0.5 text-xs text-muted hover:text-danger">✕</button>
            </div>
            <div className="mt-1.5 grid grid-cols-2 gap-2">
              <input
                defaultValue={c.role ?? ""}
                placeholder="role (lead, supporting…)"
                onBlur={(e) => {
                  if (e.target.value !== (c.role ?? "")) {
                    void run("scene.cast.update", {
                      scene_id: row.scene.id, character_id: c.character_id, role: e.target.value,
                    });
                  }
                }}
                className="rounded border border-line bg-panel px-2 py-1 text-xs text-fg"
              />
              <input
                defaultValue={String(c.notes_json?.notes ?? "")}
                placeholder="scene-level continuity notes…"
                onBlur={(e) => {
                  if (e.target.value !== String(c.notes_json?.notes ?? "")) {
                    void run("scene.cast.update", {
                      scene_id: row.scene.id, character_id: c.character_id,
                      notes: { ...(c.notes_json ?? {}), notes: e.target.value },
                    });
                  }
                }}
                className="rounded border border-line bg-panel px-2 py-1 text-xs text-fg"
              />
            </div>
          </li>
          );
        })}
        {row.cast.length === 0 && <li className="text-xs text-muted">No characters assigned yet.</li>}
      </ul>
      {avail.length > 0 && (
        <div className="mt-2 flex gap-2">
          <select value={pick} onChange={(e) => setPick(e.target.value)}
            className="flex-1 rounded-md border border-line bg-card px-2 py-1.5 text-sm text-fg">
            <option value="">Add character…</option>
            {avail.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <button
            disabled={!pick}
            onClick={() => { void run("scene.cast.assign", { scene_id: row.scene.id, character_id: pick }); setPick(""); }}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-fg disabled:opacity-40">
            Add
          </button>
        </div>
      )}
      <ConfirmDialog
        open={removeConfirm !== null}
        onOpenChange={(o) => !o && setRemoveConfirm(null)}
        title={`Remove ${removeConfirm?.name} from the scene cast?`}
        body={`${removeConfirm?.name} is inherited by ${removeConfirm?.heirs} shot${removeConfirm?.heirs === 1 ? "" : "s"} — they'll lose the assignment too. Per-shot overrides in other shots stay untouched.`}
        confirmLabel="Remove from scene"
        onConfirm={() => {
          if (removeConfirm) void run("scene.cast.remove", { scene_id: row.scene.id, character_id: removeConfirm.cid });
          setRemoveConfirm(null);
        }}
      />
    </div>
  );
}

// ---------- shot dialog ----------

function ShotDialog({ scene, entry, onClose, characters, locations, imageAssets, projectId, run }: {
  scene: StoryboardScene;
  entry: StoryboardScene["shots"][number] | null;
  onClose: () => void;
  characters: Entity[]; locations: Entity[];
  imageAssets: { id: string; name: string }[];
  projectId: string;
  run: (t: string, p: Record<string, unknown>) => Promise<boolean>;
}) {
  const shot = entry?.shot ?? null;
  const [form, setForm] = useState({
    title: shot?.title ?? "",
    shot_type: shot?.shot_type ?? "medium",
    duration_sec: String(shot?.duration_sec ?? ""),
    description: shot?.description ?? "",
    intent: shot?.intent ?? "",
    prompt: shot?.prompt ?? "",
    location_id: shot?.location_id ?? "",
    thumbnail_asset_id: shot?.thumbnail_asset_id ?? "",
    reference_set_id: shot?.reference_set_id ?? "",
    source_asset_id: shot?.source_asset_id ?? "",
    continuity_notes: String(shot?.continuity_json?.notes ?? ""),
    continuity_changes: String(shot?.continuity_json?.changes ?? ""),
  });
  // C4-B — the project's reference sets, filtered to this shot's cast
  // (own + inherited). Membership lives server-side; the picker only
  // rebinds the set id.
  const [refSets, setRefSets] = useState<ReferenceSet[]>([]);
  useEffect(() => {
    let live = true;
    listReferenceSets(projectId)
      .then((s) => { if (live) setRefSets(s); })
      .catch(() => { if (live) setRefSets([]); });
    return () => { live = false; };
  }, [projectId]);
  // Cast editing state: {character_id: CastState}, plus added/removed tracking.
  const [cast, setCast] = useState<Record<string, CastState>>(
    () => Object.fromEntries((entry?.cast ?? []).map((c) => [c.character_id, castStateOf(c)])),
  );
  const [added, setAdded] = useState<string[]>([]);
  const [removed, setRemoved] = useState<string[]>([]);
  const [pick, setPick] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [revs, setRevs] = useState<EntityRevision[]>([]);
  const [revertTo, setRevertTo] = useState<number | null>(null);
  const refreshRevs = useApp((s) => s.refreshProjectData);

  useEffect(() => {
    if (shot) {
      void apiFetch<EntityRevision[]>(`/api/projects/${projectId}/shots/${shot.id}/versions`)
        .then(setRevs).catch(() => {});
    }
  }, [shot, projectId]);

  const origCast = useMemo(
    () => Object.fromEntries((entry?.cast ?? []).map((c) => [c.character_id, castStateOf(c)])),
    [entry],
  );
  const assigned = new Set(Object.keys(cast));
  // Inherited members aren't "available" — they're customized via Override.
  const inheritedRows = entry?.inherited ?? [];
  const inheritedIds = new Set(inheritedRows.map((c) => c.character_id));
  // C4-B — sets whose character is in this shot's effective cast
  // (own ∪ inherited). The backend enforces the same rule.
  const castIds = new Set([...Object.keys(cast), ...inheritedIds]);
  const castSets = castIds.size
    ? refSets.filter((s) => castIds.has(s.character_id))
    : refSets;
  const sceneCastIds = new Set(scene.cast.map((c) => c.character_id));
  const avail = characters.filter(
    (c) => c.status === "active" && !assigned.has(c.id) && !inheritedIds.has(c.id));
  const charName = (id: string) =>
    characters.find((c) => c.id === id)?.name ??
    inheritedRows.find((c) => c.character_id === id)?.character.name ?? "?";

  async function save() {
    setBusy(true);
    setErr(null);
    // Sequential commits — each bumps the project revision; commands are
    // chained through a live revision read so ordering can't conflict.
    const cmds: [string, Record<string, unknown>][] = [];
    if (shot) {
      cmds.push(["shot.update", {
        shot_id: shot.id,
        title: form.title.trim(), shot_type: form.shot_type,
        duration_sec: parseFloat(form.duration_sec) || 0,
        description: form.description, intent: form.intent, prompt: form.prompt,
        location_id: form.location_id || null,
        thumbnail_asset_id: form.thumbnail_asset_id || null,
        reference_set_id: form.reference_set_id || null,
        source_asset_id: form.source_asset_id || null,
        continuity: { notes: form.continuity_notes, changes: form.continuity_changes },
      }]);
    }
    for (const cid of removed) {
      cmds.push(["shot.cast.remove", { shot_id: shot!.id, character_id: cid }]);
    }
    for (const cid of added) {
      cmds.push(["shot.cast.assign", { shot_id: shot!.id, character_id: cid, state: castPayload(cast[cid]) }]);
    }
    for (const cid of Object.keys(cast)) {
      if (added.includes(cid) || removed.includes(cid)) continue;
      if (JSON.stringify(cast[cid]) !== JSON.stringify(origCast[cid])) {
        cmds.push(["shot.cast.update", { shot_id: shot!.id, character_id: cid, state: castPayload(cast[cid]) }]);
      }
    }
    let ok = true;
    if (shot) {
      for (const [t, p] of cmds) {
        ok = await run(t, p);
        if (!ok) break;
      }
    } else {
      ok = await run("shot.create", {
        scene_id: scene.scene.id,
        title: form.title.trim(), shot_type: form.shot_type,
        duration_sec: parseFloat(form.duration_sec) || 0,
        description: form.description, intent: form.intent, prompt: form.prompt,
        location_id: form.location_id || null,
        reference_set_id: form.reference_set_id || null,
        source_asset_id: form.source_asset_id || null,
        continuity: { notes: form.continuity_notes, changes: form.continuity_changes },
      });
    }
    setBusy(false);
    if (ok) onClose();
    else setErr("Save failed — see the message above the board.");
    void refreshRevs();
  }

  const castFields: { key: keyof CastState; label: string; hint?: string }[] = [
    { key: "expression", label: "Facial expression" },
    { key: "emotion", label: "Emotional state" },
    { key: "wardrobe_variant", label: "Wardrobe / costume variant" },
    { key: "blocking", label: "Position & movement" },
    { key: "action_instructions", label: "Action / performance" },
    { key: "dialogue", label: "Dialogue" },
    { key: "voice_override", label: "Voice" },
    { key: "model_ref", label: "Model / LoRA identity ref" },
    { key: "loras", label: "LoRAs (comma-separated)" },
  ];

  return (
    <Dialog.Root open onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60" />
        <Dialog.Content className="fixed left-1/2 top-1/2 flex max-h-[88vh] w-[640px] -translate-x-1/2 -translate-y-1/2 flex-col rounded-lg border border-line bg-panel shadow-xl">
          <div className="border-b border-line px-5 py-4">
            <Dialog.Title className="text-lg font-semibold">
              {shot ? `Shot — ${shot.title || "untitled"} · rev ${shot.revision}` : `New shot — ${scene.scene.title}`}
            </Dialog.Title>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            <div className="grid grid-cols-[1fr_140px_90px] gap-3">
              <label className="block text-sm text-muted">Title
                <input autoFocus value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })}
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
              </label>
              <label className="block text-sm text-muted">Shot type
                <select value={form.shot_type} onChange={(e) => setForm({ ...form, shot_type: e.target.value })}
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg">
                  {SHOT_TYPES.map((t) => <option key={t} value={t}>{t.replace("_", " ")}</option>)}
                </select>
              </label>
              <label className="block text-sm text-muted">Duration (s)
                <input value={form.duration_sec} inputMode="decimal"
                  onChange={(e) => setForm({ ...form, duration_sec: e.target.value })}
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
              </label>
            </div>
            <label className="mt-3 block text-sm text-muted">Description
              <textarea rows={2} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
                className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
            </label>
            <label className="mt-3 block text-sm text-muted">Cinematic intent
              <textarea rows={2} value={form.intent} onChange={(e) => setForm({ ...form, intent: e.target.value })}
                className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
            </label>
            <label className="mt-3 block text-sm text-muted">
              Prompt <span className="text-faint">(always editable — generation reads it later)</span>
              <textarea rows={3} value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })}
                className="mt-1 w-full rounded-md border border-teal/40 bg-card px-3 py-2 font-mono text-sm text-fg" />
            </label>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <label className="block text-sm text-muted">Location
                <select value={form.location_id} onChange={(e) => setForm({ ...form, location_id: e.target.value })}
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg">
                  <option value="">— none —</option>
                  {locations.filter((l) => l.status === "active").map((l) => (
                    <option key={l.id} value={l.id}>{l.name}</option>
                  ))}
                </select>
              </label>
              <label className="block text-sm text-muted">Thumbnail (image asset)
                <select value={form.thumbnail_asset_id}
                  onChange={(e) => setForm({ ...form, thumbnail_asset_id: e.target.value })}
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg">
                  <option value="">— none —</option>
                  {imageAssets.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              </label>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <label className="block text-sm text-muted">Continuity — stays consistent
                <textarea rows={2} value={form.continuity_notes}
                  onChange={(e) => setForm({ ...form, continuity_notes: e.target.value })}
                  placeholder="appearance, props, lighting…"
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
              </label>
              <label className="block text-sm text-muted">Continuity — what changed
                <textarea rows={2} value={form.continuity_changes}
                  onChange={(e) => setForm({ ...form, continuity_changes: e.target.value })}
                  placeholder="since the previous shot…"
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg" />
              </label>
            </div>

            {/* ---- C4-B generation bindings ---- */}
            <div className="mt-3 rounded-md border border-line p-3">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">
                Generation bindings
              </h4>
              <div className="mt-2 grid grid-cols-2 gap-3">
                <label className="block text-sm text-muted">Character reference set
                  <select value={form.reference_set_id}
                    onChange={(e) => setForm({ ...form, reference_set_id: e.target.value })}
                    className="mt-1 w-full rounded-md border border-line bg-card px-2 py-1.5 text-xs text-fg">
                    <option value="">None</option>
                    {castSets.map((s) => (
                      <option key={s.id} value={s.id}>
                        {charName(s.character_id)} — {s.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm text-muted">Source image (img2img)
                  <select value={form.source_asset_id}
                    onChange={(e) => setForm({ ...form, source_asset_id: e.target.value })}
                    className="mt-1 w-full rounded-md border border-line bg-card px-2 py-1.5 text-xs text-fg">
                    <option value="">None — text-to-image</option>
                    {imageAssets.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </label>
              </div>
              <p className="mt-1.5 text-[10px] text-faint">
                References guide appearance; they don't guarantee identity. The backend
                re-checks the set against this shot's cast at generate time.
              </p>
              {shot && (
                <ShotGenerate
                  shot={shot} projectId={projectId}
                  setId={shot.reference_set_id} sourceId={shot.source_asset_id}
                  onDone={onClose}
                />
              )}
            </div>

            {/* ---- cast ---- */}
            {shot && (
              <div className="mt-4 rounded-md border border-line p-3">
                <div className="flex items-center justify-between">
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">
                    Shot cast · {assigned.size}
                    {inheritedIds.size > 0 && (
                      <span className="ml-1.5 font-normal normal-case text-faint">
                        + {inheritedIds.size - [...inheritedIds].filter((x) => assigned.has(x)).length} inherited
                      </span>
                    )}
                  </h4>
                  {avail.length > 0 && (
                    <div className="flex gap-2">
                      <select value={pick} onChange={(e) => setPick(e.target.value)}
                        className="rounded-md border border-line bg-card px-2 py-1 text-xs text-fg">
                        <option value="">Add character…</option>
                        {avail.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                      </select>
                      <button
                        disabled={!pick}
                        onClick={() => {
                          setCast({ ...cast, [pick]: castStateOf() });
                          setAdded([...added, pick]);
                          setExpanded(pick);
                          setPick("");
                        }}
                        className="rounded-md border border-line px-2 py-1 text-xs text-muted hover:text-fg disabled:opacity-40">
                        Add
                      </button>
                    </div>
                  )}
                </div>
                <ul className="mt-2 space-y-1.5">
                  {Object.keys(cast).map((cid) => {
                    const isOverride = inheritedIds.has(cid) || (!origCast[cid] && sceneCastIds.has(cid));
                    const pending = added.includes(cid);
                    return (
                    <li key={cid} className="rounded border border-line bg-card">
                      <div className="flex items-center gap-2 px-2 py-1.5">
                        <span className={`flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold ${chipColor(cid)}`}>
                          {initials(charName(cid))}
                        </span>
                        <span className="text-sm font-medium">{charName(cid)}</span>
                        {pending && isOverride && (
                          <span className="rounded bg-teal/15 px-1.5 py-0.5 text-[10px] text-teal">override</span>
                        )}
                        {cast[cid].expression && (
                          <span className="rounded bg-panel px-1.5 py-0.5 text-[10px] text-muted">{cast[cid].expression}</span>
                        )}
                        {cast[cid].emotion && (
                          <span className="rounded bg-panel px-1.5 py-0.5 text-[10px] text-muted">{cast[cid].emotion}</span>
                        )}
                        <button
                          onClick={() => setExpanded(expanded === cid ? null : cid)}
                          className="ml-auto text-[10px] text-muted hover:text-fg">
                          {expanded === cid ? "▾ less" : "▸ state"}
                        </button>
                        <button
                          title={!pending && sceneCastIds.has(cid)
                            ? "Remove override — character returns to inherited scene state"
                            : "Remove from shot"}
                          onClick={() => {
                            const next = { ...cast };
                            delete next[cid];
                            setCast(next);
                            if (added.includes(cid)) setAdded(added.filter((x) => x !== cid));
                            else setRemoved([...removed, cid]);
                          }}
                          className="rounded border border-line px-1.5 py-0.5 text-xs text-muted hover:text-danger">✕</button>
                      </div>
                      {expanded === cid && (
                        <div className="grid grid-cols-2 gap-2 border-t border-line p-3">
                          {castFields.map((f) => (
                            <label key={f.key} className="block text-[11px] text-muted">
                              {f.label}
                              <input
                                value={cast[cid][f.key]}
                                onChange={(e) => setCast({ ...cast, [cid]: { ...cast[cid], [f.key]: e.target.value } })}
                                className="mt-0.5 w-full rounded border border-line bg-panel px-2 py-1 text-xs text-fg"
                              />
                            </label>
                          ))}
                        </div>
                      )}
                    </li>
                    );
                  })}
                  {assigned.size === 0 && inheritedRows.length === 0 && (
                    <li className="text-xs text-muted">Nobody in this shot yet — add characters above.</li>
                  )}
                </ul>

                {/* inherited from scene — read-only until customized */}
                {inheritedRows.filter((c) => !assigned.has(c.character_id)).length > 0 && (
                  <div className="mt-3 border-t border-dashed border-line pt-2.5">
                    <h5 className="text-[10px] font-semibold uppercase tracking-wide text-faint">
                      Inherited from scene cast
                    </h5>
                    <ul className="mt-1.5 space-y-1">
                      {inheritedRows.filter((c) => !assigned.has(c.character_id)).map((c) => (
                        <li key={c.character_id} className="flex items-center gap-2 rounded border border-dashed border-line bg-panel/50 px-2 py-1.5">
                          <span className={`flex h-6 w-6 items-center justify-center rounded-full border border-dashed border-current text-[10px] font-bold opacity-60 ${chipColor(c.character_id)}`}>
                            {initials(c.character.name)}
                          </span>
                          <span className="text-sm text-muted">{c.character.name}</span>
                          {c.role && <span className="rounded bg-card px-1.5 py-0.5 text-[10px] text-faint">{c.role}</span>}
                          <button
                            onClick={() => {
                              setCast({ ...cast, [c.character_id]: castStateOf() });
                              setAdded([...added, c.character_id]);
                              setExpanded(c.character_id);
                            }}
                            className="ml-auto rounded border border-line px-2 py-0.5 text-[10px] text-muted hover:border-teal hover:text-teal">
                            Customize for this shot
                          </button>
                        </li>
                      ))}
                    </ul>
                    <p className="mt-1.5 text-[10px] text-faint">
                      Inherited members use their scene defaults here. Customizing creates a per-shot
                      override — it only affects this shot, never the others.
                    </p>
                  </div>
                )}
              </div>
            )}

            {err && <p className="mt-3 text-sm text-danger">{err}</p>}

            {revs.length > 0 && (
              <div className="mt-4 border-t border-line pt-3">
                <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Version history</h4>
                <ul className="mt-2 max-h-36 space-y-1 overflow-y-auto">
                  {revs.map((r) => (
                    <li key={r.id} className="flex items-center justify-between rounded px-2 py-1 text-xs hover:bg-card">
                      <span className="text-muted">rev {r.revision} — {r.summary} · {new Date(r.created_at).toLocaleString()}</span>
                      {r.revision !== shot?.revision && (
                        <button onClick={() => setRevertTo(r.revision)}
                          className="rounded border border-line px-2 py-0.5 text-muted hover:text-fg">Revert</button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
          <div className="flex items-center justify-between border-t border-line px-5 py-3">
            <span className="text-xs text-faint">
              {shot ? `${entry?.cast.length ?? 0} in cast` : ""}
            </span>
            <div className="flex gap-2">
              <Dialog.Close className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg">Cancel</Dialog.Close>
              <button onClick={() => void save()} disabled={busy}
                className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40">
                {busy ? "Saving…" : "Save shot"}
              </button>
            </div>
          </div>
          <ConfirmDialog
            open={revertTo !== null}
            onOpenChange={(o) => !o && setRevertTo(null)}
            title={`Revert shot to revision ${revertTo}?`}
            body="Fields and shot cast return to that revision; the current state stays in history."
            confirmLabel="Revert"
            onConfirm={() => {
              if (shot) void run("shot.revert", { shot_id: shot.id, to_revision: revertTo });
              onClose();
            }}
          />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ---------- C4-B shot generate ----------

interface ImageContracts {
  implemented: string[];
  img2img_implemented?: string[];
}

/** Compact generate-from-shot section inside the shot dialog. Composes
 * the saved shot bindings (reference set + source image) through the
 * shot.generate convenience command — same preview -> approve -> commit
 * path as CloudImagePanel, same expected_* staleness pinning. */
function ShotGenerate({ shot, projectId, setId, sourceId, onDone }: {
  shot: { id: string; prompt: string };
  projectId: string;
  setId: string | null;
  sourceId: string | null;
  onDone: () => void;
}) {
  const jobs = useApp((s) => s.jobs);
  const setWorkspaceTab = useApp((s) => s.setWorkspaceTab);
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [details, setDetails] = useState<Record<string, ProviderDetail>>({});
  const [contracts, setContracts] = useState<string[]>([]);
  const [providerKey, setProviderKey] = useState("");
  const [modelId, setModelId] = useState("");
  const [pending, setPending] = useState<{ payload: Record<string, unknown>; proposal: CommandProposal } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    apiFetch<ImageContracts>("/api/providers/image-contracts")
      .then((c) => {
        if (!live) return;
        setContracts(sourceId ? (c.img2img_implemented ?? []) : c.implemented);
      })
      .catch(() => { if (live) setContracts([]); });
    apiFetch<ProviderSummary[]>("/api/providers?category=image")
      .then((ps) => {
        if (!live) return;
        setProviders(ps);
        return Promise.all(ps.map((p) =>
          apiFetch<ProviderDetail>(`/api/providers/${p.key}`)
            .then((d) => [p.key, d] as const)));
      })
      .then((ds) => {
        if (!live || !ds) return;
        setDetails(Object.fromEntries(ds));
      })
      .catch(() => {});
    return () => { live = false; };
  }, [sourceId]);

  const selectable = useMemo(
    () => providers.filter((p) => contracts.includes(p.key)),
    [providers, contracts],
  );
  const models = useMemo(() => {
    const d = details[providerKey];
    return (d?.models ?? []).filter((m) =>
      (m.capability || "").startsWith("image") &&
      (sourceId ? m.editing?.img2img : true));
  }, [details, providerKey, sourceId]);

  // The latest generated asset for this shot — provenance carries the
  // composed shot_generate block with the shot id.
  const assets = useApp((s) => s.assetsPage?.items ?? []);
  const latest = useMemo(() => assets.find((a) =>
    a.kind === "image" &&
    (a.provenance_json?.request as Record<string, unknown> | undefined)
      ?.shot_generate &&
    (((a.provenance_json?.request as Record<string, unknown> | undefined)
      ?.shot_generate as Record<string, unknown> | undefined)?.shot_id === shot.id)),
    [assets, shot.id]);
  const running = useMemo(() => Object.values(jobs).find((j) =>
    j.project_id === projectId && j.adapter_id === "cloud.image.sim" &&
    (j.status === "queued" || j.status === "running")),
    [jobs, projectId]);

  function payload(): Record<string, unknown> {
    return {
      shot_id: shot.id,
      provider_key: providerKey || undefined,
      model_id: modelId || undefined,
    };
  }

  async function preview() {
    setBusy(true); setErr(null);
    try {
      const proposal = await previewCommand(projectId, "shot.generate", payload());
      setPending({ payload: payload(), proposal });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setPending(null);
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!pending) return;
    setBusy(true); setErr(null);
    try {
      const rev = useApp.getState().currentProject()?.revision ?? 0;
      const p = { ...pending.payload };
      if (pending.proposal.resolved_references) {
        p.expected_references = pending.proposal.resolved_references;
        p.expected_set = pending.proposal.resolved_set;
      }
      // Pin the shot's generation-relevant state too — prompt, source,
      // cast, continuity edits since preview refuse the commit.
      if (pending.proposal.resolved_shot) {
        p.expected_shot = pending.proposal.resolved_shot;
      }
      await commitCommand(projectId, rev, "shot.generate", p, "storyboard");
      setPending(null);
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setPending(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 border-t border-dashed border-line pt-3" data-testid="shot-generate">
      <div className="flex items-center gap-2">
        <h5 className="text-[10px] font-semibold uppercase tracking-wide text-faint">
          Generate for this shot <SimBadge />
        </h5>
        {running && (
          <span className="rounded bg-teal/15 px-1.5 py-0.5 text-[10px] text-teal">
            {running.status === "running" ? `running ${Math.round(running.progress * 100)}%` : "queued"}
          </span>
        )}
      </div>
      <div className="mt-2 flex items-end gap-2">
        <label className="block text-[11px] text-muted">Provider
          <select value={providerKey} onChange={(e) => { setProviderKey(e.target.value); setModelId(""); }}
            className="mt-0.5 w-40 rounded border border-line bg-card px-2 py-1 text-xs text-fg">
            <option value="">Choose…</option>
            {selectable.map((p) => <option key={p.key} value={p.key}>{p.label} (simulated)</option>)}
          </select>
        </label>
        <label className="block text-[11px] text-muted">Model
          <select value={modelId} onChange={(e) => setModelId(e.target.value)}
            className="mt-0.5 w-40 rounded border border-line bg-card px-2 py-1 text-xs text-fg">
            <option value="">Choose…</option>
            {models.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
          </select>
        </label>
        <button
          disabled={busy || !providerKey}
          onClick={() => void preview()}
          className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:border-teal hover:text-teal disabled:opacity-40">
          {busy ? "…" : "Preview"}
        </button>
      </div>
      <p className="mt-1.5 text-[10px] text-faint">
        {sourceId ? "Image-to-image using the bound source frame" : "Text-to-image"}
        {setId ? " with the bound reference set" : ""}. Save the shot first —
        generation uses the saved bindings, not this dialog's draft values.
      </p>
      {latest && (
        <div className="mt-2 flex items-center gap-2">
          <AuthImage pid={projectId} assetId={latest.id} alt="Latest generated"
            className="h-14 w-20 rounded border border-line object-cover"
            fallback={<div className="h-14 w-20 rounded border border-line bg-ink" />} />
          <div className="text-[10px] text-muted">
            <div className="font-medium text-fg">Latest output — {latest.name}</div>
            <button onClick={() => { setWorkspaceTab("assets"); onDone(); }}
              className="mt-0.5 text-teal hover:underline">
              View in assets
            </button>
          </div>
        </div>
      )}
      {pending && (
        <div className="mt-2 rounded-md border border-warn/40 bg-warn/10 p-2.5">
          <div className="text-xs font-medium">Confirm simulated generation</div>
          <div className="mt-1 text-[11px] text-muted">
            {pending.proposal.summary}
            {pending.proposal.cost_estimate?.est_cost_usd != null && (
              <> · est ${pending.proposal.cost_estimate.est_cost_usd.toFixed(4)}</>
            )}
          </div>
          {pending.proposal.resolved_references && (
            <div className="mt-1 text-[10px] text-faint">
              {pending.proposal.resolved_references.length} reference(s) pinned —
              a set edit after this preview refuses the commit.
            </div>
          )}
          <div className="mt-2 flex gap-2">
            <button disabled={busy} onClick={() => void approve()}
              className="rounded-md bg-teal px-3 py-1 text-xs font-medium text-ink disabled:opacity-40">
              Approve and queue
            </button>
            <button disabled={busy} onClick={() => setPending(null)}
              className="rounded-md border border-line px-3 py-1 text-xs text-muted">
              Cancel
            </button>
          </div>
        </div>
      )}
      {err && <p className="mt-2 text-xs text-danger">{err}</p>}
    </div>
  );
}
