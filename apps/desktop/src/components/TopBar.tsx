import clsx from "clsx";
import brandLogoUrl from "../assets/atomic-dream-engine-logo.png";
import { useApp, type WorkspaceMode } from "../stores/appStore";

const MODES: { id: WorkspaceMode; label: string }[] = [
  { id: "guided", label: "Guided" },
  { id: "standard", label: "Standard" },
  { id: "advanced", label: "Advanced" },
];

export function TopBar() {
  const mode = useApp((s) => s.workspaceMode);
  const setMode = useApp((s) => s.setWorkspaceMode);
  const project = useApp((s) => s.projects.find((p) => p.id === s.currentProjectId));
  const ws = useApp((s) => s.wsConnected);

  return (
    <header className="flex h-16 items-center border-b border-line bg-panel px-4">
      <div className="flex w-64 min-w-0 items-center">
        <img
          src={brandLogoUrl}
          alt="Atomic Dream Engine"
          className="h-14 w-auto max-w-full shrink object-contain object-left"
        />
      </div>
      <div className="flex-1 text-center text-sm text-muted">
        {project ? `${project.name} / ${project.template.replace("_", " ")}` : "No project open"}
        {!ws && <span className="ml-3 text-amber">(reconnecting…)</span>}
      </div>
      <nav className="flex gap-1 text-sm" aria-label="Workspace depth">
        {MODES.map((m) => (
          <button
            key={m.id}
            onClick={() => setMode(m.id)}
            aria-pressed={mode === m.id}
            className={clsx(
              "rounded px-3 py-1.5",
              mode === m.id ? "bg-card-hi text-teal" : "text-muted hover:text-fg",
            )}
          >
            {m.label}
          </button>
        ))}
      </nav>
    </header>
  );
}
