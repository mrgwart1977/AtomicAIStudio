import { useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { AuthImage } from "../components/AuthImage";
import type { CommitResult, ProjectCard } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { RenameDialog } from "../components/RenameDialog";
import { useApp } from "../stores/appStore";
import { NewProjectDialog } from "./NewProjectDialog";

type SortKey = "updated_at" | "name" | "created_at";


async function runCommand(
  p: { id: string; revision: number },
  commandType: string,
  payload: object,
): Promise<CommitResult> {
  return apiFetch<CommitResult>(`/api/projects/${p.id}/commands/commit`, {
    method: "POST",
    body: JSON.stringify({
      command_type: commandType,
      payload,
      scope: "project",
      origin: "projects_page",
      expected_revision: p.revision,
      idempotency_key: `${commandType}-${crypto.randomUUID()}`,
    }),
  });
}

function Card({
  p, onOpen, onAction,
}: {
  p: ProjectCard;
  onOpen: () => void;
  onAction: (action: string) => void;
}) {
  const [menu, setMenu] = useState(false);
  return (
    <div className="group relative overflow-hidden rounded-lg border border-line bg-card">
      <button onClick={onOpen} className="block w-full text-left">
        <div className="flex h-28 items-center justify-center bg-panel text-faint">
          <AuthImage
            pid={p.id}
            assetId={p.thumbnail_asset_id}
            alt=""
            className="h-full w-full object-cover"
            fallback={<span className="text-xs uppercase tracking-widest">{p.kind}</span>}
          />
        </div>
        <div className="p-3">
          <div className="flex items-center justify-between">
            <h2 className="truncate font-semibold">{p.name}</h2>
            {p.status === "archived" && (
              <span className="rounded bg-amber/15 px-1.5 py-0.5 text-[10px] uppercase text-amber">archived</span>
            )}
          </div>
          <p className="mt-0.5 text-xs text-muted">
            {p.aspect_ratio} · {p.fps_num / p.fps_den || p.fps_num} fps · updated{" "}
            {new Date(p.updated_at).toLocaleDateString()}
          </p>
          <p className="mt-1 text-xs text-faint">
            {p.counts.characters} cast · {p.counts.worlds} worlds · {p.counts.assets} assets
            {p.running_jobs > 0 ? ` · ${p.running_jobs} job${p.running_jobs > 1 ? "s" : ""} running` : ""}
          </p>
        </div>
      </button>
      <div className="relative border-t border-line px-2 py-1.5">
        <button
          onClick={() => setMenu((v) => !v)}
          className="rounded px-2 py-1 text-xs text-muted hover:bg-card-hi hover:text-fg"
        >
          Actions ▾
        </button>
        {menu && (
          <div className="absolute bottom-8 left-2 z-10 w-40 rounded-md border border-line bg-panel py-1 shadow-lg">
            {p.status === "active" ? (
              <>
                <MenuBtn onClick={() => onAction("rename")}>Rename…</MenuBtn>
                <MenuBtn onClick={() => onAction("duplicate")}>Duplicate</MenuBtn>
                <MenuBtn onClick={() => onAction("archive")}>Archive</MenuBtn>
                <MenuBtn danger onClick={() => onAction("delete")}>Delete…</MenuBtn>
              </>
            ) : (
              <>
                <MenuBtn onClick={() => onAction("restore")}>Restore</MenuBtn>
                <MenuBtn danger onClick={() => onAction("delete")}>Delete…</MenuBtn>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function MenuBtn({ children, onClick, danger }: { children: React.ReactNode; onClick: () => void; danger?: boolean }) {
  return (
    <button
      onClick={onClick}
      className={`block w-full px-3 py-1.5 text-left text-xs ${
        danger ? "text-danger hover:bg-danger/10" : "text-muted hover:bg-card hover:text-fg"
      }`}
    >
      {children}
    </button>
  );
}

export function ProjectsPage() {
  const projects = useApp((s) => s.projects);
  const setProject = useApp((s) => s.setCurrentProject);
  const setPage = useApp((s) => s.setPage);
  const refresh = useApp((s) => s.refreshProjects);

  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("updated_at");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [kindFilter, setKindFilter] = useState<string>("all");
  const [wizard, setWizard] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [renameFor, setRenameFor] = useState<ProjectCard | null>(null);
  const [deleteFor, setDeleteFor] = useState<ProjectCard | null>(null);

  const shown = useMemo(() => {
    let list = projects;
    if (search) list = list.filter((p) => p.name.toLowerCase().includes(search.toLowerCase()));
    if (statusFilter !== "all") list = list.filter((p) => p.status === statusFilter);
    if (kindFilter !== "all") list = list.filter((p) => p.kind === kindFilter);
    return [...list].sort((a, b) => {
      if (sort === "name") return a.name.localeCompare(b.name);
      return sort === "created_at"
        ? b.created_at.localeCompare(a.created_at)
        : b.updated_at.localeCompare(a.updated_at);
    });
  }, [projects, search, sort, statusFilter, kindFilter]);

  async function action(p: ProjectCard, act: string) {
    setError(null);
    try {
      if (act === "rename") {
        setRenameFor(p);
        return;
      }
      if (act === "delete") {
        setDeleteFor(p);
        return;
      }
      if (act === "duplicate") await runCommand(p, "project.duplicate", { name: `${p.name} (copy)` });
      else if (act === "archive") await runCommand(p, "project.archive", {});
      else if (act === "restore") await runCommand(p, "project.restore", {});
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="mx-auto max-w-6xl p-8">
      <div className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-bold">Your projects</h1>
          <p className="mt-1 text-muted">Independent productions — isolated, versioned, backed up.</p>
        </div>
        <button
          onClick={() => setWizard(true)}
          className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi"
        >
          + New project
        </button>
      </div>

      <div className="mt-5 flex flex-wrap items-center gap-3">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search projects…"
          aria-label="Search projects"
          className="w-64 rounded-md border border-line bg-card px-3 py-1.5 text-sm"
        />
        <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)} aria-label="Sort"
          className="rounded-md border border-line bg-card px-2 py-1.5 text-sm">
          <option value="updated_at">Recently updated</option>
          <option value="created_at">Recently created</option>
          <option value="name">Name</option>
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} aria-label="Status"
          className="rounded-md border border-line bg-card px-2 py-1.5 text-sm">
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="archived">Archived</option>
        </select>
        <select value={kindFilter} onChange={(e) => setKindFilter(e.target.value)} aria-label="Type"
          className="rounded-md border border-line bg-card px-2 py-1.5 text-sm">
          <option value="all">All types</option>
          <option value="movie">Movie</option>
          <option value="short">Short film</option>
          <option value="scene">Scene</option>
          <option value="image">Image</option>
        </select>
        <span className="text-xs text-faint">{shown.length} project{shown.length === 1 ? "" : "s"}</span>
      </div>

      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}

      <div className="mt-6 grid grid-cols-3 gap-5">
        {shown.map((p) => (
          <Card
            key={p.id}
            p={p}
            onOpen={() => {
              setProject(p.id);
              setPage("project");
            }}
            onAction={(a) => void action(p, a)}
          />
        ))}
      </div>
      {shown.length === 0 && (
        <p className="mt-10 text-center text-sm text-muted">
          No projects match. Create your first project to get started.
        </p>
      )}

      <NewProjectDialog open={wizard} onOpenChange={setWizard} />

      {renameFor && (
        <RenameDialog
          open
          onOpenChange={(o) => !o && setRenameFor(null)}
          title={`Rename "${renameFor.name}"`}
          initialValue={renameFor.name}
          onSubmit={(name) => {
            void runCommand(renameFor, "project.rename", { name })
              .then(refresh)
              .catch((e) => setError(String(e)));
          }}
        />
      )}
      {deleteFor && (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setDeleteFor(null)}
          title={`Delete "${deleteFor.name}"?`}
          body="This permanently removes the project, its characters, worlds, assets and history. Existing backup packages are not affected."
          confirmLabel="Delete project"
          requireText={deleteFor.name}
          onConfirm={() => {
            void runCommand(deleteFor, "project.delete", { confirm_name: deleteFor.name })
              .then(refresh)
              .catch((e) => setError(String(e)));
          }}
        />
      )}
    </div>
  );
}
