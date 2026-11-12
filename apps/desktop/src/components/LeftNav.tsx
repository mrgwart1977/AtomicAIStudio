import clsx from "clsx";
import { useApp, type Page, type WorkspaceTab } from "../stores/appStore";

interface NavItem {
  id: string;
  label: string;
  page?: Page;
  tab?: WorkspaceTab;
  milestone?: string;
}

// Only screens with real functionality are enabled; everything else is a
// visible-but-disabled placeholder entry — the full nav structure ships now,
// the features land in their mapped milestones. Bible entries deep-link into
// the active project's workspace (require an open project).
const NAV: NavItem[] = [
  { id: "home", label: "Home", page: "home" },
  { id: "projects", label: "Projects", page: "projects" },
  { id: "cast", label: "Cast", tab: "cast" },
  { id: "worlds", label: "Worlds", tab: "worlds" },
  { id: "storyboard", label: "Storyboard", tab: "storyboard" },
  { id: "scene", label: "Scene Builder", milestone: "M5" },
  { id: "image", label: "Image Studio", tab: "generate" },
  { id: "cloud", label: "Cloud Studio", tab: "cloud" },
  { id: "video", label: "Video Studio", milestone: "M4" },
  { id: "audio", label: "Audio Studio", milestone: "M6" },
  { id: "training", label: "Training", milestone: "M3" },
  { id: "editor", label: "Editor", milestone: "M7" },
  { id: "workflows", label: "Workflows", milestone: "M4" },
  { id: "models", label: "Model Manager", page: "models" },
  { id: "providers", label: "AI Providers", page: "providers" },
];

export function LeftNav() {
  const page = useApp((s) => s.page);
  const setPage = useApp((s) => s.setPage);
  const setTab = useApp((s) => s.setWorkspaceTab);
  const hasProject = useApp((s) => s.currentProjectId !== null);

  return (
    <nav className="flex w-56 flex-col gap-0.5 border-r border-line bg-panel p-3" aria-label="Primary">
      {NAV.map((item) =>
        item.page ? (
          <button
            key={item.id}
            onClick={() => setPage(item.page!)}
            aria-current={page === item.page ? "page" : undefined}
            className={clsx(
              "rounded-md px-3 py-2 text-left text-sm",
              page === item.page ? "bg-action/30 text-teal" : "text-muted hover:bg-card hover:text-fg",
            )}
          >
            {item.label}
          </button>
        ) : item.tab ? (
          <button
            key={item.id}
            disabled={!hasProject}
            title={hasProject ? undefined : "Open a project first"}
            onClick={() => {
              setTab(item.tab!);
              setPage("project");
            }}
            className={clsx(
              "rounded-md px-3 py-2 text-left text-sm",
              hasProject
                ? "text-muted hover:bg-card hover:text-fg"
                : "cursor-not-allowed text-faint",
            )}
          >
            {item.label}
          </button>
        ) : (
          <div
            key={item.id}
            title={`Available in ${item.milestone}`}
            aria-disabled="true"
            className="flex cursor-not-allowed items-center justify-between rounded-md px-3 py-2 text-sm text-faint"
          >
            {item.label}
            <span className="text-[10px] uppercase tracking-wide">{item.milestone}</span>
          </div>
        ),
      )}
    </nav>
  );
}
