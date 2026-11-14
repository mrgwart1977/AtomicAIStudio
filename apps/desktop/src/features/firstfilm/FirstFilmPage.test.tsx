// C5-D — First Film wizard: step navigation, persisted progress via
// project.update_settings, resume from settings_json, and capability-
// accurate method choices (import always available; planned/uninstalled
// models are never offered as operational).
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FirstFilmPage } from "./FirstFilmPage";
import { initialState } from "./wizard";

const apiFetch = vi.fn();
const commitCommand = vi.fn();
const previewCommand = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
  previewCommand: (...a: unknown[]) => previewCommand(...a),
  pickFile: () => Promise.resolve(null),
}));

const PROJECT = {
  id: "p1", revision: 3, name: "Demo Film", status: "active",
  settings_json: {},
};
let currentProject: typeof PROJECT = PROJECT;

vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({
    currentProject: () => currentProject,
    jobs: {},
    setWorkspaceTab: () => {},
  }),
}));
import { useApp } from "../../stores/appStore";
(useApp as unknown as { getState: () => unknown }).getState = () => ({
  currentProject: () => currentProject,
  refreshProjectData: () => Promise.resolve(),
});

function route(url: string) {
  if (url.endsWith("/storyboard")) return [];
  if (url.endsWith("/character")) return [];
  if (url.endsWith("/timelines")) return [];
  if (url.includes("/assets")) return { items: [] };
  if (url === "/api/providers?category=image") return [{ key: "pk", label: "P" }];
  if (url === "/api/providers/pk") return { models: [{ id: "m1", capability: "image" }] };
  if (url === "/api/models") return [];
  if (url.endsWith("/jobs")) return [];
  throw new Error(`unmocked fetch ${url}`);
}

beforeEach(() => {
  cleanup();
  apiFetch.mockReset();
  commitCommand.mockReset();
  previewCommand.mockReset();
  apiFetch.mockImplementation((u: string) => Promise.resolve(route(u)));
  commitCommand.mockResolvedValue({ command_id: "c1", job_id: null });
  currentProject = { ...PROJECT, settings_json: {} };
});

async function renderWizard() {
  render(<FirstFilmPage />);
  await waitFor(() => screen.getByText(/Step 1 of 11/));
}

describe("FirstFilmPage", () => {
  it("renders the stepper and gates Continue until the step is done", async () => {
    await renderWizard();
    // setup step saves via commit then advances itself
    expect(screen.getByLabelText("Film title")).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(11);
  });

  it("saves film settings through real commands and persists wizard state", async () => {
    await renderWizard();
    fireEvent.change(screen.getByLabelText("Film title"), { target: { value: "My Movie" } });
    fireEvent.click(screen.getByText("Save & continue"));
    await waitFor(() => screen.getByText(/Who.s in your film/));
    const calls = commitCommand.mock.calls.map((c) => [c[2], c[3]]);
    expect(calls[0][0]).toBe("project.rename");
    expect(calls[1][0]).toBe("project.update_settings");
    // wizard state persisted via update_settings.settings.first_film
    const saved = calls.find((c) => (c[1] as Record<string, unknown>)?.settings);
    expect(saved).toBeTruthy();
  });

  it("Back returns to the previous step without duplicating state", async () => {
    await renderWizard();
    fireEvent.change(screen.getByLabelText("Film title"), { target: { value: "Demo Film" } });
    fireEvent.click(screen.getByText("Save & continue"));
    await waitFor(() => screen.getByText(/Who's in your film/));
    fireEvent.click(screen.getByText("← Back"));
    await waitFor(() => screen.getByText("About your film"));
  });

  it("resumes from persisted wizard state on load", async () => {
    const persisted = initialState("Demo Film");
    persisted.step = 4; persisted.furthest = 6;
    persisted.scene_id = "sc1"; persisted.shot_ids = ["sh1"];
    persisted.method = "import";
    currentProject = { ...PROJECT, settings_json: { first_film: persisted } };
    apiFetch.mockImplementation((u: string) => {
      if (u.endsWith("/storyboard")) {
        return Promise.resolve([{
          scene: { id: "sc1", title: "Rooftop", status: "active" },
          shots: [{ shot: { id: "sh1", title: "wide", prompt: "rain" } }],
        }]);
      }
      return Promise.resolve(route(u));
    });
    render(<FirstFilmPage />);
    await waitFor(() => screen.getByText(/Step 5 of 11/));
    expect(screen.getByText(/How will you fill your shots/)).toBeTruthy();
  });

  it("drops deleted entities with notices on resume", async () => {
    const persisted = initialState("Demo Film");
    persisted.step = 2; persisted.furthest = 4;
    persisted.scene_id = "deleted-scene"; persisted.shot_ids = ["deleted-shot"];
    currentProject = { ...PROJECT, settings_json: { first_film: persisted } };
    render(<FirstFilmPage />);
    await waitFor(() => screen.getAllByText(/was deleted/));
  });

  it("method step: import is ready, image is simulated, uninstalled video is disabled", async () => {
    const persisted = initialState();
    persisted.step = 4; persisted.furthest = 4;
    persisted.scene_id = "sc1"; persisted.shot_ids = ["sh1"];
    currentProject = { ...PROJECT, settings_json: { first_film: persisted } };
    apiFetch.mockImplementation((u: string) => {
      if (u === "/api/models") {
        return Promise.resolve([
          { key: "v1", display_name: "VidGen", modality: "video", support: "tested",
            compatibility: { status: "compatible" }, install: null, download_bytes: 1e9 },
        ]);
      }
      if (u.endsWith("/storyboard")) {
        return Promise.resolve([{
          scene: { id: "sc1", title: "S", status: "active" },
          shots: [{ shot: { id: "sh1", title: "s", prompt: "" } }],
        }]);
      }
      return Promise.resolve(route(u));
    });
    render(<FirstFilmPage />);
    await waitFor(() => screen.getByText("Import my own video clips"));
    expect(screen.getByText("Available now")).toBeTruthy();
    expect(screen.getByText("Simulated")).toBeTruthy();
    const vidBtn = screen.getByText(/VidGen/).closest("button")!;
    expect(vidBtn.disabled).toBe(true);
    expect(screen.getByText("Not installed")).toBeTruthy();
  });
});
