// C2.0d — cloud text UI tests: simulation honesty, catalog-driven
// eligibility, explicit cloud opt-in, preview→approve→commit,
// budget/reconciliation surfaces, lifecycle rendering, and the
// absence of any credential collection.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { CloudPage } from "./CloudPage";

const apiFetch = vi.fn();
const commitCommand = vi.fn();
const previewCommand = vi.fn();

vi.mock("../../api/client", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
  previewCommand: (...a: unknown[]) => previewCommand(...a),
  pickFile: vi.fn(),
  pickDirectory: vi.fn(),
}));

const PROJECT = { id: "p1", revision: 7, name: "Demo" };
const store: Record<string, unknown> = {
  currentProjectId: "p1",
  currentProject: () => PROJECT,
  jobs: {},
  entities: { character: [], world: [], location: [] },
  storyboard: [],
  refreshAssets: vi.fn(),
};
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

const PROVIDERS = [
  { key: "openai", label: "OpenAI", category: "text", access_class: "A", tier: "T1" },
  { key: "anthropic", label: "Anthropic", category: "text", access_class: "A", tier: "T1" },
  { key: "google-gemini", label: "Google / Gemini", category: "text", access_class: "A", tier: "T1" },
  { key: "deepseek", label: "DeepSeek", category: "text", access_class: "A", tier: "T1" },
  { key: "xai", label: "xAI / Grok", category: "text", access_class: "A", tier: "T1" },
  { key: "inelig", label: "Inelig Co", category: "text", access_class: "C", tier: "T2" },
];

const DETAILS: Record<string, unknown> = {
  openai: {
    key: "openai", label: "OpenAI", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["text_generation"] }],
    models: [
      { id: "gpt-5", capability: "text", cost_table: { input_per_mtok: 1.25, output_per_mtok: 10.0 } },
      { id: "dall-e", capability: "image" },
    ],
  },
  anthropic: {
    key: "anthropic", label: "Anthropic", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["text_generation"] }],
    models: [{ id: "claude-opus-4-1", capability: "text", cost_table: { input_per_mtok: 15, output_per_mtok: 75 } }],
  },
  "google-gemini": {
    key: "google-gemini", label: "Google / Gemini", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["text_generation"] }],
    models: [
      { id: "gemini-2.5-pro", capability: "text", cost_table: { input_per_mtok: 1.25, output_per_mtok: 10.0 } },
      { id: "gemini-2.5-flash", capability: "text", cost_table: { input_per_mtok: 0.3, output_per_mtok: 2.5 } },
    ],
  },
  deepseek: {
    key: "deepseek", label: "DeepSeek", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["text_generation"] }],
    models: [{ id: "deepseek-chat", capability: "text", cost_table: { input_per_mtok: 0.27, output_per_mtok: 1.1 } }],
  },
  xai: {
    key: "xai", label: "xAI / Grok", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["text_generation"] }],
    models: [{ id: "grok-4", capability: "text", cost_table: { input_per_mtok: 3.0, output_per_mtok: 15.0 } }],
  },
  inelig: {
    key: "inelig", label: "Inelig Co", access_class: "C",
    strategies: [{ method: "assisted_workflow", tier: "T2", capabilities: ["text_generation"] }],
    models: [],
  },
};

const SCOPES_ZERO = ["session", "day", "month", "lifetime"].map((scope) => ({
  scope, limit_micro: null, consumed_micro: 0, open_micro: 0, charged_micro: 0,
}));

const SIM_SUM = {
  environment: "simulated", session_id: "sess-1",
  scopes: SCOPES_ZERO.map((r) => (r.scope === "day"
    ? { ...r, limit_micro: 500000, open_micro: 12500, charged_micro: 5000, consumed_micro: 17500 }
    : r)),
  budgets: [
    { id: "b1", target: "project", scope: "day", limit_micro: 500000, label: "dev cap" },
    { id: "b2", target: "global", scope: "month", limit_micro: 9000000, label: "" },
  ],
};
const LIVE_SUM = {
  environment: "live", session_id: "sess-1",
  scopes: SCOPES_ZERO, budgets: [...SIM_SUM.budgets],
};

const RESERVATIONS = [
  {
    id: "res1", job_id: "job-amb", command_id: null, project_id: "p1",
    provider_key: "openai", environment: "simulated", status: "open",
    reserved_micro: 12500, remote_job_id: "resp_x", reconcile_count: 0,
    reconcile_note: null, created_at: "2026-01-01T00:00:00Z",
  },
  {
    // Orphan: open hold whose job row is unavailable — must stay
    // visible with its exposure, never silently released.
    id: "res-orphan", job_id: "job-gone-00000000", command_id: null, project_id: "p1",
    provider_key: "anthropic", environment: "simulated", status: "open",
    reserved_micro: 25000, remote_job_id: "msg_gone", reconcile_count: 0,
    reconcile_note: null, created_at: "2026-01-01T00:00:30Z",
  },
  {
    // Terminal: already settled — no actions should be offered.
    id: "res-set", job_id: "job-set", command_id: null, project_id: "p1",
    provider_key: "openai", environment: "simulated", status: "settled",
    reserved_micro: 8000, remote_job_id: "resp_s", reconcile_count: 1,
    reconcile_note: "ok", created_at: "2026-01-01T00:00:10Z",
  },
  {
    // Live env (cannot exist today) — must render read-only.
    id: "res-live", job_id: "job-live", command_id: null, project_id: "p1",
    provider_key: "openai", environment: "live", status: "open",
    reserved_micro: 4000, remote_job_id: "resp_l", reconcile_count: 0,
    reconcile_note: null, created_at: "2026-01-01T00:00:20Z",
  },
];

const JOBS: Record<string, unknown> = {
  "job-ok": {
    id: "job-ok", project_id: "p1", adapter_id: "cloud.text.rest", kind: "generate",
    status: "succeeded", progress: 1, message: "", error: null,
    execution_scope: "remote", recovery_state: null, cancel_requested: false,
    created_at: "2026-01-01T00:02:00Z", completed_at: "2026-01-01T00:02:30Z",
    progress_json: {
      remote_intent: { provider_key: "openai", model_id: "gpt-5", lifecycle: "async_poll", simulated: true },
      remote_job_id: "resp_abc", remote_status: "completed",
      remote_outcome: "succeeded", remote_polls: 3,
      usage_json: { input_tokens: 42, output_tokens: 128 },
    },
  },
  "job-amb": {
    id: "job-amb", project_id: "p1", adapter_id: "cloud.text.rest", kind: "generate",
    status: "failed", progress: 0, message: "", error: "interrupted",
    execution_scope: "remote", recovery_state: "needs_reconciliation",
    cancel_requested: false,
    created_at: "2026-01-01T00:01:00Z", completed_at: null,
    progress_json: {
      remote_intent: { provider_key: "openai", model_id: "gpt-5", lifecycle: "async_poll", simulated: true },
      remote_job_id: "resp_x", remote_outcome: "ambiguous",
      reconcile_required: true,
    },
  },
  "job-sync": {
    id: "job-sync", project_id: "p1", adapter_id: "cloud.text.rest", kind: "generate",
    status: "running", progress: 0, message: "", error: null,
    execution_scope: "remote", recovery_state: null, cancel_requested: false,
    created_at: "2026-01-01T00:03:00Z", completed_at: null,
    progress_json: {
      remote_intent: { provider_key: "anthropic", model_id: "claude-opus-4-1", lifecycle: "sync", simulated: true },
    },
  },
  "job-gemini": {
    id: "job-gemini", project_id: "p1", adapter_id: "cloud.text.rest", kind: "generate",
    status: "succeeded", progress: 1, message: "", error: null,
    execution_scope: "remote", recovery_state: null, cancel_requested: false,
    created_at: "2026-01-01T00:04:00Z", completed_at: "2026-01-01T00:04:05Z",
    progress_json: {
      remote_intent: { provider_key: "google-gemini", model_id: "gemini-2.5-pro", lifecycle: "sync", simulated: true },
      remote_job_id: null, remote_status: "succeeded",
      remote_outcome: "succeeded",
      usage_json: { input_tokens: 19, output_tokens: 31 },
    },
  },
  "job-set": {
    id: "job-set", project_id: "p1", adapter_id: "cloud.text.rest", kind: "generate",
    status: "failed", progress: 0, message: "", error: "interrupted",
    execution_scope: "remote", recovery_state: "needs_reconciliation",
    cancel_requested: false,
    created_at: "2026-01-01T00:01:30Z", completed_at: null,
    progress_json: {
      remote_intent: { provider_key: "openai", model_id: "gpt-5", lifecycle: "async_poll", simulated: true },
      remote_job_id: "resp_s", remote_outcome: "ambiguous", reconcile_required: true,
    },
  },
  "job-live": {
    id: "job-live", project_id: "p1", adapter_id: "cloud.text.rest", kind: "generate",
    status: "failed", progress: 0, message: "", error: "interrupted",
    execution_scope: "remote", recovery_state: "needs_reconciliation",
    cancel_requested: false,
    created_at: "2026-01-01T00:01:40Z", completed_at: null,
    progress_json: {
      remote_intent: { provider_key: "openai", model_id: "gpt-5", lifecycle: "async_poll", simulated: true },
      remote_job_id: "resp_l", remote_outcome: "ambiguous", reconcile_required: true,
    },
  },
  "job-local": {
    id: "job-local", project_id: "p1", adapter_id: "image.sdxl", kind: "generate",
    status: "running", progress: 0.4, message: "", error: null,
    execution_scope: "local", recovery_state: null,
    created_at: "2026-01-01T00:00:00Z", completed_at: null, progress_json: null,
  },
};

function route(url: string, opts?: { method?: string }) {
  if (url === "/api/providers?category=text") return Promise.resolve(PROVIDERS);
  if (url.startsWith("/api/providers/")) {
    const d = DETAILS[url.split("/").pop() as string];
    return d ? Promise.resolve(d) : Promise.reject(new Error("404"));
  }
  if (url.includes("spend/summary") && url.includes("environment=simulated")) return Promise.resolve(SIM_SUM);
  if (url.includes("spend/summary") && url.includes("environment=live")) return Promise.resolve(LIVE_SUM);
  if (url.includes("spend/reservations")) return Promise.resolve(RESERVATIONS);
  if (url.includes("/cancel") && opts?.method === "POST") return Promise.resolve({ status: "cancel_requested" });
  return Promise.reject(new Error(`unmocked ${url}`));
}

beforeEach(() => {
  cleanup();
  vi.clearAllMocks();
  store.jobs = { ...JOBS };
  apiFetch.mockImplementation(route);
  previewCommand.mockResolvedValue({
    summary: "Generate cloud text", warnings: ["SIMULATED transport", "prompt text (~12 chars) → OpenAI"],
    affected: [], estimated_cost: { compute: "", currency_usd: 0.005, requires_approval: true },
    current_revision: 7, launches_generation: true,
  });
  commitCommand.mockResolvedValue({
    command_id: "cmd1", status: "committed", revision: 8,
    idempotent_replay: false, job_id: "job-new", warnings: [],
  });
});

async function fillForm() {
  render(createElement(CloudPage));
  await waitFor(() => screen.getByText("OpenAI (simulated API)"));
  fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "openai" } });
  await waitFor(() => screen.getByText("gpt-5"));
  fireEvent.change(screen.getByLabelText("Model"), { target: { value: "gpt-5" } });
  fireEvent.change(screen.getByLabelText("Prompt"), { target: { value: "Write a beat." } });
  fireEvent.change(screen.getByLabelText(/Max estimated cost/), { target: { value: "0.10" } });
}

describe("CloudPage — simulation honesty & eligibility", () => {
  it("renders the persistent simulation banner and labels providers as simulated", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("OpenAI (simulated API)"));
    expect(screen.getAllByText("SIMULATION — No external AI provider contacted.").length)
      .toBeGreaterThanOrEqual(2); // page banner + per remote job
    expect(screen.getByText("Anthropic (simulated API)")).toBeTruthy();
    expect(screen.getByText(/simulated API integrations/)).toBeTruthy();
  });

  it("offers only catalog-eligible providers (A-class + T1 official_api text)", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("OpenAI (simulated API)"));
    expect(screen.getByText("Anthropic (simulated API)")).toBeTruthy();
    // All five A-class T1 REST providers surface from the catalog —
    // the selector is data-driven, nothing is hardcoded.
    expect(screen.getByText("Google / Gemini (simulated API)")).toBeTruthy();
    expect(screen.getByText("DeepSeek (simulated API)")).toBeTruthy();
    expect(screen.getByText("xAI / Grok (simulated API)")).toBeTruthy();
    expect(screen.queryByText("Inelig Co (simulated API)")).toBeNull();
  });

  it("lists Gemini models for the new provider and computes its estimate", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("Google / Gemini (simulated API)"));
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "google-gemini" } });
    await waitFor(() => screen.getByText("gemini-2.5-pro"));
    expect(screen.getByText("gemini-2.5-flash")).toBeTruthy();
    expect(screen.queryByText("gpt-5")).toBeNull(); // other provider's models hidden
  });

  it("model picker lists only text-capable models of the chosen provider", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("OpenAI (simulated API)"));
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "openai" } });
    await waitFor(() => screen.getByText("gpt-5"));
    expect(screen.queryByText("dall-e")).toBeNull(); // image capability excluded
  });

  it("collects no credentials — no password/secret/key inputs anywhere", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("OpenAI (simulated API)"));
    expect(document.querySelector('input[type="password"]')).toBeNull();
    // No input may solicit a key/token/secret.
    for (const el of Array.from(document.querySelectorAll("input, textarea"))) {
      const label = el.closest("label")?.textContent ?? "";
      const aria = el.getAttribute("aria-label") ?? "";
      const ph = el.getAttribute("placeholder") ?? "";
      expect(`${label} ${aria} ${ph}`.toLowerCase()).not.toMatch(/api.?key|secret|token|credential/);
    }
    expect(screen.queryByText(/enter your (api )?key/i)).toBeNull();
    expect(screen.queryByText(/credential status/i)).toBeNull();
  });
});

describe("CloudPage — generate flow", () => {
  it("requires explicit cloud opt-in before preview is enabled", async () => {
    await fillForm();
    const btn = screen.getByText("Preview request") as HTMLButtonElement;
    expect(btn.disabled).toBe(true); // cloud target unchecked
    fireEvent.click(screen.getByTestId("cloud-target"));
    expect(btn.disabled).toBe(false);
  });

  it("shows a labeled cost estimate and data-out disclosure", async () => {
    await fillForm();
    // 12-char prompt → estIn=3; (3*1.25 + 512*10)/1e6 = 0.005124
    expect(screen.getByTestId("cost-estimate").textContent).toContain("$0.005124");
    expect(screen.getByTestId("cost-estimate").textContent).toContain("estimate");
    expect(screen.getByTestId("data-out").textContent).toContain("prompt text (~13 chars) → OpenAI");
  });

  it("runs preview → approve → commit with execution_target=cloud", async () => {
    await fillForm();
    fireEvent.click(screen.getByTestId("cloud-target"));
    fireEvent.click(screen.getByText("Preview request"));
    await waitFor(() => screen.getByTestId("command-approval"));
    expect(previewCommand).toHaveBeenCalledWith("p1", "generate.text.cloud",
      expect.objectContaining({
        provider_key: "openai", model_id: "gpt-5",
        prompt: "Write a beat.", execution_target: "cloud", max_cost_usd: 0.10,
      }));
    // approval panel shows warnings + its own simulation banner
    const panel = screen.getByTestId("command-approval");
    expect(panel.textContent).toContain("SIMULATION — No external AI provider contacted.");
    expect(panel.textContent).toContain("SIMULATED transport");
    expect(panel.textContent).toContain("estimate only — not a charge");
    expect(commitCommand).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Approve & commit"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "generate.text.cloud",
      expect.objectContaining({ execution_target: "cloud" }),
    ));
    await waitFor(() => screen.getByText(/Committed generate\.text\.cloud/));
  });

  it("surfaces preview API errors instead of committing", async () => {
    await fillForm();
    previewCommand.mockRejectedValueOnce(new Error("422: provider_key is not eligible"));
    fireEvent.click(screen.getByTestId("cloud-target"));
    fireEvent.click(screen.getByText("Preview request"));
    await waitFor(() => screen.getByText(/Preview failed: Error: 422: provider_key is not eligible/));
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("surfaces data-load API errors", async () => {
    apiFetch.mockImplementation((url: string) =>
      url === "/api/providers?category=text"
        ? Promise.reject(new Error("500: boom")) : route(url));
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText(/providers: Error: 500: boom/));
  });
});

describe("CloudPage — budgets", () => {
  it("separates simulated and live accounting with reserved/charged/available", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("Simulated accounting"));
    const sim = screen.getByText("Simulated accounting").closest("div")!.parentElement!;
    expect(sim.textContent).toContain("synthetic");
    expect(sim.textContent).toContain("$0.0125"); // reserved 12500µ
    expect(sim.textContent).toContain("$0.0050"); // charged 5000µ
    expect(sim.textContent).toContain("$0.4825"); // 500000-17500 available
    expect(screen.getByText("Live accounting").closest("div")!.parentElement!.textContent)
      .toContain("informational only");
  });

  it("lists project and global budgets distinctly", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("Project budgets"));
    expect(screen.getByText("Project budgets").parentElement!.textContent).toContain("day");
    expect(screen.getByText("Global budgets (all cloud spend)").parentElement!.textContent).toContain("month");
  });

  it("sets a budget via preview → commit spend.budget_set", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("Preview budget change"));
    fireEvent.change(screen.getByPlaceholderText("5.00"), { target: { value: "2.5" } });
    fireEvent.click(screen.getByText("Preview budget change"));
    await waitFor(() => screen.getByTestId("command-approval"));
    expect(previewCommand).toHaveBeenCalledWith("p1", "spend.budget_set",
      expect.objectContaining({ scope: "day", limit_usd: 2.5, target: "project" }));
    fireEvent.click(screen.getByText("Approve & commit"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "spend.budget_set", expect.objectContaining({ limit_usd: 2.5 })));
  });

  it("removes a budget via preview → commit spend.budget_remove", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("Global budgets (all cloud spend)"));
    const removes = screen.getAllByText("Remove");
    fireEvent.click(removes[1]); // global month row (sorted after project day)
    await waitFor(() => screen.getByTestId("command-approval"));
    expect(previewCommand).toHaveBeenCalledWith("p1", "spend.budget_remove",
      expect.objectContaining({ scope: "month", target: "global" }));
  });
});

describe("CloudPage — remote job lifecycle", () => {
  it("renders lifecycle states without invented progress", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("remote-job-job-ok"));
    const ok = screen.getByTestId("remote-job-job-ok");
    expect(ok.textContent).toContain("Intent recorded");
    expect(ok.textContent).toContain("synthetic remote id");
    expect(ok.textContent).toContain("Completed");
    expect(ok.textContent).toContain("succeeded");
    expect(ok.querySelector('[role="progressbar"]')).toBeNull();
    // local-scope job is never listed here
    expect(screen.queryByTestId("remote-job-job-local")).toBeNull();
  });

  it("marks ambiguous jobs as needing reconciliation", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("remote-job-job-amb"));
    expect(screen.getByTestId("remote-job-job-amb").textContent).toContain("Needs reconciliation");
  });

  it("renders a sync job with no remote id honestly (Gemini)", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("remote-job-job-gemini"));
    const g = screen.getByTestId("remote-job-job-gemini");
    expect(g.textContent).toContain("google-gemini");
    // generateContent returns no id — the Submitted step says so
    // instead of inventing a synthetic handle.
    expect(g.textContent).toContain("no remote id — sync contract");
    expect(g.textContent).toContain("Completed");
    expect(g.textContent).not.toContain("synthetic remote id");
  });

  it("offers cancel only where the provider documents it", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("remote-job-job-sync"));
    // Anthropic sync lifecycle → no cancel control, explanatory note instead
    expect(screen.getByTestId("remote-job-job-sync").textContent)
      .toContain("no single-message cancellation");
  });

  it("cancels an async job through the documented endpoint", async () => {
    store.jobs = {
      "job-run": {
        ...JOBS["job-ok"], id: "job-run", status: "running", completed_at: null,
      },
    } as Record<string, unknown>;
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("remote-job-job-run"));
    fireEvent.click(screen.getByText("Cancel"));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/jobs/job-run/cancel", { method: "POST" }));
  });
});

describe("CloudPage — reconciliation queue", () => {
  it("shows provider, job, outcome, reserved exposure and status — labeled synthetic", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("reconcile-job-amb"));
    const row = screen.getByTestId("reconcile-job-amb");
    expect(row.textContent).toContain("openai");
    expect(row.textContent).toContain("job-amb".slice(0, 8));
    expect(row.textContent).toContain("ambiguous");
    expect(row.textContent).toContain("$0.0125");
    expect(row.textContent).toContain("(synthetic)");
    expect(row.textContent).toContain("open");
  });

  it("sends still_unknown through preview → commit spend.reconcile", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("Still unknown (keep hold)"));
    fireEvent.click(screen.getByText("Still unknown (keep hold)"));
    await waitFor(() => screen.getByTestId("command-approval"));
    expect(previewCommand).toHaveBeenCalledWith("p1", "spend.reconcile",
      expect.objectContaining({ job_id: "job-amb", outcome: "still_unknown" }));
    fireEvent.click(screen.getByText("Approve & commit"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "spend.reconcile",
      expect.objectContaining({ job_id: "job-amb", outcome: "still_unknown" })));
  });

  it("shows the generation recap in the approval preview (provider/model/sim/max cost/data-out/target)", async () => {
    await fillForm();
    fireEvent.click(screen.getByTestId("cloud-target"));
    fireEvent.click(screen.getByText("Preview request"));
    await waitFor(() => screen.getByTestId("gen-approval-detail"));
    const d = screen.getByTestId("gen-approval-detail");
    expect(d.textContent).toContain("openai");
    expect(d.textContent).toContain("gpt-5");
    expect(d.textContent).toContain("simulated — mocked transport");
    expect(d.textContent).toContain("$0.1");
    expect(d.textContent).toContain('execution_target="cloud"');
    expect(d.textContent).toContain("prompt text (~13 chars)");
  });

  it("confirmed_charge collects actual_usd and accept_overage", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByText("Confirm charge…"));
    fireEvent.click(screen.getByText("Confirm charge…"));
    fireEvent.change(screen.getByLabelText("Actual amount (USD)"), { target: { value: "0.0099" } });
    fireEvent.click(screen.getByLabelText(/Accept overage/));
    fireEvent.click(screen.getByText("Preview reconcile"));
    await waitFor(() => screen.getByTestId("command-approval"));
    expect(previewCommand).toHaveBeenCalledWith("p1", "spend.reconcile",
      expect.objectContaining({
        job_id: "job-amb", outcome: "confirmed_charge",
        actual_usd: 0.0099, accept_overage: true,
      }));
  });

  it("offers no actions on an already-resolved reservation", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("reconcile-job-set"));
    const row = screen.getByTestId("reconcile-job-set");
    expect(row.textContent).toContain("settled");
    expect(row.textContent).toContain("replay idempotently");
    expect(row.querySelector("button")).toBeNull();
  });

  it("keeps live-environment rows read-only — no live billing confirmation", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("reconcile-job-live"));
    const row = screen.getByTestId("reconcile-job-live");
    expect(row.textContent).toContain("reconciliation disabled");
    expect(row.textContent).toContain("no live billing workflows");
    expect(row.querySelector("button")).toBeNull();
  });
});

describe("CloudPage — orphaned reservations", () => {
  it("keeps open holds with unavailable jobs visible with exposure and manual-recovery guidance", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("orphan-res-orphan"));
    const row = screen.getByTestId("orphan-res-orphan");
    expect(row.textContent).toContain("anthropic");
    expect(row.textContent).toContain("job-gone".slice(0, 8));
    expect(row.textContent).toContain("$0.0250");
    expect(row.textContent).toContain("(synthetic)");
    expect(row.textContent).toContain("requires manual recovery");
    expect(row.textContent).toContain("not released or reconciled automatically");
    // No buttons — nothing silently releases the hold from this surface.
    expect(row.querySelector("button")).toBeNull();
  });

  it("surfaces orphan holds in the spend view as included in Reserved", async () => {
    render(createElement(CloudPage));
    await waitFor(() => screen.getByTestId("orphan-spend-note"));
    const note = screen.getByTestId("orphan-spend-note");
    expect(note.textContent).toContain("1 open hold");
    expect(note.textContent).toContain("$0.0250");
    expect(note.textContent).toContain("included in Reserved above");
    expect(note.textContent).toContain("never released automatically");
  });
});
