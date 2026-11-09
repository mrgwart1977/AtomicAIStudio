import { useEffect } from "react";
import { connectWs, getBackendInfo } from "./api/client";
import type { Job } from "./api/types";
import { JobsPanel } from "./components/JobsPanel";
import { LeftNav } from "./components/LeftNav";
import { PromptBar } from "./components/PromptBar";
import { TopBar } from "./components/TopBar";
import { HomePage } from "./features/HomePage";
import { ModelManagerPage } from "./features/ModelManagerPage";
import { ProjectsPage } from "./features/ProjectsPage";
import { ProjectWorkspace } from "./features/workspace/ProjectWorkspace";
import { useApp, type Page, type WorkspaceTab } from "./stores/appStore";

export default function App() {
  const ready = useApp((s) => s.ready);
  const page = useApp((s) => s.page);

  useEffect(() => {
    let disposeWs: (() => void) | undefined;
    (async () => {
      await getBackendInfo();
      const s = useApp.getState();
      await s.refreshProjects().catch(() => {});
      const hash = location.hash.replace("#", "");
      if (["home", "projects", "models"].includes(hash)) s.setPage(hash as Page);
      else if (hash.startsWith("project")) {
        const t = hash.split("/")[1] as WorkspaceTab | undefined;
        if (t) useApp.setState({ workspaceTab: t });
        const first = useApp.getState().projects[0];
        if (first) s.setCurrentProject(first.id);
        s.setPage("project");
      }
      await s.refreshLocations().catch(() => {});
      s.setReady(true);
      disposeWs = connectWs(
        (e) => {
          if (e.event === "job.progress" && e.resource_id) {
            const st = useApp.getState();
            const existing = st.jobs[e.resource_id];
            st.upsertJob({
              ...(existing ?? {
                id: e.resource_id,
                project_id: e.project_id ?? "",
                adapter_id: String(e.payload.adapter_id ?? ""),
                kind: "image",
                resumable: true,
                output_asset_id: null,
                command_id: null,
                created_at: "",
                completed_at: null,
                error: null,
              }),
              status: e.payload.status as Job["status"],
              progress: Number(e.payload.progress ?? 0),
              message: String(e.payload.message ?? ""),
              error: (e.payload.error as string | null) ?? null,
              output_asset_id: (e.payload.output_asset_id as string | null) ?? null,
              progress_json: (e.payload.progress_json as Job["progress_json"]) ?? null,
              cancel_requested: Boolean(e.payload.cancel_requested ?? false),
            });
            if (e.payload.status === "succeeded") void st.refreshProjectData();
          }
        },
        (open) => useApp.getState().setWsConnected(open),
      );
    })();
    return () => disposeWs?.();
  }, []);

  if (!ready) {
    return (
      <div className="flex h-full items-center justify-center text-muted">
        Starting Atomic AI Studio…
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <TopBar />
      <div className="flex min-h-0 flex-1">
        <LeftNav />
        <main className="min-w-0 flex-1 overflow-y-auto bg-ink">
          {page === "home" && <HomePage />}
          {page === "projects" && <ProjectsPage />}
          {page === "models" && <ModelManagerPage />}
          {page === "project" && <ProjectWorkspace />}
        </main>
        <JobsPanel />
      </div>
      <PromptBar />
    </div>
  );
}
