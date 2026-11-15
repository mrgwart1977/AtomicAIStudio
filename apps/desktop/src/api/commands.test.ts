// S-C5 — regression: commitCommand must feed the post-commit revision
// back into the store. A handler issuing sequential commits pins the
// next one against the returned revision; without the sync, the second
// commit deterministically 422s (found via installed-app UI automation).
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useApp } from "../stores/appStore";
import type { ProjectCard } from "./types";

const apiFetch = vi.fn();
vi.mock("./client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));
import { commitCommand } from "./commands";

const PROJECT: ProjectCard = {
  id: "p1", name: "Demo", status: "active", template: "blank",
  kind: "movie", aspect_ratio: "16:9", width: 1920, height: 1080,
  fps_num: 24, fps_den: 1, style: "default",
  storage_location_id: null, backup_dir: null, revision: 3,
  settings_json: {}, created_at: "t", updated_at: "t",
  counts: { characters: 0, worlds: 0, locations: 0, assets: 0 },
  running_jobs: 0, thumbnail_asset_id: null,
};

const RESULT = (revision: number) => ({
  command_id: "c", status: "committed", revision,
  idempotent_replay: false, job_id: null,
  duplicate_project_id: null, deleted: false, warnings: [], bridge: null,
});

describe("commitCommand revision pin", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    useApp.setState({ projects: [{ ...PROJECT }] });
  });

  it("syncs the returned revision into the store", async () => {
    apiFetch.mockResolvedValue(RESULT(4));
    await commitCommand("p1", 3, "project.rename", { name: "x" });
    expect(useApp.getState().projects[0].revision).toBe(4);
  });

  it("sequential commits pin against the previous result", async () => {
    apiFetch.mockResolvedValueOnce(RESULT(4)).mockResolvedValueOnce(RESULT(5));
    await commitCommand("p1", 3, "project.rename", { name: "x" });
    const pin = useApp.getState().projects[0].revision;
    await commitCommand("p1", pin, "project.update_settings", {});
    const last = apiFetch.mock.calls[1];
    expect(JSON.parse((last[1] as { body: string }).body).expected_revision).toBe(4);
  });

  it("never regresses the pin on an older revision", async () => {
    useApp.setState({ projects: [{ ...PROJECT, revision: 9 }] });
    apiFetch.mockResolvedValue(RESULT(7));
    await commitCommand("p1", 9, "project.rename", { name: "x" });
    expect(useApp.getState().projects[0].revision).toBe(9);
  });

  it("refreshProjects never regresses the pin with a stale list", async () => {
    useApp.setState({ projects: [{ ...PROJECT, revision: 33 }] });
    // An in-flight GET issued before a commit resolved lands after it.
    apiFetch.mockResolvedValue([{ ...PROJECT, revision: 32 }]);
    await useApp.getState().refreshProjects();
    expect(useApp.getState().projects[0].revision).toBe(33);
  });

  it("refreshProjectData never regresses the pin with a stale row", async () => {
    useApp.setState({
      projects: [{ ...PROJECT, revision: 33 }],
      currentProjectId: "p1",
    });
    apiFetch.mockImplementation((url: string) =>
      Promise.resolve(url.endsWith("/jobs") ? []
        : url.endsWith("/api/projects") ? [{ ...PROJECT, revision: 32 }]
        : { ...PROJECT, revision: 32 }));
    await useApp.getState().refreshProjectData();
    expect(useApp.getState().projects[0].revision).toBe(33);
  });
});
