// C3-B — cloud image UI tests: simulation honesty, contract-gated
// provider selection, preview invalidation, duplicate-submit guard,
// job/financial status separation, asset navigation, and the absence
// of any credential or sensitive-URL surface.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { CloudImagePanel } from "./CloudImagePanel";

const apiFetch = vi.fn();
const commitCommand = vi.fn();
const previewCommand = vi.fn();
const setWorkspaceTab = vi.fn();
const setAssetKindFilter = vi.fn();
const refreshAssets = vi.fn();

vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
  getBackendInfo: () => Promise.resolve({ baseUrl: "http://test", token: "tk" }),
  assetFileUrl: (pid: string, aid: string, info: { baseUrl: string; token: string }) =>
    `${info.baseUrl}/api/projects/${pid}/assets/${aid}/file?token=${info.token}`,
}));
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
  previewCommand: (...a: unknown[]) => previewCommand(...a),
}));

const PROJECT = { id: "p1", revision: 7, name: "Demo" };
const store: Record<string, unknown> = {
  currentProjectId: "p1",
  currentProject: () => PROJECT,
  jobs: {},
  entities: { character: [{ id: "ch1", name: "Ava", status: "active" }], world: [], location: [] },
  storyboard: [
    { scene: { id: "sc1", title: "Opening" },
      shots: [{ shot: { id: "sh1", title: "Wide" }, cast: [], inherited: [] }],
      cast: [] },
    { scene: { id: "sc2", title: "Chase" },
      shots: [{ shot: { id: "sh2", title: "Closeup" }, cast: [], inherited: [] }],
      cast: [] },
  ],
  assetsPage: {
    items: [{
      id: "asset-1", media_json: { scene_id: "sc1", shot_id: "sh1", character_id: "ch1" },
    }],
    total: 1, offset: 0, limit: 100,
  },
  setWorkspaceTab, setAssetKindFilter, refreshAssets,
};
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

const PROVIDERS = [
  { key: "openai-images", label: "OpenAI Images", category: "image", access_class: "A", tier: "T1" },
  { key: "gemini-images", label: "Gemini Images", category: "image", access_class: "A", tier: "T1" },
  { key: "canva", label: "Canva AI", category: "image", access_class: "B", tier: "T1" },
];

const DETAILS: Record<string, unknown> = {
  "openai-images": {
    key: "openai-images", label: "OpenAI Images", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["image.generate"] }],
    models: [{
      id: "gpt-image-1", capability: "image",
      sizes: ["auto", "1024x1024", "1536x1024", "1024x1536"],
      qualities: ["auto", "low", "medium", "high"],
      cost_table: { unit: "per_image", rates: { low: 0.02, medium: 0.07, high: 0.19 }, checked: "2026-11-13" },
    }],
  },
  "gemini-images": {
    key: "gemini-images", label: "Gemini Images", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["image.generate"] }],
    models: [{ id: "imagen-4", capability: "image" }],
  },
  canva: {
    key: "canva", label: "Canva AI", access_class: "B",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["image.generate"] }],
    models: [{ id: "craft-1", capability: "image" }],
  },
};

const CONTRACTS = {
  implemented: ["openai-images"], adapter_id: "cloud.image.sim",
  simulated: true, note: "simulated transports only",
};

const SIM_SUM = {
  environment: "simulated", session_id: "s",
  scopes: [
    { scope: "session", limit_micro: null, consumed_micro: 0, open_micro: 0, charged_micro: 0 },
    { scope: "day", limit_micro: 500000, consumed_micro: 100000, open_micro: 0, charged_micro: 100000 },
    { scope: "month", limit_micro: null, consumed_micro: 0, open_micro: 0, charged_micro: 0 },
    { scope: "lifetime", limit_micro: 1000000, consumed_micro: 200000, open_micro: 0, charged_micro: 200000 },
  ],
  budgets: [],
};

const RESERVATIONS = [
  { id: "r1", job_id: "job-ok", command_id: null, project_id: "p1",
    provider_key: "openai-images", environment: "simulated", status: "settled",
    reserved_micro: 190000, remote_job_id: null, reconcile_count: 0,
    reconcile_note: null, created_at: "2026-01-01T00:00:00Z" },
  { id: "r2", job_id: "job-pending", command_id: null, project_id: "p1",
    provider_key: "openai-images", environment: "simulated", status: "open",
    reserved_micro: 190000, remote_job_id: null, reconcile_count: 0,
    reconcile_note: "held", created_at: "2026-01-01T00:01:00Z" },
];

const JOBS: Record<string, unknown> = {
  "job-ok": {
    id: "job-ok", project_id: "p1", adapter_id: "cloud.image.sim", kind: "image",
    status: "succeeded", progress: 1, message: "", error: null,
    output_asset_id: "asset-1",
    execution_scope: "remote", recovery_state: null, cancel_requested: false,
    created_at: "2026-01-01T00:02:00Z", completed_at: "2026-01-01T00:02:30Z",
    progress_json: {
      remote_intent: { provider_key: "openai-images", model_id: "gpt-image-1", lifecycle: "sync" },
      remote_outcome: "succeeded", remote_status: "succeeded",
    },
  },
  "job-run": {
    id: "job-run", project_id: "p1", adapter_id: "cloud.image.sim", kind: "image",
    status: "running", progress: 0.4, message: "remote job running (simulated transport)",
    error: null, output_asset_id: null,
    execution_scope: "remote", recovery_state: null, cancel_requested: false,
    created_at: "2026-01-01T00:03:00Z", completed_at: null,
    progress_json: {
      remote_intent: { provider_key: "openai-images", model_id: "gpt-image-1", lifecycle: "sync" },
    },
  },
  "job-pending": {
    id: "job-pending", project_id: "p1", adapter_id: "cloud.image.sim", kind: "image",
    status: "failed", progress: 0.9, message: "", output_asset_id: null,
    error: "image generated remotely (temporary URL) — download path not implemented",
    execution_scope: "remote", recovery_state: null, cancel_requested: false,
    created_at: "2026-01-01T00:01:00Z", completed_at: "2026-01-01T00:01:30Z",
    progress_json: {
      remote_intent: { provider_key: "openai-images", model_id: "gpt-image-1", lifecycle: "sync" },
      remote_outcome: "artifact_pending_retrieval", remote_status: "succeeded",
      reconcile_required: true,
      // A signed URL the backend redacts on read — the UI must never
      // render it even if a raw value ever reached the client.
      image_url: "https://secret.example/temp.png?sig=TOPSECRET",
    },
  },
  "job-amb": {
    id: "job-amb", project_id: "p1", adapter_id: "cloud.image.sim", kind: "image",
    status: "failed", progress: 0.9, message: "", error: "interrupted",
    output_asset_id: null,
    execution_scope: "remote", recovery_state: "needs_reconciliation",
    cancel_requested: false,
    created_at: "2026-01-01T00:04:00Z", completed_at: null,
    progress_json: { remote_outcome: "ambiguous", reconcile_required: true },
  },
  "job-text": {
    id: "job-text", project_id: "p1", adapter_id: "cloud.text.rest", kind: "generate",
    status: "succeeded", progress: 1, message: "", error: null, output_asset_id: null,
    execution_scope: "remote", recovery_state: null,
    created_at: "2026-01-01T00:05:00Z", completed_at: null, progress_json: {},
  },
};

function route(url: string, opts?: { method?: string }) {
  if (url === "/api/providers?category=image") return Promise.resolve(PROVIDERS);
  if (url === "/api/providers/image-contracts") return Promise.resolve(CONTRACTS);
  if (url.startsWith("/api/providers/")) {
    const d = DETAILS[url.split("/").pop() as string];
    return d ? Promise.resolve(d) : Promise.reject(new Error("404"));
  }
  return Promise.reject(new Error(`unmocked ${url} ${opts?.method ?? ""}`));
}

const PROPS = {
  pid: "p1", revision: 7, simSummary: SIM_SUM, reservations: RESERVATIONS,
  onCommitted: vi.fn(),
};

function renderPanel() {
  return render(createElement(CloudImagePanel, PROPS));
}

async function fillForm() {
  renderPanel();
  await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
  fireEvent.change(screen.getByTestId("ci-provider"), { target: { value: "openai-images" } });
  await waitFor(() => screen.getByText("gpt-image-1"));
  fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "a lighthouse at dusk" } });
}

beforeEach(() => {
  cleanup();
  vi.clearAllMocks();
  store.jobs = { ...JOBS };
  apiFetch.mockImplementation(route);
  previewCommand.mockResolvedValue({
    summary: "Simulate cloud image job for openai-images",
    warnings: ["Cost is an ESTIMATE only (~$0.1900, per_image)",
      "Runs on a SIMULATED transport"],
    affected: [], estimated_cost: { compute: "local", currency_usd: 0, requires_approval: false },
    current_revision: 7, launches_generation: true,
  });
  commitCommand.mockResolvedValue({
    command_id: "cmd1", status: "committed", revision: 8,
    idempotent_replay: false, job_id: "job-new", warnings: [],
  });
});

describe("CloudImagePanel — simulation honesty & provider selection", () => {
  it("labels the form, jobs, and results as simulated everywhere", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    const bannerCount = screen.getAllByText(
      "SIMULATION — No external AI provider contacted.").length;
    expect(bannerCount).toBeGreaterThanOrEqual(5); // form + 4 image job cards
    expect(screen.getByText(/deterministic test image, not real provider output/)).toBeTruthy();
    // result card provenance label
    expect(screen.getByText(/provenance: simulated/)).toBeTruthy();
    expect(screen.getByText(/deterministic test PNG, not provider output/)).toBeTruthy();
  });

  it("enables only implemented contracts; others are disabled with reasons", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    const sel = screen.getByTestId("ci-provider") as HTMLSelectElement;
    const opts = Array.from(sel.options);
    expect(opts.find((o) => o.value === "openai-images")!.disabled).toBe(false);
    expect(opts.find((o) => o.value === "gemini-images")!.disabled).toBe(true);
    expect(opts.find((o) => o.value === "canva")!.disabled).toBe(true);
    expect(opts.find((o) => o.value === "gemini-images")!.textContent)
      .toContain("no implemented contract");
    expect(screen.getByTestId("ci-unavailable").textContent)
      .toContain("2 catalog providers");
  });

  it("refuses to arm preview for an unimplemented provider", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    const sel = screen.getByTestId("ci-provider") as HTMLSelectElement;
    fireEvent.change(sel, { target: { value: "gemini-images" } });
    fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "x" } });
    expect((screen.getByTestId("ci-preview-btn") as HTMLButtonElement).disabled).toBe(true);
  });

  it("lists only image-capable models for the chosen provider", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    fireEvent.change(screen.getByTestId("ci-provider"), { target: { value: "openai-images" } });
    await waitFor(() => screen.getByText("gpt-image-1"));
    // preselected first image model
    expect((screen.getByTestId("ci-model") as HTMLSelectElement).value).toBe("gpt-image-1");
  });

  it("offers catalog-declared sizes and qualities — nothing invented", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    fireEvent.change(screen.getByTestId("ci-provider"), { target: { value: "openai-images" } });
    await waitFor(() => screen.getByText("gpt-image-1"));
    const sizeOpts = Array.from((screen.getByTestId("ci-size") as HTMLSelectElement).options).map((o) => o.value);
    expect(sizeOpts).toEqual(["auto", "1024x1024", "1536x1024", "1024x1536"]);
    const qOpts = Array.from((screen.getByTestId("ci-quality") as HTMLSelectElement).options).map((o) => o.value);
    expect(qOpts).toEqual(["auto", "low", "medium", "high"]);
  });

  it("never renders a sensitive remote URL present in job detail", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-pending"));
    expect(document.body.textContent).not.toContain("TOPSECRET");
    expect(document.body.textContent).not.toContain("secret.example");
    expect(document.querySelector('a[href*="secret.example"]')).toBeNull();
  });

  it("collects no credentials — no secret/key inputs anywhere", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    expect(document.querySelector('input[type="password"]')).toBeNull();
    for (const el of Array.from(document.querySelectorAll("input, textarea"))) {
      const label = el.closest("label")?.textContent ?? "";
      const aria = el.getAttribute("aria-label") ?? "";
      const ph = el.getAttribute("placeholder") ?? "";
      expect(`${label} ${aria} ${ph}`.toLowerCase()).not.toMatch(/api.?key|secret|token|credential/);
    }
  });
});

describe("CloudImagePanel — estimate, budget, settings", () => {
  it("shows the worst-case per-image estimate for provider-default quality", async () => {
    await fillForm();
    expect(screen.getByTestId("ci-cost-estimate").textContent).toContain("$0.1900");
    expect(screen.getByTestId("ci-cost-estimate").textContent).toContain("estimate only");
  });

  it("re-estimates per quality tier", async () => {
    await fillForm();
    fireEvent.change(screen.getByTestId("ci-quality"), { target: { value: "low" } });
    expect(screen.getByTestId("ci-cost-estimate").textContent).toContain("$0.0200");
    fireEvent.change(screen.getByTestId("ci-quality"), { target: { value: "high" } });
    expect(screen.getByTestId("ci-cost-estimate").textContent).toContain("$0.1900");
  });

  it("shows synthetic budget availability", async () => {
    await fillForm();
    // tightest capped scope: day → 500000-100000 = 400000µ = $0.40
    expect(screen.getByTestId("ci-budget-hint").textContent)
      .toContain("$0.4000 (synthetic) available in the day budget");
  });

  it("warns when the estimate may exceed remaining budget", async () => {
    const tight = { ...SIM_SUM, scopes: SIM_SUM.scopes.map((s) =>
      s.scope === "day" ? { ...s, limit_micro: 150000 } : s) };
    render(createElement(CloudImagePanel, { ...PROPS, simSummary: tight }));
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    fireEvent.change(screen.getByTestId("ci-provider"), { target: { value: "openai-images" } });
    await waitFor(() => screen.getByText("gpt-image-1"));
    fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "x" } });
    expect(screen.getByTestId("ci-budget-hint").textContent).toContain("May exceed the day budget");
  });

  it("populates filmmaking associations from storyboard and entities", async () => {
    await fillForm();
    const scene = screen.getByTestId("ci-scene") as HTMLSelectElement;
    expect(Array.from(scene.options).map((o) => o.textContent)).toContain("Opening");
    expect(Array.from(scene.options).map((o) => o.textContent)).toContain("Chase");
    const shot = screen.getByTestId("ci-shot") as HTMLSelectElement;
    expect(Array.from(shot.options).map((o) => o.textContent)).toContain("Opening · Wide");
    const chr = screen.getByTestId("ci-character") as HTMLSelectElement;
    expect(Array.from(chr.options).map((o) => o.textContent)).toContain("Ava");
    // selecting a scene narrows shots to it
    fireEvent.change(scene, { target: { value: "sc1" } });
    expect(Array.from(shot.options).map((o) => o.value)).toEqual(["", "sh1"]);
  });
});

describe("CloudImagePanel — preview → approve → commit", () => {
  it("runs preview → approve → commit with the full payload", async () => {
    await fillForm();
    fireEvent.change(screen.getByTestId("ci-size"), { target: { value: "1536x1024" } });
    fireEvent.change(screen.getByTestId("ci-quality"), { target: { value: "medium" } });
    fireEvent.change(screen.getByTestId("ci-scene"), { target: { value: "sc1" } });
    fireEvent.change(screen.getByTestId("ci-shot"), { target: { value: "sh1" } });
    fireEvent.change(screen.getByTestId("ci-character"), { target: { value: "ch1" } });
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    expect(previewCommand).toHaveBeenCalledWith("p1", "generate.image.cloud",
      expect.objectContaining({
        provider_key: "openai-images", model_id: "gpt-image-1",
        prompt: "a lighthouse at dusk", execution_target: "cloud",
        size: "1536x1024", quality: "medium",
        scene_id: "sc1", shot_id: "sh1", character_id: "ch1",
      }));
    const card = screen.getByTestId("ci-approval");
    expect(card.textContent).toContain("SIMULATION — No external AI provider contacted.");
    expect(card.textContent).toContain("simulated — mocked transport");
    expect(card.textContent).toContain("estimate — not a charge");
    expect(commitCommand).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("ci-approve"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "generate.image.cloud",
      expect.objectContaining({ provider_key: "openai-images" })));
    await waitFor(() => screen.getByText(/Committed generate\.image\.cloud/));
  });

  it("invalidates the approval when ANY parameter changes afterwards", async () => {
    await fillForm();
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "changed" } });
    await waitFor(() => expect(screen.queryByTestId("ci-approval")).toBeNull());
    // re-preview required — commit of the stale approval is impossible
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("also invalidates on non-prompt parameter changes", async () => {
    await fillForm();
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    fireEvent.change(screen.getByTestId("ci-quality"), { target: { value: "low" } });
    await waitFor(() => expect(screen.queryByTestId("ci-approval")).toBeNull());
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("prevents duplicate submissions while a request is in flight", async () => {
    await fillForm();
    let release: (v: unknown) => void = () => {};
    previewCommand.mockImplementationOnce(
      () => new Promise((res) => { release = res; }));
    const btn = screen.getByTestId("ci-preview-btn") as HTMLButtonElement;
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    await waitFor(() => expect(previewCommand).toHaveBeenCalledTimes(1));
    expect(btn.disabled).toBe(true);
    release({ summary: "s", warnings: [], affected: [], current_revision: 7,
      estimated_cost: null, launches_generation: true });
    await waitFor(() => screen.getByTestId("ci-approval"));
  });

  it("surfaces preview refusals (e.g. pricing_unavailable) without committing", async () => {
    await fillForm();
    previewCommand.mockRejectedValueOnce(new Error("422: pricing_unavailable"));
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByText(/Preview failed: Error: 422: pricing_unavailable/));
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("surfaces commit refusals (e.g. budget) as errors", async () => {
    await fillForm();
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    commitCommand.mockRejectedValueOnce(new Error("422: budget_exceeded"));
    fireEvent.click(screen.getByTestId("ci-approve"));
    await waitFor(() => screen.getByText(/Commit failed: Error: 422: budget_exceeded/));
  });
});

describe("CloudImagePanel — job lifecycle & asset navigation", () => {
  it("filters to cloud.image.sim jobs only", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-ok"));
    expect(screen.getByTestId("ci-job-job-run")).toBeTruthy();
    expect(screen.getByTestId("ci-job-job-pending")).toBeTruthy();
    expect(screen.getByTestId("ci-job-job-amb")).toBeTruthy();
    expect(screen.queryByTestId("ci-job-job-text")).toBeNull(); // text job excluded
  });

  it("renders generation status distinctly from financial status", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-ok"));
    expect(screen.getByTestId("ci-status-job-ok").textContent).toBe("succeeded");
    expect(screen.getByTestId("ci-status-job-run").textContent).toBe("running");
    expect(screen.getByTestId("ci-status-job-pending").textContent).toBe("failed");
    // financial row is separate and synthetic-labeled
    const fin = screen.getByTestId("ci-fin-job-ok");
    expect(fin.textContent).toContain("reservation");
    expect(fin.textContent).toContain("settled");
    expect(fin.textContent).toContain("synthetic — no real charge exists");
    // pending retrieval keeps its open hold visible
    const finP = screen.getByTestId("ci-fin-job-pending");
    expect(finP.textContent).toContain("open");
    expect(finP.textContent).toContain("hold — reconcile required");
  });

  it("maps remote outcomes — pending retrieval and ambiguous need reconcile", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-pending"));
    expect(screen.getByTestId("ci-outcome-job-pending").textContent)
      .toContain("awaiting artifact retrieval");
    expect(screen.getByTestId("ci-outcome-job-pending").textContent)
      .toContain("reconcile required");
    expect(screen.getByTestId("ci-outcome-job-amb").textContent)
      .toContain("Outcome unknown");
    expect(screen.getByTestId("ci-outcome-job-ok").textContent).toBe("Completed");
  });

  it("shows running progress", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-run"));
    expect(screen.getByTestId("ci-job-job-run").textContent)
      .toContain("remote job running (simulated transport)");
  });

  it("renders the result thumbnail and navigates to the existing asset", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-ok"));
    const img = screen.getByTestId("ci-thumb-job-ok") as HTMLImageElement;
    expect(img.src).toContain("/api/projects/p1/assets/asset-1/file");
    // "View in Assets" navigates — it never re-imports or duplicates
    fireEvent.click(screen.getByTestId("ci-view-assets-job-ok"));
    expect(setAssetKindFilter).toHaveBeenCalledWith("image");
    expect(setWorkspaceTab).toHaveBeenCalledWith("assets");
  });

  it("shows filmmaking associations from the generated asset", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-links-job-ok"));
    expect(screen.getByTestId("ci-links-job-ok").textContent)
      .toContain("scene: Opening");
    expect(screen.getByTestId("ci-links-job-ok").textContent)
      .toContain("shot: Wide");
    expect(screen.getByTestId("ci-links-job-ok").textContent)
      .toContain("character: Ava");
  });

  it("uses responsive grids that collapse on narrow windows", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    // form grids are single-column on small screens, multi-col from sm up
    const grid = document.querySelector(".grid.sm\\:grid-cols-2");
    expect(grid).toBeTruthy();
    expect(document.querySelector(".grid.sm\\:grid-cols-3")).toBeTruthy();
  });
});
