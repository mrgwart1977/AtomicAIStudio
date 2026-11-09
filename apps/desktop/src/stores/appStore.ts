import { create } from "zustand";
import { apiFetch } from "../api/client";
import type {
  AdapterProfile,
  AssetPage,
  BackupRun,
  Entity,
  EntityRevision,
  HardwareReport,
  Job,
  Project,
  ProjectCard,
  StorageLocation,
} from "../api/types";

export type Page = "home" | "projects" | "models" | "project";
export type WorkspaceTab = "overview" | "cast" | "worlds" | "locations" | "assets" | "backups" | "settings";
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
  locations: StorageLocation[];
  hardware: HardwareReport | null;
  adapters: AdapterProfile[];
  promptDraft: string;
  setReady: (v: boolean) => void;
  setWsConnected: (v: boolean) => void;
  setPage: (p: Page) => void;
  setWorkspaceTab: (t: WorkspaceTab) => void;
  setWorkspaceMode: (m: WorkspaceMode) => void;
  setProjects: (p: ProjectCard[]) => void;
  setCurrentProject: (id: string | null) => void;
  upsertJob: (j: Job) => void;
  setPromptDraft: (t: string) => void;
  setAssetKindFilter: (k: string) => void;
  refreshProjects: () => Promise<void>;
  refreshProjectData: () => Promise<void>;
  refreshEntities: (etype: string) => Promise<void>;
  refreshRevisions: (etype: string, entityId: string) => Promise<void>;
  refreshAssets: () => Promise<void>;
  refreshBackups: () => Promise<void>;
  refreshLocations: () => Promise<void>;
  refreshHardware: () => Promise<void>;
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
  locations: [],
  hardware: null,
  adapters: [],
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
  setWorkspaceMode: (workspaceMode) => set({ workspaceMode }),
  setProjects: (projects) => set({ projects }),
  upsertJob: (j) => set((s) => ({ jobs: { ...s.jobs, [j.id]: j } })),
  setPromptDraft: (promptDraft) => set({ promptDraft }),
  setAssetKindFilter: (assetKindFilter) => {
    set({ assetKindFilter });
    void get().refreshAssets();
  },
  setCurrentProject: (id) => {
    set({
      currentProjectId: id,
      jobs: {},
      assetsPage: null,
      entities: { character: [], world: [], location: [] },
      entityRevisions: {},
      backups: [],
      promptDraft: "",
    });
    if (id) {
      void apiFetch<{ text: string }>(`/api/projects/${id}/prompt-draft`)
        .then((d) => set({ promptDraft: d.text }))
        .catch(() => {});
      void get().refreshProjectData();
      void get().refreshAssets();
      void get().refreshBackups();
      for (const t of ["character", "world", "location"]) void get().refreshEntities(t);
    }
  },
  refreshProjects: async () => {
    const projects = await apiFetch<ProjectCard[]>("/api/projects");
    set({ projects });
  },
  refreshProjectData: async () => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const [jobs, proj] = await Promise.all([
      apiFetch<Job[]>(`/api/projects/${pid}/jobs`),
      apiFetch<Project>(`/api/projects/${pid}`),
    ]);
    set((s) => ({
      jobs: Object.fromEntries(jobs.map((j) => [j.id, j])),
      projects: s.projects.map((p) => (p.id === pid ? { ...p, ...proj } : p)),
    }));
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
  currentProject: () => get().projects.find((p) => p.id === get().currentProjectId),
}));
