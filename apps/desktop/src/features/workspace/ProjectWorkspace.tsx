import clsx from "clsx";
import { useApp, type WorkspaceTab } from "../../stores/appStore";
import { AssetsPage } from "./AssetsPage";
import { BackupsPage } from "./BackupsPage";
import { EntityPage } from "./EntityPage";
import { CloudPage } from "./CloudPage";
import { GeneratePage } from "./GeneratePage";
import { OverviewPage } from "./OverviewPage";
import { SettingsPage } from "./SettingsPage";
import { StoryboardPage } from "./StoryboardPage";

const TABS: { id: WorkspaceTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "storyboard", label: "Storyboard" },
  { id: "generate", label: "Generate" },
  { id: "cloud", label: "Cloud" },
  { id: "cast", label: "Cast" },
  { id: "worlds", label: "Worlds" },
  { id: "locations", label: "Locations" },
  { id: "assets", label: "Assets" },
  { id: "backups", label: "Backups" },
  { id: "settings", label: "Settings" },
];

export function ProjectWorkspace() {
  const project = useApp((s) => s.currentProject());
  const tab = useApp((s) => s.workspaceTab);
  const setTab = useApp((s) => s.setWorkspaceTab);
  const setPage = useApp((s) => s.setPage);
  const setProject = useApp((s) => s.setCurrentProject);

  if (!project) {
    return (
      <div className="mx-auto max-w-3xl p-10 text-center text-muted">
        <p>No project selected.</p>
        <button onClick={() => setPage("projects")} className="mt-4 rounded-md bg-action px-4 py-2 text-sm text-white">
          Choose a project
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-4 border-b border-line bg-panel px-6 pt-4">
        <button
          onClick={() => { setProject(null); setPage("projects"); }}
          className="mb-3 rounded-md border border-line px-2.5 py-1 text-xs text-muted hover:text-fg"
        >
          ← Projects
        </button>
        <div className="mb-3 min-w-0 shrink">
          <h1 className="truncate text-lg font-semibold leading-tight">{project.name}</h1>
          <p className="text-xs text-muted">
            {project.kind} · {project.aspect_ratio} · {Math.round((project.fps_num / project.fps_den) * 1000) / 1000} fps
            {project.status === "archived" ? " · archived" : ""}
          </p>
        </div>
        {/* 10 tabs can't always fit at the 1100px minimum — the nav
            scrolls horizontally instead of pushing the header wide.
            Keyboard focus auto-scrolls an off-screen tab into view;
            every tab remains a real button. */}
        <nav className="ml-auto flex min-w-0 gap-1 overflow-x-auto" aria-label="Workspace">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              aria-current={tab === t.id ? "page" : undefined}
              className={clsx(
                "shrink-0 whitespace-nowrap rounded-t-md px-3.5 py-2 text-sm",
                tab === t.id
                  ? "border-b-2 border-teal bg-card text-teal"
                  : "text-muted hover:text-fg",
              )}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "overview" && <OverviewPage />}
        {tab === "storyboard" && <StoryboardPage />}
        {tab === "generate" && <GeneratePage />}
        {tab === "cloud" && <CloudPage />}
        {tab === "cast" && <EntityPage etype="character" title="Cast" />}
        {tab === "worlds" && <EntityPage etype="world" title="Worlds" />}
        {tab === "locations" && <EntityPage etype="location" title="Locations" />}
        {tab === "assets" && <AssetsPage />}
        {tab === "backups" && <BackupsPage />}
        {tab === "settings" && <SettingsPage />}
      </div>
    </div>
  );
}
