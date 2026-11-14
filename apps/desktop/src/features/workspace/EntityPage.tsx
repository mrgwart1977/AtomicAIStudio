import { useEffect, useMemo, useState } from "react";
import { commitCommand } from "../../api/commands";
import type { Asset, Entity, EntityAssetLink, EntityRevision } from "../../api/types";
import { AuthImage } from "../../components/AuthImage";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useApp } from "../../stores/appStore";

const FIELD_HINTS: Record<string, { key: string; label: string }[]> = {
  character: [
    { key: "role", label: "Role" },
    { key: "identity_notes", label: "Identity (who they are)" },
    { key: "appearance", label: "Appearance" },
    { key: "distinguishing_features", label: "Distinguishing features" },
    { key: "personality", label: "Personality" },
    { key: "behavior", label: "Behavior" },
    { key: "backstory", label: "Backstory" },
    { key: "voice_profile", label: "Voice profile" },
    { key: "speaking_style", label: "Speaking style" },
    { key: "visual_anchor", label: "Visual anchor (keeps them consistent)" },
    { key: "model_ref", label: "AI model association" },
    { key: "continuity_notes", label: "Continuity notes (identity lock baseline)" },
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

// List-valued structured fields for characters — edited as add/remove rows
// and stored as JSON arrays inside profile_json (extensible by design).
const LIST_FIELDS: Record<string, { key: string; label: string; fields: [string, string][] }[]> = {
  character: [
    { key: "expressions", label: "Facial expressions & emotional states",
      fields: [["name", "Name (e.g. subtle smile)"], ["notes", "Notes"]] },
    { key: "wardrobe", label: "Wardrobe & costume variations",
      fields: [["name", "Name (e.g. red jacket)"], ["notes", "Description"]] },
  ],
};

// profile_json fields may hold structured values (objects/arrays) written via API
// or future structured editors — display them as editable JSON so they never
// degrade to "[object Object]", and parse JSON-typed input back on save.
function fieldText(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v, null, 2);
}

function fieldValue(text: string): unknown {
  const t = text.trim();
  if (!t) return "";
  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      return JSON.parse(t);
    } catch {
      return text;
    }
  }
  return text;
}

export function EntityPage({ etype, title }: { etype: string; title: string }) {
  const project = useApp((s) => s.currentProject());
  const entitiesByType = useApp((s) => s.entities);
  const entities = entitiesByType[etype] ?? [];
  const revisions = useApp((s) => s.entityRevisions);
  const refresh = useApp((s) => s.refreshEntities);
  const refreshRevs = useApp((s) => s.refreshRevisions);
  const refreshProject = useApp((s) => s.refreshProjectData);
  const entityAssets = useApp((s) => s.entityAssets);
  const refreshEntityAssets = useApp((s) => s.refreshEntityAssets);
  const assets = useApp((s) => s.assetsPage?.items ?? []);
  const worlds = entitiesByType.world ?? [];

  const [sel, setSel] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({});
  const [lists, setLists] = useState<Record<string, Record<string, string>[]>>({});
  const [csv, setCsv] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showArchive, setShowArchive] = useState(false);
  const [revertTo, setRevertTo] = useState<number | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [search, setSearch] = useState("");
  const entity: Entity | undefined = entities.find((e) => e.id === sel);
  const revs: EntityRevision[] = sel ? revisions[sel] ?? [] : [];
  const fields = FIELD_HINTS[etype] ?? [];
  const listDefs = LIST_FIELDS[etype] ?? [];
  const shown = useMemo(() => {
    let rows = entities.filter((e) => showArchived || e.status === "active");
    const q = search.trim().toLowerCase();
    if (q) {
      rows = rows.filter((e) =>
        e.name.toLowerCase().includes(q) ||
        e.summary.toLowerCase().includes(q) ||
        JSON.stringify(e.data).toLowerCase().includes(q));
    }
    return rows;
  }, [entities, showArchived, search]);
  const imageAssets = useMemo(() => assets.filter((a) => a.kind === "image" && a.status === "ready"), [assets]);
  const selLinks = useMemo(
    () => entityAssets.filter((l) => l.entity_type === etype && l.entity_id === sel),
    [entityAssets, etype, sel],
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
      ...Object.fromEntries(fields.map((f) => [f.key, fieldText(e.data?.[f.key])])),
      ...(e.world_id ? { world_id: e.world_id } : {}),
    });
    setLists(
      Object.fromEntries(
        listDefs.map((d) => [
          d.key,
          Array.isArray(e.data?.[d.key])
            ? (e.data[d.key] as Record<string, string>[]).map((x) => ({ ...x }))
            : [],
        ]),
      ),
    );
    setCsv({
      tags: Array.isArray(e.data?.tags) ? (e.data.tags as string[]).join(", ") : "",
      loras: Array.isArray(e.data?.loras) ? (e.data.loras as string[]).join(", ") : "",
    });
    if (project) void refreshEntityAssets(e.id);
    setError(null);
  }

  async function run(commandType: string, payload: Record<string, unknown>) {
    if (!project) return;
    setBusy(true);
    setError(null);
    try {
      const rev = useApp.getState().currentProject()?.revision ?? project.revision;
      await commitCommand(project.id, rev, commandType, payload);
      await Promise.all([refresh(etype), refreshProject()]);
      if (entity) {
        void refreshRevs(etype, entity.id);
        void refreshEntityAssets(entity.id);
      }
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    const data: Record<string, unknown> = {};
    for (const f of fields) if (form[f.key]?.trim()) data[f.key] = fieldValue(form[f.key]);
    for (const d of listDefs) {
      const rows = (lists[d.key] ?? []).filter((r) => r.name?.trim());
      if (rows.length) data[d.key] = rows;
    }
    for (const k of ["tags", "loras"]) {
      const arr = (csv[k] ?? "").split(",").map((x) => x.trim()).filter(Boolean);
      if (arr.length) data[k] = arr;
    }
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
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={`Search ${title.toLowerCase()}…`}
            className="mb-2 w-full rounded-md border border-line bg-card px-3 py-1.5 text-sm text-fg placeholder:text-faint"
          />
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

              {/* structured list fields (characters: expressions, wardrobe) */}
              {listDefs.map((d) => (
                <div key={d.key} className="mt-4 rounded-md border border-line p-3">
                  <div className="flex items-center justify-between">
                    <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">{d.label}</h4>
                    <button
                      onClick={() => setLists({ ...lists, [d.key]: [...(lists[d.key] ?? []), { name: "", notes: "" }] })}
                      className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg">
                      + Add
                    </button>
                  </div>
                  <ul className="mt-2 space-y-1.5">
                    {(lists[d.key] ?? []).map((row, i) => (
                      <li key={i} className="flex items-start gap-2">
                        <div className="grid flex-1 grid-cols-2 gap-2">
                          {d.fields.map(([fk, fl], j) => (
                            <input
                              key={fk}
                              value={row[fk] ?? ""}
                              placeholder={fl}
                              onChange={(e) => {
                                const next = [...(lists[d.key] ?? [])];
                                next[i] = { ...next[i], [fk]: e.target.value };
                                setLists({ ...lists, [d.key]: next });
                              }}
                              className={`rounded border border-line bg-panel px-2 py-1 text-xs text-fg ${j === 0 ? "" : ""}`}
                            />
                          ))}
                        </div>
                        <button
                          onClick={() => setLists({ ...lists, [d.key]: (lists[d.key] ?? []).filter((_, x) => x !== i) })}
                          className="rounded border border-line px-1.5 py-0.5 text-xs text-muted hover:text-danger">✕</button>
                      </li>
                    ))}
                    {(lists[d.key] ?? []).length === 0 && (
                      <li className="text-[11px] text-faint">None yet — add one above.</li>
                    )}
                  </ul>
                </div>
              ))}

              {etype === "character" && (
                <div className="mt-3 grid grid-cols-2 gap-3">
                  <label className="block text-sm text-muted">Tags (comma-separated)
                    <input value={csv.tags ?? ""} onChange={(e) => setCsv({ ...csv, tags: e.target.value })}
                      className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg" />
                  </label>
                  <label className="block text-sm text-muted">LoRA associations (comma-separated)
                    <input value={csv.loras ?? ""} onChange={(e) => setCsv({ ...csv, loras: e.target.value })}
                      className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg" />
                  </label>
                </div>
              )}

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
                    {etype === "character" && (
                      <button
                        onClick={() => void run("character.duplicate", { character_id: entity.id })}
                        className="rounded-md border border-line px-3 py-2 text-sm text-muted hover:text-fg"
                      >
                        Duplicate
                      </button>
                    )}
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

              {/* reference images / identity assets */}
              {!creating && entity && (
                <ReferenceAssets
                  links={selLinks}
                  assets={imageAssets}
                  projectId={project!.id}
                  run={run}
                  etype={etype}
                  entityId={entity.id}
                />
              )}

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

const LINK_ROLES = ["reference", "identity", "wardrobe", "voice", "style"];

function ReferenceAssets({ links, assets, projectId, run, etype, entityId }: {
  links: EntityAssetLink[];
  assets: Asset[];
  projectId: string;
  run: (t: string, p: Record<string, unknown>) => Promise<unknown>;
  etype: string;
  entityId: string;
}) {
  const [assetId, setAssetId] = useState("");
  const [role, setRole] = useState("reference");
  const [label, setLabel] = useState("");
  const [weight, setWeight] = useState("");
  const linkedIds = new Set(links.map((l) => l.asset_id));
  const avail = assets.filter((a) => !linkedIds.has(a.id));
  const nameOf = (id: string) => assets.find((a) => a.id === id)?.name ?? "asset";

  const move = (l: EntityAssetLink, dir: -1 | 1) => {
    const ids = links.map((x) => x.id);
    const i = ids.indexOf(l.id);
    const j = i + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    void run("entity.assets.reorder", {
      entity_type: etype, entity_id: entityId, ordered_link_ids: ids,
    });
  };

  return (
    <div className="mt-6 border-t border-line pt-4">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">
        Reference images & identity assets
      </h4>
      <p className="mt-1 text-[10px] text-faint">
        References guide generation — they improve consistency but do not
        guarantee identity preservation.
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        {links.map((l, i) => (
          <div key={l.id} className="relative w-24 rounded-md border border-line bg-panel">
            <AuthImage pid={projectId} assetId={l.asset_id}
              alt={l.label || nameOf(l.asset_id)}
              className="h-16 w-full rounded-t-md object-cover"
              fallback={<div className="flex h-16 items-center justify-center text-[10px] text-faint">img</div>} />
            <div className="p-1">
              <div className="truncate text-[9px] font-medium">{l.label || nameOf(l.asset_id)}</div>
              <div className="text-[8px] uppercase text-faint">
                {l.role}{l.weight != null ? ` · w${l.weight}` : ""}
              </div>
              <div className="mt-0.5 flex justify-between text-[9px] text-muted">
                <button title="Move earlier" disabled={i === 0}
                  onClick={() => move(l, -1)}
                  className="rounded px-1 hover:text-fg disabled:opacity-30">◀</button>
                <button title="Move later" disabled={i === links.length - 1}
                  onClick={() => move(l, 1)}
                  className="rounded px-1 hover:text-fg disabled:opacity-30">▶</button>
              </div>
            </div>
            <button
              title="Unlink (asset is kept)"
              onClick={() => void run("entity.assets.unlink", { link_id: l.id })}
              className="absolute -right-1 -top-1 rounded-full border border-line bg-card px-1 text-[9px] text-muted hover:text-danger">
              ✕
            </button>
          </div>
        ))}
        {links.length === 0 && (
          <p className="text-xs text-faint">No linked assets yet — attach reference images so identity stays anchored.</p>
        )}
      </div>
      {avail.length > 0 && (
        <div className="mt-2 flex items-center gap-2">
          <select value={assetId} onChange={(e) => setAssetId(e.target.value)}
            className="flex-1 rounded-md border border-line bg-panel px-2 py-1.5 text-xs text-fg">
            <option value="">Choose an image asset…</option>
            {avail.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
          <select value={role} onChange={(e) => setRole(e.target.value)}
            className="rounded-md border border-line bg-panel px-2 py-1.5 text-xs text-fg">
            {LINK_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="label"
            className="w-28 rounded-md border border-line bg-panel px-2 py-1.5 text-xs text-fg" />
          <input value={weight} onChange={(e) => setWeight(e.target.value)}
            placeholder="weight" title="Optional guidance strength 0–2 (empty = provider default)"
            inputMode="decimal"
            className="w-16 rounded-md border border-line bg-panel px-2 py-1.5 text-xs text-fg" />
          <button
            disabled={!assetId}
            onClick={() => {
              const w = weight.trim() === "" ? undefined : Number(weight);
              if (w !== undefined && (Number.isNaN(w) || w < 0 || w > 2)) return;
              void run("entity.assets.link", {
                entity_type: etype, entity_id: entityId,
                asset_id: assetId, role, label,
                ...(w !== undefined ? { weight: w } : {}),
              });
              setAssetId(""); setLabel(""); setWeight("");
            }}
            className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg disabled:opacity-40">
            Link
          </button>
        </div>
      )}
    </div>
  );
}
