import clsx from "clsx";
import logoUrl from "../assets/logo.png";
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
    <header className="flex h-14 items-center border-b border-line bg-panel px-4">
      <div className="flex w-64 items-center gap-2 text-lg font-bold tracking-wide">
        <img src={logoUrl} alt="" className="h-8 w-8 shrink-0" aria-hidden="true" />
        <span>
          <span className="text-teal">ATOMIC</span> <span className="text-fg">DREAM ENGINE</span>
        </span>
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
