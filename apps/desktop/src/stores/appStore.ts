import { create } from "zustand";
import { apiFetch } from "../api/client";
import type { AdapterProfile, Asset, HardwareReport, Job, Project } from "../api/types";

export type Page = "home" | "projects" | "models";
export type WorkspaceMode = "guided" | "standard" | "advanced";

interface AppState {
  ready: boolean;
  wsConnected: boolean;
  page: Page;
  workspaceMode: WorkspaceMode;
  projects: Project[];
  currentProjectId: string | null;
  jobs: Record<string, Job>;
  assets: Asset[];
  hardware: HardwareReport | null;
  adapters: AdapterProfile[];
  promptDraft: string;
  setReady: (v: boolean) => void;
  setWsConnected: (v: boolean) => void;
  setPage: (p: Page) => void;
  setWorkspaceMode: (m: WorkspaceMode) => void;
  setProjects: (p: Project[]) => void;
  setCurrentProject: (id: string | null) => void;
  upsertJob: (j: Job) => void;
  setPromptDraft: (t: string) => void;
  refreshProjects: () => Promise<void>;
  refreshProjectData: () => Promise<void>;
  refreshHardware: () => Promise<void>;
  currentProject: () => Project | undefined;
}

export const useApp = create<AppState>((set, get) => ({
  ready: false,
  wsConnected: false,
  page: "home",
  workspaceMode: "guided",
  projects: [],
  currentProjectId: null,
  jobs: {},
  assets: [],
  hardware: null,
  adapters: [],
  promptDraft: "",
  setReady: (ready) => set({ ready }),
  setWsConnected: (wsConnected) => set({ wsConnected }),
  setPage: (page) => {
    set({ page });
    if (typeof location !== "undefined") location.hash = page;
  },
  setWorkspaceMode: (workspaceMode) => set({ workspaceMode }),
  setProjects: (projects) => set({ projects }),
  upsertJob: (j) => set((s) => ({ jobs: { ...s.jobs, [j.id]: j } })),
  setPromptDraft: (promptDraft) => set({ promptDraft }),
  setCurrentProject: (id) => {
    set({ currentProjectId: id, jobs: {}, assets: [], promptDraft: "" });
    if (id) {
      void apiFetch<{ text: string }>(`/api/projects/${id}/prompt-draft`)
        .then((d) => set({ promptDraft: d.text }))
        .catch(() => {});
      void get().refreshProjectData();
    }
  },
  refreshProjects: async () => {
    const projects = await apiFetch<Project[]>("/api/projects");
    set({ projects });
  },
  refreshProjectData: async () => {
    const pid = get().currentProjectId;
    if (!pid) return;
    const [jobs, assets] = await Promise.all([
      apiFetch<Job[]>(`/api/projects/${pid}/jobs`),
      apiFetch<Asset[]>(`/api/projects/${pid}/assets`),
    ]);
    set({ jobs: Object.fromEntries(jobs.map((j) => [j.id, j])), assets });
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
