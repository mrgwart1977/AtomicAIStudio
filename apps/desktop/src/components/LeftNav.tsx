import clsx from "clsx";
import { useApp, type Page } from "../stores/appStore";

interface NavItem {
  id: string;
  label: string;
  page?: Page;
  milestone?: string;
}

// Only screens with real M0 functionality are enabled; everything else is a
// visible-but-disabled placeholder entry — the full nav structure ships now,
// the features land in their mapped milestones.
const NAV: NavItem[] = [
  { id: "home", label: "Home", page: "home" },
  { id: "projects", label: "Projects", page: "projects" },
  { id: "cast", label: "Cast", milestone: "M2" },
  { id: "worlds", label: "Worlds", milestone: "M1" },
  { id: "storyboard", label: "Storyboard", milestone: "M1" },
  { id: "scene", label: "Scene Builder", milestone: "M5" },
  { id: "image", label: "Image Studio", milestone: "M3" },
  { id: "video", label: "Video Studio", milestone: "M4" },
  { id: "audio", label: "Audio Studio", milestone: "M6" },
  { id: "training", label: "Training", milestone: "M3" },
  { id: "editor", label: "Editor", milestone: "M7" },
  { id: "workflows", label: "Workflows", milestone: "M4" },
  { id: "models", label: "Model Manager", page: "models" },
];

export function LeftNav() {
  const page = useApp((s) => s.page);
  const setPage = useApp((s) => s.setPage);

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
