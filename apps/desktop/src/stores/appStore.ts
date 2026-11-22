import { create } from "zustand";
import { apiFetch } from "../api/client";
import type {
  AdapterProfile,
  AssetPage,
  BackupRun,
  Entity,
  EntityAssetLink,
  EntityRevision,
  GpuSchedulerState,
  HardwareReport,
  Job,
  ModelArtifact,
  ModelCatalogEntry,
  Project,
  ProjectCard,
  StorageLocation,
  StoryboardScene,
} from "../api/types";

const TERMINAL_STATUSES = new Set<Job["status"]>(["succeeded", "failed", "cancelled"]);

function jobRank(status: string): number {
  return TERMINAL_STATUSES.has(status as Job["status"]) ? 2
    : status === "running" ? 1 : 0;
}

/** U-2b-1 R1 — freshness merge for an authoritative (HTTP) job row
 * against a possibly newer WS-derived entry. Monotonic per job:
 * queued < running < terminal; equal rank → higher progress wins.
 * cancel_requested is sticky — an older snapshot may not un-ask. */
export function mergeJobEntry(existing: Job | undefined, incoming: Job): Job {
  if (!existing) return incoming;
  const ri = jobRank(incoming.status), re = jobRank(existing.status);
  if (ri < re) return existing;
  if (ri === re && incoming.progress < existing.progress) return existing;
  return {
    ...incoming,
    cancel_requested:
      Boolean(incoming.cancel_requested) || Boolean(existing.cancel_requested),
  };
}

/** App-level ops jobs (imports, downloads, benchmarks) carry
 * project_id === null — they are global and survive project switches. */
function keepAppLevel(jobs: Record<string, Job>): Record<string, Job> {
  return Object.fromEntries(
    Object.entries(jobs).filter(([, j]) => j.project_id === null));
}

export type Page = "home" | "projects" | "models" | "providers" | "project";
export type WorkspaceTab =
  | "first_film" | "overview" | "screenplay" | "storyboard" | "timeline" | "cast" | "worlds" | "locations"
  | "generate" | "cloud" | "assets" | "backups" | "settings";
export type WorkspaceMode = "guided" | "standard" | "advanced";

interface AppState {
  ready: boolean;
  wsConnected: boolean;
  page: Page;
  workspaceTab: WorkspaceTab;
  workspaceMode: WorkspaceMode;
  projects: ProjectCard[];
  currentProjectId: string | null;
  jobs: Record<string, Job>;
  assetsPage: AssetPage | null;
  assetKindFilter: string;
  entities: Record<string, Entity[]>;
  entityRevisions: Record<string, EntityRevision[]>;
  backups: BackupRun[];
  storyboard: StoryboardScene[];
  entityAssets: EntityAssetLink[];
  locations: StorageLocation[];
  hardware: HardwareReport | null;
  adapters: AdapterProfile[];
  models: ModelCatalogEntry[];
  artifacts: ModelArtifact[];
  gpuState: GpuSchedulerState | null;
  generateShotId: string | null;
  promptDraft: string;
  setReady: (v: boolean) => void;
  setWsConnected: (v: boolean) => void;
  setPage: (p: Page) => void;
  setWorkspaceTab: (t: WorkspaceTab) => void;
  setWorkspaceMode: (m: WorkspaceMode) => void;
  setProjects: (p: ProjectCard[]) => void;
  /** Patch the stored revision pin after a successful commit so the next
   * expected_revision is correct without a full project refetch —
   * sequential commits in one handler depend on it. */
  applyCommitRevision: (projectId: string, revision: number) => void;
  setCurrentProject: (id: string | null) => void;
  upsertJob: (j: Job) => void;
  hydrateJobs: () => Promise<void>;
  setPromptDraft: (t: string) => void;
  setAssetKindFilter: (k: string) => void;
  refreshProjects: () => Promise<void>;
  refreshProjectData: () => Promise<void>;
  refreshEntities: (etype: string) => Promise<void>;
  refreshRevisions: (etype: string, entityId: string) => Promise<void>;
  refreshAssets: () => Promise<void>;
  refreshBackups: () => Promise<void>;
  refreshStoryboard: () => Promise<void>;
  refreshEntityAssets: (entityId?: string) => Promise<void>;
  refreshLocations: () => Promise<void>;
  refreshHardware: () => Promise<void>;
  refreshModels: () => Promise<void>;
  setGenerateShotId: (id: string | null) => void;
  currentProject: () => ProjectCard | undefined;
}

export const useApp = create<AppState>((set, get) => ({
  ready: false,
  wsConnected: false,
  page: "home",
  workspaceTab: "overview",
  workspaceMode: "guided",
  projects: [],
  currentProjectId: null,
  jobs: {},
  assetsPage: null,
  assetKindFilter: "",
  entities: { character: [], world: [], location: [] },
  entityRevisions: {},
  backups: [],
  storyboard: [],
  entityAssets: [],
  locations: [],
  hardware: null,
  adapters: [],
  models: [],
  artifacts: [],
  gpuState: null,
  generateShotId: null,
  promptDraft: "",
  setReady: (ready) => set({ ready }),
  setWsConnected: (wsConnected) => set({ wsConnected }),
  setPage: (page) => {
    set({ page });
    if (typeof location !== "undefined") {
      location.hash = page === "project" ? `project/${get().workspaceTab}` : page;
    }
  },
  setWorkspaceTab: (workspaceTab) => {
    set({ workspaceTab });
    if (typeof location !== "undefined" && get().page === "project") {
      location.hash = `project/${workspaceTab}`;
    }
    void get().refreshProjectData();
  },
  setWorkspaceMode: (workspaceMode) => {
    set({ workspaceMode });
    // C5-D — "Guided" depth lands on the First Film wizard.
    if (workspaceMode === "guided" && get().page === "project") {
      get().setWorkspaceTab("first_film");
    }
  },
  setProjects: (projects) => set({ projects }),
  applyCommitRevision: (projectId, revision) =>
    set((s) => ({
      projects: s.projects.map((p) =>
        p.id === projectId && revision > p.revision
          ? { ...p, revision }
          : p),
    })),
  upsertJob: (j) => set((s) => ({ jobs: { ...s.jobs, [j.id]: j } })),
  // R1 — authoritative hydration of app-level ops jobs (imports,
  // downloads, benchmarks) from GET /api/jobs. Merge-only: it never
  // removes entries and never regresses a fresher WS state, so a
  // requeued import becomes visible on (re)connect instead of waiting
  // for the backend's next progress publish. Project rows are left to
  // refreshProjectData — project views own those.
  hydrateJobs: async () => {
    const jobs = await apiFetch<Job[]>("/api/jobs");
    set((s) => {
      let changed = false;
      const next = { ...s.jobs };
      for (const j of jobs) {
        if (j.project_id !== null) continue;
        const m = mergeJobEntry(next[j.id], j);
        if (m !== next[j.id]) { next[j.id] = m; changed = true; }
      }
      return changed ? { jobs: next } : {};
    });
  },
  setPromptDraft: (promptDraft) => set({ promptDraft }),
  setAssetKindFilter: (assetKindFilter) => {
    set({ assetKindFilter });
    void get().refreshAssets();
  },
  setCurrentProject: (id) => {
    set((s) => ({
      currentProjectId: id,
      // R1 — switching views clears project-scoped state but app-level
      // ops jobs are global; dropping them hid a running import until
      // its next WS event.
      jobs: keepAppLevel(s.jobs),
      assetsPage: null,
      entities: { character: [], world: [], location: [] },
      entityRevisions: {},
      backups: [],
      storyboard: [],
      entityAssets: [],
      promptDraft: "",
    }));
    if (id) {
      void apiFetch<{ text: string }>(`/api/projects/${id}/prompt-draft`)
        .then((d) => set({ promptDraft: d.text }))
        .catch(() => {});
      void get().refreshProjectData();
      void get().refreshAssets();
      void get().refreshBackups();
      void get().refreshStoryboard();
      void get().refreshEntityAssets();
      for (const t of ["character", "world", "location"]) void get().refreshEntities(t);
    }
  },
  refreshProjects: async () => {
    const projects = await apiFetch<ProjectCard[]>("/api/projects");
    // Revisions are server-monotonic: an in-flight GET issued before a
    // commit resolved must not regress the pin it already advanced.
    set((s) => ({
      projects: projects.map((p) => {
        const cur = s.projects.find((x) => x.id === p.id);
        return cur && cur.revision > p.revision
          ? { ...p, revision: cur.revision }
          : p;
      }),
    }));
  },
  refreshProjectData: async () => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const [jobs, proj] = await Promise.all([
      apiFetch<Job[]>(`/api/projects/${pid}/jobs`),
      apiFetch<Project>(`/api/projects/${pid}`),
    ]);
    set((s) => {
      // R1 — rebuild: app-level entries are global and stay; this
      // project's rows come fresh from the API but must not regress a
      // newer WS event that landed while the fetch was in flight.
      const next = keepAppLevel(s.jobs);
      for (const j of jobs) next[j.id] = mergeJobEntry(s.jobs[j.id], j);
      return {
        jobs: next,
        // Same monotonic guard for the single-project GET: never let an
        // older response overwrite a newer pinned revision.
        projects: s.projects.map((p) => (p.id === pid
          ? { ...p, ...proj, revision: Math.max(p.revision, proj.revision) }
          : p)),
      };
    });
    void get().refreshProjects();
  },
  refreshEntities: async (etype) => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const rows = await apiFetch<Entity[]>(`/api/projects/${pid}/${etype}?include_archived=true`);
    set((s) => ({ entities: { ...s.entities, [etype]: rows } }));
  },
  refreshRevisions: async (etype, entityId) => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const revs = await apiFetch<EntityRevision[]>(
      `/api/projects/${pid}/${etype}/${entityId}/versions`,
    );
    set((s) => ({ entityRevisions: { ...s.entityRevisions, [entityId]: revs } }));
  },
  refreshAssets: async () => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const kind = get().assetKindFilter;
    const assetsPage = await apiFetch<AssetPage>(
      `/api/projects/${pid}/assets?limit=200${kind ? `&kind=${kind}` : ""}`,
    );
    set({ assetsPage });
  },
  refreshBackups: async () => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const backups = await apiFetch<BackupRun[]>(`/api/projects/${pid}/backups`);
    set({ backups });
  },
  refreshStoryboard: async () => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const storyboard = await apiFetch<StoryboardScene[]>(`/api/projects/${pid}/storyboard`);
    set({ storyboard });
  },
  refreshEntityAssets: async (entityId) => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const entityAssets = await apiFetch<EntityAssetLink[]>(
      `/api/projects/${pid}/entity-assets${entityId ? `?entity_id=${entityId}` : ""}`,
    );
    set({ entityAssets });
  },
  refreshLocations: async () => {
    const locations = await apiFetch<StorageLocation[]>("/api/storage/locations");
    set({ locations });
  },
  refreshHardware: async () => {
    const [hardware, adapters] = await Promise.all([
      apiFetch<HardwareReport>("/api/hardware"),
      apiFetch<AdapterProfile[]>("/api/adapters"),
    ]);
    set({ hardware, adapters });
  },
  refreshModels: async () => {
    const [models, gpuState, artifacts] = await Promise.all([
      apiFetch<ModelCatalogEntry[]>("/api/models"),
      apiFetch<GpuSchedulerState>("/api/gpu/scheduler"),
      apiFetch<ModelArtifact[]>("/api/models/artifacts"),
    ]);
    set({ models, gpuState, artifacts });
  },
  setGenerateShotId: (generateShotId) => set({ generateShotId }),
  currentProject: () => get().projects.find((p) => p.id === get().currentProjectId),
}));
