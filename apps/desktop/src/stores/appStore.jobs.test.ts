// U-2b-1 R1 — authoritative job hydration + freshness merge.
// App-level ops jobs (project_id === null) must stay discoverable after
// restart and project navigation, and an older HTTP snapshot may never
// regress a newer WebSocket-derived state.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useApp, mergeJobEntry } from "./appStore";
import type { Job } from "../api/types";

const apiFetch = vi.fn();
vi.mock("../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));

/** A requeued import as GET /api/jobs returns it — note the DB rows
 * carry no source path: request_json is never serialized. */
const APP_IMPORT: Job = {
  id: "jimp", project_id: null, adapter_id: "ops.model.import",
  kind: "model_import", status: "queued", progress: 0,
  message: "re-queued after restart", error: null,
  resumable: true, output_asset_id: null, command_id: null,
  cancel_requested: false, attempt_no: 2, execution_scope: "local",
  recovery_state: "interrupted",
  created_at: "2026-01-01T00:00:00Z", completed_at: null,
};

const PROJ_JOB: Job = {
  ...APP_IMPORT, id: "jproj", project_id: "p1",
  adapter_id: "gen.image.sdcpp", kind: "image", status: "running",
  progress: 0.5, message: "rendering",
};

function mockApi(appJobs: unknown[] = [], projJobs: unknown[] = []) {
  apiFetch.mockImplementation((path: string) => {
    if (path === "/api/jobs") return Promise.resolve(appJobs);
    if (path === "/api/projects/p1/jobs") return Promise.resolve(projJobs);
    if (path === "/api/projects/p1") return Promise.resolve({});
    return Promise.resolve([]);
  });
}

const jobsOf = () => useApp.getState().jobs;
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("hydrateJobs — app-level job hydration (R1)", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    useApp.setState({
      jobs: {}, currentProjectId: null, projects: [], promptDraft: "",
      assetsPage: null, backups: [], storyboard: [], entityAssets: [],
      entities: { character: [], world: [], location: [] },
      entityRevisions: {},
    });
  });

  it("surfaces a requeued import before its next WS event", async () => {
    mockApi([APP_IMPORT]);
    await useApp.getState().hydrateJobs();
    expect(apiFetch).toHaveBeenCalledWith("/api/jobs");
    const j = jobsOf().jimp;
    expect(j.status).toBe("queued");
    expect(j.message).toBe("re-queued after restart");
    expect(j.kind).toBe("model_import");
  });

  it("does not hydrate project-scoped rows — project views own those", async () => {
    mockApi([APP_IMPORT, PROJ_JOB]);
    await useApp.getState().hydrateJobs();
    expect(jobsOf().jimp).toBeTruthy();
    expect(jobsOf().jproj).toBeUndefined();
  });

  it("a stale snapshot cannot regress a newer WS event", async () => {
    useApp.setState({ jobs: { jimp: { ...APP_IMPORT, status: "running", progress: 0.9, message: "hashing" } } });
    mockApi([APP_IMPORT]); // queued@0 snapshot taken before the WS event
    await useApp.getState().hydrateJobs();
    expect(jobsOf().jimp.status).toBe("running");
    expect(jobsOf().jimp.progress).toBe(0.9);
  });

  it("a hydrated terminal row outranks a stale running entry", async () => {
    useApp.setState({ jobs: { jimp: { ...APP_IMPORT, status: "running", progress: 0.5 } } });
    mockApi([{ ...APP_IMPORT, status: "succeeded", progress: 1,
               completed_at: "2026-01-01T00:01:00Z" }]);
    await useApp.getState().hydrateJobs();
    expect(jobsOf().jimp.status).toBe("succeeded");
    expect(jobsOf().jimp.completed_at).toBe("2026-01-01T00:01:00Z");
  });

  it("an existing terminal entry beats a stale hydrated non-terminal", async () => {
    useApp.setState({ jobs: { jimp: { ...APP_IMPORT, status: "cancelled", progress: 0.4 } } });
    mockApi([APP_IMPORT]); // snapshot from before the cancel landed
    await useApp.getState().hydrateJobs();
    expect(jobsOf().jimp.status).toBe("cancelled");
  });

  it("cancel_requested is sticky across hydration", async () => {
    useApp.setState({ jobs: { jimp: { ...APP_IMPORT, status: "running", progress: 0.4, cancel_requested: true } } });
    mockApi([{ ...APP_IMPORT, status: "running", progress: 0.6 }]);
    await useApp.getState().hydrateJobs();
    expect(jobsOf().jimp.progress).toBe(0.6);
    expect(jobsOf().jimp.cancel_requested).toBe(true);
  });

  it("a WS event after hydration wins normally", async () => {
    mockApi([APP_IMPORT]);
    await useApp.getState().hydrateJobs();
    useApp.getState().upsertJob({ ...APP_IMPORT, status: "running", progress: 0.5 });
    expect(jobsOf().jimp.status).toBe("running");
    expect(jobsOf().jimp.progress).toBe(0.5);
  });

  it("is idempotent — repeated hydration adds no duplicate entries", async () => {
    mockApi([APP_IMPORT]);
    await useApp.getState().hydrateJobs();
    await useApp.getState().hydrateJobs();
    expect(Object.keys(jobsOf())).toEqual(["jimp"]);
  });

  it("hydrated rows carry no source path or request payload", async () => {
    mockApi([APP_IMPORT]);
    await useApp.getState().hydrateJobs();
    const j = jobsOf().jimp as Record<string, unknown>;
    for (const k of Object.keys(j)) {
      expect(["source_path", "request_json", "path", "source_uri"]).not.toContain(k);
    }
  });
});

describe("project navigation — app-level jobs survive (R1)", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    useApp.setState({
      jobs: {}, currentProjectId: null, projects: [], promptDraft: "",
      assetsPage: null, backups: [], storyboard: [], entityAssets: [],
      entities: { character: [], world: [], location: [] },
      entityRevisions: {},
    });
  });

  it("entering a project keeps app jobs, drops other-project jobs", async () => {
    useApp.setState({
      jobs: {
        jimp: { ...APP_IMPORT, status: "running", progress: 0.3 },
        jother: { ...PROJ_JOB, id: "jother", project_id: "p9" },
      },
    });
    mockApi([], [PROJ_JOB]);
    useApp.getState().setCurrentProject("p1");
    // synchronous portion: app job preserved, other project dropped
    expect(jobsOf().jimp.status).toBe("running");
    expect(jobsOf().jother).toBeUndefined();
    await vi.waitFor(() => expect(jobsOf().jproj).toBeTruthy());
    expect(jobsOf().jproj.status).toBe("running");
    expect(jobsOf().jimp).toBeTruthy();
  });

  it("returning to app views keeps the hydrated import visible", async () => {
    useApp.setState({ jobs: { jimp: APP_IMPORT } });
    mockApi();
    useApp.getState().setCurrentProject("p1");
    useApp.getState().setCurrentProject(null);
    expect(jobsOf().jimp.status).toBe("queued");
  });

  it("a stale project fetch cannot regress a newer WS project job", async () => {
    useApp.setState({
      currentProjectId: "p1",
      jobs: { jproj: { ...PROJ_JOB, status: "running", progress: 0.9 } },
    });
    mockApi([], [{ ...PROJ_JOB, status: "queued", progress: 0 }]);
    await useApp.getState().refreshProjectData();
    expect(jobsOf().jproj.status).toBe("running");
    expect(jobsOf().jproj.progress).toBe(0.9);
  });
});

describe("mergeJobEntry — monotonic freshness guard", () => {
  it("accepts the incoming row when nothing is stored", () => {
    expect(mergeJobEntry(undefined, APP_IMPORT)).toBe(APP_IMPORT);
  });
  it("prefers the higher-rank status", () => {
    const running = { ...APP_IMPORT, status: "running" as const, progress: 0.9 };
    expect(mergeJobEntry(running, APP_IMPORT)).toBe(running);
    expect(mergeJobEntry(APP_IMPORT, running).status).toBe("running");
    expect(mergeJobEntry(APP_IMPORT, running).progress).toBe(0.9);
  });
});
