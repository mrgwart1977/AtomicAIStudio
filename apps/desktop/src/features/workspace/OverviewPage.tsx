import { useMemo } from "react";
import { useApp } from "../../stores/appStore";

export function OverviewPage() {
  const project = useApp((s) => s.currentProject());
  const entities = useApp((s) => s.entities);
  const assetsPage = useApp((s) => s.assetsPage);
  const jobs = useApp((s) => s.jobs);
  const backups = useApp((s) => s.backups);
  const setTab = useApp((s) => s.setWorkspaceTab);

  const jobList = useMemo(
    () =>
      Object.values(jobs).sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")).slice(0, 6),
    [jobs],
  );

  if (!project) return null;

  const stats = [
    { label: "Cast", n: entities.character?.filter((e) => e.status === "active").length ?? 0, tab: "cast" as const },
    { label: "Worlds", n: entities.world?.filter((e) => e.status === "active").length ?? 0, tab: "worlds" as const },
    { label: "Locations", n: entities.location?.filter((e) => e.status === "active").length ?? 0, tab: "locations" as const },
    { label: "Assets", n: assetsPage?.total ?? 0, tab: "assets" as const },
    { label: "Backups", n: backups.filter((b) => b.status === "succeeded").length, tab: "backups" as const },
  ];

  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="grid grid-cols-5 gap-4">
        {stats.map((s) => (
          <button
            key={s.label}
            onClick={() => setTab(s.tab)}
            className="rounded-lg border border-line bg-card p-4 text-left hover:border-teal/50"
          >
            <div className="text-2xl font-bold">{s.n}</div>
            <div className="mt-1 text-xs uppercase tracking-wide text-muted">{s.label}</div>
          </button>
        ))}
      </div>

      <div className="mt-6 grid grid-cols-2 gap-5">
        <div className="rounded-lg border border-line bg-card p-4">
          <h3 className="text-sm font-semibold">Project format</h3>
          <dl className="mt-3 space-y-1.5 text-sm">
            <div className="flex justify-between"><dt className="text-muted">Type</dt><dd>{project.kind}</dd></div>
            <div className="flex justify-between"><dt className="text-muted">Frame</dt><dd>{project.width}×{project.height} ({project.aspect_ratio})</dd></div>
            <div className="flex justify-between"><dt className="text-muted">Frame rate</dt><dd>{Math.round((project.fps_num / project.fps_den) * 1000) / 1000} fps</dd></div>
            <div className="flex justify-between"><dt className="text-muted">Style</dt><dd>{project.style === "none" ? "—" : project.style}</dd></div>
            <div className="flex justify-between"><dt className="text-muted">Revision</dt><dd>{project.revision}</dd></div>
          </dl>
        </div>
        <div className="rounded-lg border border-line bg-card p-4">
          <h3 className="text-sm font-semibold">Recent activity</h3>
          {jobList.length === 0 ? (
            <p className="mt-3 text-sm text-muted">No jobs yet — everything you generate will appear here.</p>
          ) : (
            <ul className="mt-3 space-y-2">
              {jobList.map((j) => (
                <li key={j.id} className="flex items-center justify-between text-sm">
                  <span className="truncate text-muted">{j.kind}</span>
                  <span className={
                    j.status === "succeeded" ? "text-teal" : j.status === "failed" ? "text-danger" : "text-amber"
                  }>
                    {j.status === "running" || j.status === "queued" ? `${j.status} ${Math.round(j.progress * 100)}%` : j.status}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
