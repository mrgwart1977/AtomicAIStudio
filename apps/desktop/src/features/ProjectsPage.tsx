import { useState } from "react";
import { apiFetch } from "../api/client";
import { useApp } from "../stores/appStore";
import { NewProjectDialog } from "./NewProjectDialog";

function projectMeta(p: { template: string; status: string }) {
  return `${p.template.replace("_", " ")}${p.status === "archived" ? " • archived" : ""}`;
}

export function ProjectsPage() {
  const projects = useApp((s) => s.projects);
  const currentId = useApp((s) => s.currentProjectId);
  const setProject = useApp((s) => s.setCurrentProject);
  const refresh = useApp((s) => s.refreshProjects);
  const [dialog, setDialog] = useState(false);
  const [confirmArchive, setConfirmArchive] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function runCommand(p: { id: string; revision: number }, commandType: string, payload: object, key: string) {
    setError(null);
    try {
      await apiFetch(`/api/projects/${p.id}/commands/commit`, {
        method: "POST",
        body: JSON.stringify({
          command_type: commandType,
          payload,
          scope: "project",
          origin: "projects_page",
          expected_revision: p.revision,
          idempotency_key: `${commandType}-${key}-${crypto.randomUUID().slice(0, 8)}`,
        }),
      });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="mx-auto max-w-5xl p-8">
      <h1 className="text-2xl font-bold">Your projects</h1>
      <p className="mt-1 text-muted">Independent productions, explicitly shared assets and recoverable versions.</p>
      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}

      <div className="mt-6 grid grid-cols-3 gap-5">
        {projects.map((p) => (
          <div key={p.id} className="rounded-lg border border-line bg-card p-4">
            {renaming === p.id ? (
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    void runCommand(p, "project.rename", { name: name.trim() }, "rn");
                    setRenaming(null);
                  }
                  if (e.key === "Escape") setRenaming(null);
                }}
                className="w-full rounded border border-line bg-panel px-2 py-1 text-sm"
                aria-label="Rename project"
              />
            ) : (
              <h2 className="text-lg font-semibold">{p.name}</h2>
            )}
            <p className="mt-1 text-sm capitalize text-muted">{projectMeta(p)}</p>
            <div className="mt-3 flex flex-wrap gap-2 text-xs">
              {p.status === "active" && (
                <button
                  onClick={() => setProject(p.id)}
                  className="rounded border border-teal/50 px-2.5 py-1 text-teal hover:bg-teal/10"
                >
                  {currentId === p.id ? "Open" : "Open project"}
                </button>
              )}
              <button
                onClick={() => {
                  setRenaming(p.id);
                  setName(p.name);
                }}
                className="rounded border border-line px-2.5 py-1 text-muted hover:text-fg"
              >
                Rename
              </button>
              {p.status === "active" ? (
                <button
                  onClick={() => {
                    if (confirmArchive === p.id) {
                      void runCommand(p, "project.archive", {}, "ar");
                      setConfirmArchive(null);
                    } else {
                      setConfirmArchive(p.id);
                    }
                  }}
                  className="rounded border border-amber/50 px-2.5 py-1 text-amber hover:bg-amber/10"
                >
                  {confirmArchive === p.id ? "Confirm archive?" : "Archive"}
                </button>
              ) : (
                <button
                  onClick={() => void runCommand(p, "project.restore", {}, "rs")}
                  className="rounded border border-teal/50 px-2.5 py-1 text-teal hover:bg-teal/10"
                >
                  Restore
                </button>
              )}
            </div>
          </div>
        ))}

        <div
          className="cursor-not-allowed rounded-lg border border-line bg-card p-4 opacity-50"
          title="Available in M1 — explicit opt-in sharing"
        >
          <h2 className="text-lg font-semibold">Shared library</h2>
          <p className="mt-1 text-sm text-muted">Copy or link an approved version deliberately.</p>
          <span className="mt-2 inline-block text-xs text-faint">M1</span>
        </div>
        <div
          className="cursor-not-allowed rounded-lg border border-line bg-card p-4 opacity-50"
          title="Available in M1 — backup & recovery"
        >
          <h2 className="text-lg font-semibold">Backups</h2>
          <p className="mt-1 text-sm text-muted">Last verified save • Restore and inspect integrity</p>
          <span className="mt-2 inline-block text-xs text-faint">M1</span>
        </div>
        <button
          onClick={() => setDialog(true)}
          className="rounded-lg border border-dashed border-line bg-card p-4 text-left hover:border-teal/50"
        >
          <h2 className="text-lg font-semibold">New project</h2>
          <p className="mt-1 text-sm text-muted">Movie / Series / Short / Blank template</p>
        </button>
      </div>

      <NewProjectDialog open={dialog} onOpenChange={setDialog} />
    </div>
  );
}
