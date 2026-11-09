import { useEffect, useMemo, useState } from "react";
import { commitCommand } from "../../api/commands";
import type { Entity, EntityRevision } from "../../api/types";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useApp } from "../../stores/appStore";

const FIELD_HINTS: Record<string, { key: string; label: string }[]> = {
  character: [
    { key: "role", label: "Role" },
    { key: "personality", label: "Personality" },
    { key: "appearance", label: "Appearance" },
    { key: "backstory", label: "Backstory" },
    { key: "visual_anchor", label: "Visual anchor (keeps them consistent)" },
  ],
  world: [
    { key: "premise", label: "Premise" },
    { key: "rules", label: "Rules of the world" },
    { key: "tone", label: "Tone" },
    { key: "era", label: "Era / time period" },
    { key: "visual_language", label: "Visual language" },
  ],
  location: [
    { key: "description", label: "Description" },
    { key: "atmosphere", label: "Atmosphere" },
    { key: "lighting", label: "Lighting" },
    { key: "props", label: "Signature props" },
  ],
};

function revOf(p: { revision: number } | undefined) {
  return p?.revision ?? 1;
}

export function EntityPage({ etype, title }: { etype: string; title: string }) {
  const project = useApp((s) => s.currentProject());
  const entitiesByType = useApp((s) => s.entities);
  const entities = entitiesByType[etype] ?? [];
  const revisions = useApp((s) => s.entityRevisions);
  const refresh = useApp((s) => s.refreshEntities);
  const refreshRevs = useApp((s) => s.refreshRevisions);
  const refreshProject = useApp((s) => s.refreshProjectData);
  const worlds = entitiesByType.world ?? [];

  const [sel, setSel] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showArchive, setShowArchive] = useState(false);
  const [revertTo, setRevertTo] = useState<number | null>(null);
  const [showArchived, setShowArchived] = useState(false);

  const entity: Entity | undefined = entities.find((e) => e.id === sel);
  const revs: EntityRevision[] = sel ? revisions[sel] ?? [] : [];
  const fields = FIELD_HINTS[etype] ?? [];
  const shown = useMemo(
    () => entities.filter((e) => showArchived || e.status === "active"),
    [entities, showArchived],
  );

  useEffect(() => {
    if (sel) void refreshRevs(etype, sel);
  }, [sel, etype, refreshRevs]);

  function startEdit(e: Entity) {
    setSel(e.id);
    setCreating(false);
    setForm({
      name: e.name,
      summary: e.summary,
      ...Object.fromEntries(fields.map((f) => [f.key, String(e.data?.[f.key] ?? "")])),
      ...(e.world_id ? { world_id: e.world_id } : {}),
    });
    setError(null);
  }

  async function run(commandType: string, payload: Record<string, unknown>) {
    if (!project) return;
    setBusy(true);
    setError(null);
    try {
      await commitCommand(project.id, revOf(project), commandType, payload);
      await Promise.all([refresh(etype), refreshProject()]);
      if (entity) void refreshRevs(etype, entity.id);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    const data: Record<string, string> = {};
    for (const f of fields) if (form[f.key]?.trim()) data[f.key] = form[f.key].trim();
    const payload: Record<string, unknown> = {
      name: form.name?.trim() ?? "",
      summary: form.summary ?? "",
      data,
    };
    if (etype === "location" && form.world_id) payload.world_id = form.world_id;
    const ok = creating
      ? await run(`${etype}.create`, payload)
      : await run(`${etype}.update`, { ...payload, [`${etype}_id`]: sel });
    if (ok) {
      setCreating(false);
      if (creating) setSel(null);
    }
  }

  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">{title}</h2>
          <p className="mt-0.5 text-sm text-muted">
            Persistent, versioned {title.toLowerCase()} — reusable across every scene in this project.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-muted">
            <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
            Show archived
          </label>
          <button
            onClick={() => {
              setCreating(true);
              setSel(null);
              setForm({});
            }}
            className="rounded-md bg-action px-3.5 py-1.5 text-sm font-medium text-white hover:bg-action-hi"
          >
            + New
          </button>
        </div>
      </div>

      <div className="mt-5 grid grid-cols-[260px_1fr] gap-5">
        {/* list */}
        <div className="space-y-1.5">
          {shown.map((e) => (
            <button
              key={e.id}
              onClick={() => startEdit(e)}
              className={`w-full rounded-md border p-3 text-left ${
                sel === e.id ? "border-teal bg-teal/10" : "border-line bg-card hover:border-faint"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="truncate text-sm font-medium">{e.name}</span>
                {e.status === "archived" && (
                  <span className="text-[10px] uppercase text-amber">archived</span>
                )}
              </div>
              {e.summary && <div className="mt-0.5 line-clamp-2 text-xs text-muted">{e.summary}</div>}
              <div className="mt-1 text-[10px] text-faint">rev {e.revision}</div>
            </button>
          ))}
          {shown.length === 0 && (
            <p className="rounded-md border border-dashed border-line p-4 text-center text-xs text-muted">
              Nothing here yet — create your first {etype}.
            </p>
          )}
        </div>

        {/* editor */}
        <div>
          {creating || entity ? (
            <div className="rounded-md border border-line bg-card p-5">
              <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">
                {creating ? `New ${etype}` : `Edit — rev ${entity?.revision}`}
              </h3>
              <label className="mt-4 block text-sm text-muted">
                Name
                <input
                  value={form.name ?? ""}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg"
                />
              </label>
              <label className="mt-3 block text-sm text-muted">
                Summary
                <textarea
                  value={form.summary ?? ""}
                  onChange={(e) => setForm({ ...form, summary: e.target.value })}
                  rows={2}
                  className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg"
                />
              </label>
              {etype === "location" && (
                <label className="mt-3 block text-sm text-muted">
                  World
                  <select
                    value={form.world_id ?? ""}
                    onChange={(e) => setForm({ ...form, world_id: e.target.value })}
                    className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg"
                  >
                    <option value="">— unassigned —</option>
                    {worlds.map((w) => (
                      <option key={w.id} value={w.id}>{w.name}</option>
                    ))}
                  </select>
                </label>
              )}
              {fields.map((f) => (
                <label key={f.key} className="mt-3 block text-sm text-muted">
                  {f.label}
                  <textarea
                    value={form[f.key] ?? ""}
                    onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}
                    rows={2}
                    className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg"
                  />
                </label>
              ))}

              {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}
              <div className="mt-5 flex items-center gap-2">
                <button
                  onClick={() => void save()}
                  disabled={busy || !form.name?.trim()}
                  className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
                >
                  {busy ? "Saving…" : creating ? "Create" : "Save new revision"}
                </button>
                {!creating && entity && (
                  <>
                    {entity.status === "active" ? (
                      <button
                        onClick={() => setShowArchive(true)}
                        className="rounded-md border border-line px-3 py-2 text-sm text-muted hover:text-fg"
                      >
                        Archive
                      </button>
                    ) : (
                      <button
                        onClick={() => void run(`${etype}.unarchive`, { [`${etype}_id`]: entity.id })}
                        className="rounded-md border border-line px-3 py-2 text-sm text-muted hover:text-fg"
                      >
                        Unarchive
                      </button>
                    )}
                  </>
                )}
              </div>

              {/* version history */}
              {!creating && revs.length > 0 && (
                <div className="mt-6 border-t border-line pt-4">
                  <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Version history</h4>
                  <ul className="mt-2 space-y-1">
                    {revs.map((r) => (
                      <li key={r.id} className="flex items-center justify-between rounded px-2 py-1.5 text-xs hover:bg-panel">
                        <span className="text-muted">
                          rev {r.revision} — {r.summary || "change"} · {new Date(r.created_at).toLocaleString()}
                        </span>
                        {r.revision !== entity?.revision && (
                          <button
                            onClick={() => setRevertTo(r.revision)}
                            className="rounded border border-line px-2 py-0.5 text-muted hover:text-fg"
                          >
                            Revert
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <div className="flex h-48 items-center justify-center rounded-md border border-dashed border-line text-sm text-muted">
              Select an item to view and edit it.
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={showArchive}
        onOpenChange={setShowArchive}
        title={`Archive "${entity?.name}"?`}
        body="It stays saved and can be unarchived later, but won't appear in pickers."
        confirmLabel="Archive"
        onConfirm={() => entity && void run(`${etype}.archive`, { [`${etype}_id`]: entity.id })}
      />
      <ConfirmDialog
        open={revertTo !== null}
        onOpenChange={(o) => !o && setRevertTo(null)}
        title={`Revert to revision ${revertTo}?`}
        body="The current state is preserved as a new revision — reverting is itself undoable."
        confirmLabel="Revert"
        onConfirm={() =>
          entity && void run(`${etype}.revert`, { [`${etype}_id`]: entity.id, to_revision: revertTo })
        }
      />
    </div>
  );
}
