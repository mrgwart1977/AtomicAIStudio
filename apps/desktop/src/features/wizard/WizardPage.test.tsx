// A8 — WizardPage: 15-stage navigation, progress persistence through
// project.update_settings, resume, stale-id notices, mode switching
// (same data, view flag only), and the simulated-content honesty banner.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WizardPage } from "./WizardPage";
import { STAGES, initialState } from "./wizard";

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
vi.mock("./jobs", () => ({
  pollJob: vi.fn(() => Promise.resolve({ id: "j", status: "succeeded" })),
}));

const PROJECT = { id: "p1", revision: 3, name: "Demo Film", status: "active", settings_json: {} };
let currentProject: typeof PROJECT = PROJECT;

function stagesDerived(over: Record<string, string> = {}) {
  return STAGES.map((s, i) => ({
    id: s.id, index: i, label: s.label, required: s.required,
    status: over[s.id] ?? "ready", summary: `${s.label} summary`,
    counts: {}, warnings: [],
  }));
}
function derived(over: Record<string, unknown> = {}) {
  return {
    stages: stagesDerived(),
    entities: { keyframes: [], clips: [], dialogue: [] },
    plans: [], jobs: [], progress: null, legacy_first_film: false,
    warnings: [],
    honesty: { all_simulated: true, counts: {}, note: "sim" },
    revision: 3,
    ...over,
  };
}

let tabCalls: string[] = [];
let modeCalls: string[] = [];
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({
    currentProject: () => currentProject,
    jobs: {},
    workspaceMode: "guided",
    setWorkspaceTab: (t: string) => { tabCalls.push(t); },
    setWorkspaceMode: (m: string) => { modeCalls.push(m); },
  }),
}));
import { useApp } from "../../stores/appStore";
(useApp as unknown as { getState: () => unknown }).getState = () => ({
  currentProject: () => currentProject,
  refreshProjectData: () => Promise.resolve(),
});

function route(url: string) {
  if (url.endsWith("/wizard")) return derived();
  if (url.endsWith("/storyboard")) return [];
  if (url.endsWith("/character")) return [];
  if (url.endsWith("/location")) return [];
  if (url.endsWith("/timelines")) return [];
  if (url.endsWith("/screenplays")) return [];
  if (url.endsWith("/assemblies")) return [];
  throw new Error(`unmocked fetch ${url}`);
}

beforeEach(() => {
  cleanup();
  apiFetch.mockReset();
  commitCommand.mockReset();
  previewCommand.mockReset();
  tabCalls = []; modeCalls = [];
  apiFetch.mockImplementation((u: string) => Promise.resolve(route(u)));
  commitCommand.mockResolvedValue({ command_id: "c1", job_id: null });
  currentProject = { ...PROJECT, settings_json: {} };
});

async function renderWizard() {
  render(<WizardPage />);
  await waitFor(() => screen.getByText(/Step 1 of 15/));
}

describe("WizardPage", () => {
  it("renders the 15-stage progress rail and the honesty banner", async () => {
    await renderWizard();
    expect(screen.getAllByRole("listitem")).toHaveLength(15);
    expect(screen.getByText(/simulated/)).toBeTruthy();
    expect(screen.getByText("Your project")).toBeTruthy();
  });

  it("Next advances and persists progress via project.update_settings", async () => {
    await renderWizard();
    fireEvent.click(screen.getByText("Next →"));
    await waitFor(() => screen.getByText(/Step 2 of 15/));
    const call = commitCommand.mock.calls.find(
      (c) => c[2] === "project.update_settings");
    expect(call).toBeTruthy();
    const settings = (call![3] as { settings: { wizard: { step: number } } }).settings;
    expect(settings.wizard.step).toBe(1);
    expect(settings.wizard).toMatchObject({ version: 2, furthest: 1 });
  });

  it("Back returns without duplicating state", async () => {
    await renderWizard();
    fireEvent.click(screen.getByText("Next →"));
    await waitFor(() => screen.getByText(/Step 2 of 15/));
    fireEvent.click(screen.getByText("← Back"));
    await waitFor(() => screen.getByText(/Step 1 of 15/));
  });

  it("progress rail jumps to any reached step but not beyond furthest", async () => {
    const persisted = { ...initialState(), step: 3, furthest: 3 };
    currentProject = { ...PROJECT, settings_json: { wizard: persisted } };
    render(<WizardPage />);
    await waitFor(() => screen.getByText(/Step 4 of 15/));
    const rail = screen.getAllByRole("listitem");
    fireEvent.click(rail[0]);
    await waitFor(() => screen.getByText(/Step 1 of 15/));
    // beyond furthest stays disabled
    expect((rail[14] as HTMLButtonElement).disabled).toBe(true);
  });

  it("resumes from persisted wizard state on load", async () => {
    const persisted = { ...initialState(), step: 5, furthest: 5, mode: "advanced" as const };
    currentProject = { ...PROJECT, settings_json: { wizard: persisted } };
    render(<WizardPage />);
    await waitFor(() => screen.getByText(/Step 6 of 15/));
    expect(screen.getByText("Storyboard shots")).toBeTruthy();
  });

  it("drops stale persisted ids with notices", async () => {
    const persisted = {
      ...initialState(), step: 2, furthest: 2,
      screenplay_id: "deleted-sp", timeline_id: "deleted-tl",
    };
    currentProject = { ...PROJECT, settings_json: { wizard: persisted } };
    render(<WizardPage />);
    await waitFor(() => screen.getAllByText(/was removed/));
  });

  it("mode switch is a view flag on the same persisted state", async () => {
    await renderWizard();
    fireEvent.click(screen.getByRole("button", { name: "advanced" }));
    await waitFor(() => {
      const call = commitCommand.mock.calls.find(
        (c) => c[2] === "project.update_settings");
      expect(call).toBeTruthy();
      const settings = (call![3] as { settings: { wizard: { mode: string; step: number } } }).settings;
      // same step, same data — only the view flag changes
      expect(settings.wizard.mode).toBe("advanced");
      expect(settings.wizard.step).toBe(0);
    });
    expect(modeCalls).toEqual(["advanced"]);
  });

  it("shows a legacy notice when only first_film progress exists", async () => {
    currentProject = {
      ...PROJECT,
      settings_json: { first_film: { version: 1, step: 4 } },
    };
    apiFetch.mockImplementation((u: string) => Promise.resolve(
      u.endsWith("/wizard") ? derived({ legacy_first_film: true }) : route(u)));
    render(<WizardPage />);
    await waitFor(() => screen.getByText(/Resumed from the earlier guided flow/));
    expect(screen.getByText(/Step 9 of 15/)).toBeTruthy(); // step 4 → motion
  });

  it("deep links open workspace tabs instead of duplicating them", async () => {
    await renderWizard();
    fireEvent.click(screen.getByText(/Open the project overview/));
    expect(tabCalls).toEqual(["overview"]);
  });

  it("shows stage warnings from the derived state", async () => {
    apiFetch.mockImplementation((u: string) => Promise.resolve(
      u.endsWith("/wizard") ? derived({
        stages: stagesDerived({}).map((s) => s.id === "project"
          ? { ...s, status: "attention", warnings: ["Project is empty"] }
          : s),
      }) : route(u)));
    render(<WizardPage />);
    await waitFor(() => screen.getByText("Project is empty"));
  });
});
