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

const fetchAssetObjectUrl = vi.fn();
const listReferenceSets = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
  fetchAssetObjectUrl: (...a: unknown[]) => fetchAssetObjectUrl(...a),
  listReferenceSets: (...a: unknown[]) => listReferenceSets(...a),
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
      id: "asset-1", name: "keyframe.png", kind: "image",
      media_json: { scene_id: "sc1", shot_id: "sh1", character_id: "ch1" },
    }, {
      id: "asset-ref", name: "style-ref.png", kind: "image", media_json: {},
    }],
    total: 2, offset: 0, limit: 100,
  },
  setWorkspaceTab, setAssetKindFilter, refreshAssets,
};
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

const PROVIDERS = [
  { key: "openai-images", label: "OpenAI Images", category: "image", access_class: "A", tier: "T1" },
  { key: "bfl-flux", label: "FLUX / Black Forest Labs", category: "image", access_class: "A", tier: "T1" },
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
      editing: { img2img: true, max_reference_images: 16,
        mask_inpaint: true, outpaint: false, reference_weights: false,
        identity_reference: false },
      cost_table: { unit: "per_image", rates: { low: 0.02, medium: 0.07, high: 0.19 }, checked: "2026-11-13" },
    }],
  },
  "bfl-flux": {
    key: "bfl-flux", label: "FLUX / Black Forest Labs", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["image.generate", "image.edit"] }],
    models: [
      { id: "flux-pro-1.0-fill", capability: "image.edit",
        editing: { img2img: false, mask_inpaint: true, outpaint: false,
          max_reference_images: 0 },
        cost_table: { unit: "per_image", rate: 0.05, checked: "2026-11-13" } },
      { id: "flux-pro-1.0-expand", capability: "image.edit",
        editing: { img2img: false, mask_inpaint: false, outpaint: true,
          max_reference_images: 0 },
        cost_table: { unit: "per_image", rate: 0.05, checked: "2026-11-13" } },
    ],
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
  img2img_implemented: ["openai-images"],
  img2img_adapter_id: "cloud.image.img2img.sim",
  inpaint_implemented: ["openai-images", "bfl-flux"],
  outpaint_implemented: ["bfl-flux"],
  edit_adapter_id: "cloud.image.edit.sim",
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
  { id: "r3", job_id: "job-open", command_id: null, project_id: "p1",
    provider_key: "openai-images", environment: "simulated", status: "open",
    reserved_micro: 70000, remote_job_id: null, reconcile_count: 0,
    reconcile_note: "usage_unavailable", created_at: "2026-01-01T00:02:30Z" },
];

const JOBS: Record<string, unknown> = {
  "job-ok": {
    id: "job-ok", project_id: "p1", adapter_id: "cloud.image.sim", kind: "image",
    status: "succeeded", progress: 1, message: "", error: null,
    output_asset_id: "asset-1",
    execution_scope: "remote", recovery_state: null, cancel_requested: false,
    created_at: "2026-01-01T00:02:00Z", completed_at: "2026-01-01T00:02:30Z",
    // Real backend shape: remote_status/remote_intent only —
    // remote_outcome is absent on the plain success path.
    progress_json: {
      remote_intent: { provider_key: "openai-images", model_id: "gpt-image-1", lifecycle: "sync" },
      remote_status: "succeeded", remote_job_id: "req_1",
      transport_simulated: true,
    },
  },
  "job-open": {
    // Artifact succeeded but billing stayed unresolved — the job is
    // 'succeeded' AND the hold stays open for reconcile. Success must
    // never be read as settlement.
    id: "job-open", project_id: "p1", adapter_id: "cloud.image.sim", kind: "image",
    status: "succeeded", progress: 1, message: "", error: null,
    output_asset_id: "asset-2",
    execution_scope: "remote", recovery_state: null, cancel_requested: false,
    created_at: "2026-01-01T00:02:30Z", completed_at: "2026-01-01T00:02:45Z",
    progress_json: {
      remote_intent: { provider_key: "openai-images", model_id: "gpt-image-1", lifecycle: "sync" },
      remote_status: "succeeded", transport_simulated: true,
      spend_action: "hold_usage_unavailable",
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
  fetchAssetObjectUrl.mockResolvedValue("blob:mock-thumb");
  listReferenceSets.mockResolvedValue([]);
  previewCommand.mockResolvedValue({
    summary: "Simulate cloud image job for openai-images",
    warnings: ["Cost is an ESTIMATE only (~$0.1900, per_image)",
      "Runs on a SIMULATED transport"],
    affected: [], estimated_cost: { compute: "local", currency_usd: 0, requires_approval: false },
    // The backend-authoritative estimate — what the approval MUST show.
    cost_estimate: { est_cost_usd: 0.19, unit: "per_image", currency: "USD",
      is_estimate: true, simulated: true, settlement: "usage-based" },
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
    // result card provenance labels (every succeeded job carries one)
    expect(screen.getAllByText(/provenance: simulated/).length)
      .toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/deterministic test PNG, not provider output/)
      .length).toBeGreaterThanOrEqual(2);
  });

  it("enables only implemented contracts; others are disabled with reasons", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    const sel = screen.getByTestId("ci-provider") as HTMLSelectElement;
    const opts = Array.from(sel.options);
    expect(opts.find((o) => o.value === "openai-images")!.disabled).toBe(false);
    expect(opts.find((o) => o.value === "gemini-images")!.disabled).toBe(true);
    expect(opts.find((o) => o.value === "canva")!.disabled).toBe(true);
    expect(opts.find((o) => o.value === "bfl-flux")!.disabled).toBe(true);
    expect(opts.find((o) => o.value === "gemini-images")!.textContent)
      .toContain("no implemented contract");
    expect(screen.getByTestId("ci-unavailable").textContent)
      .toContain("3 catalog providers");
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
    expect(screen.getByTestId("ci-cost-estimate").textContent).toContain("preliminary estimate");
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
    expect(card.textContent).toContain("estimate, not a charge");
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
    // every cost-affecting field is fingerprinted — each drops the approval
    for (const change of [
      () => fireEvent.change(screen.getByTestId("ci-quality"), { target: { value: "low" } }),
      () => fireEvent.change(screen.getByTestId("ci-size"), { target: { value: "1024x1024" } }),
      () => fireEvent.change(screen.getByLabelText(/^Seed \(optional/), { target: { value: "42" } }),
      () => fireEvent.change(screen.getByLabelText(/Max cost cap/), { target: { value: "0.5" } }),
    ]) {
      fireEvent.click(screen.getByTestId("ci-preview-btn"));
      await waitFor(() => screen.getByTestId("ci-approval"));
      change();
      await waitFor(() => expect(screen.queryByTestId("ci-approval")).toBeNull());
    }
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("approval shows the BACKEND authoritative estimate — not the hint", async () => {
    await fillForm();
    previewCommand.mockResolvedValueOnce({
      summary: "s", warnings: ["Cost is an ESTIMATE only (~$0.0777)"],
      affected: [], estimated_cost: { compute: "local", currency_usd: 0, requires_approval: false },
      cost_estimate: { est_cost_usd: 0.0777, unit: "per_image", currency: "USD",
        is_estimate: true, simulated: true, settlement: "usage-based" },
      current_revision: 7, launches_generation: true,
    });
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    const el = await screen.findByTestId("ci-approval-cost");
    // $0.0777 is the backend figure — the mirror's worst-case $0.19 must NOT appear
    expect(el.textContent).toContain("$0.0777");
    expect(el.textContent).toContain("authoritative preview");
    expect(el.textContent).toContain("estimate, not a charge");
    expect(el.textContent).not.toContain("$0.1900");
  });

  it("cannot approve a preview the backend could not price", async () => {
    await fillForm();
    previewCommand.mockResolvedValueOnce({
      summary: "s", warnings: ["pricing unavailable"],
      affected: [], estimated_cost: { compute: "local", currency_usd: 0, requires_approval: false },
      cost_estimate: { est_cost_usd: null, unit: "per_credit", currency: "USD",
        is_estimate: true, simulated: true, settlement: "usage-based" },
      current_revision: 7, launches_generation: true,
    });
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    expect((screen.getByTestId("ci-approve") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("ci-approval").textContent)
      .toContain("cannot be priced");
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
    // plain success emits no remote_outcome — nothing extra is claimed
    expect(screen.queryByTestId("ci-outcome-job-ok")).toBeNull();
  });

  it("job success is never presented as financial settlement", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-open"));
    expect(screen.getByTestId("ci-status-job-open").textContent).toBe("succeeded");
    // …yet its reservation stayed OPEN — the financial row says so
    const fin = screen.getByTestId("ci-fin-job-open");
    expect(fin.textContent).toContain("open");
    expect(fin.textContent).toContain("hold — reconcile required");
    expect(fin.textContent).toContain("synthetic — no real charge exists");
  });

  it("shows running progress", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-run"));
    expect(screen.getByTestId("ci-job-job-run").textContent)
      .toContain("remote job running (simulated transport)");
  });

  it("renders the result thumbnail via header auth — no token in any URL", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-job-job-ok"));
    const img = await screen.findByTestId("ci-thumb-job-ok") as HTMLImageElement;
    // authenticated fetch → object URL: the DOM never holds a token URL
    expect(img.src).toBe("blob:mock-thumb");
    expect(fetchAssetObjectUrl).toHaveBeenCalledWith("p1", "asset-1");
    expect(document.body.innerHTML).not.toContain("token=");
    expect(document.querySelector('a[href*="token="]')).toBeNull();
    // "View in Assets" navigates — it never re-imports or duplicates
    fireEvent.click(screen.getByTestId("ci-view-assets-job-ok"));
    expect(setAssetKindFilter).toHaveBeenCalledWith("image");
    expect(setWorkspaceTab).toHaveBeenCalledWith("assets");
  });

  it("fails safe when the thumbnail cannot be loaded", async () => {
    fetchAssetObjectUrl.mockRejectedValue(new Error("404"));
    renderPanel();
    const fallback = await screen.findByTestId("ci-thumb-fallback-job-ok");
    expect(fallback.textContent).toContain("preview unavailable");
    expect(screen.queryByTestId("ci-thumb-job-ok")).toBeNull();
    // the View in Assets path still works — the asset exists even if
    // its preview bytes could not be fetched
    expect(screen.getByTestId("ci-view-assets-job-ok")).toBeTruthy();
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

describe("CloudImagePanel — C3-D2 image-to-image mode", () => {
  it("shows img2img inputs only in edit mode and requires a source", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    expect(screen.queryByTestId("ci-source")).toBeNull();
    fireEvent.click(screen.getByTestId("ci-mode-img2img"));
    await waitFor(() => screen.getByTestId("ci-img2img-inputs"));
    expect(screen.getByTestId("ci-source")).toBeTruthy();
    expect(screen.getByText(/never changed|never modified/i)).toBeTruthy();
    // Preview stays disabled until a source image is picked.
    fireEvent.change(screen.getByTestId("ci-provider"), { target: { value: "openai-images" } });
    await waitFor(() => screen.getByText("gpt-image-1"));
    fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "stormy night" } });
    expect((screen.getByTestId("ci-preview-btn") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByTestId("ci-source"), { target: { value: "asset-1" } });
    expect((screen.getByTestId("ci-preview-btn") as HTMLButtonElement).disabled).toBe(false);
  });

  it("lists references, excludes the source, and sends them in the payload", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    fireEvent.click(screen.getByTestId("ci-mode-img2img"));
    fireEvent.change(screen.getByTestId("ci-provider"), { target: { value: "openai-images" } });
    await waitFor(() => screen.getByText("gpt-image-1"));
    fireEvent.change(screen.getByTestId("ci-source"), { target: { value: "asset-1" } });
    fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "restyle" } });
    // The source asset must not appear as a reference option.
    expect(screen.queryByTestId("ci-ref-asset-1")).toBeNull();
    fireEvent.click(screen.getByTestId("ci-ref-asset-ref"));
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    const payload = previewCommand.mock.calls[0][2] as Record<string, unknown>;
    expect(previewCommand.mock.calls[0][1]).toBe("generate.image.img2img");
    expect(payload.source_asset_id).toBe("asset-1");
    expect(payload.reference_asset_ids).toEqual(["asset-ref"]);
    expect(screen.getByTestId("ci-approval-source").textContent)
      .toContain("1 reference");
  });

  it("C4-A — sends a character reference set and pins resolved contents", async () => {
    listReferenceSets.mockResolvedValue([{
      id: "rs1", project_id: "p1", character_id: "ch1", name: "Hero refs",
      seed: 42, notes: "", position: 0, status: "active", revision: 1,
      member_count: 2, created_at: "2026-01-01T00:00:00Z",
    }]);
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    fireEvent.click(screen.getByTestId("ci-mode-img2img"));
    fireEvent.change(screen.getByTestId("ci-provider"), { target: { value: "openai-images" } });
    await waitFor(() => screen.getByText("gpt-image-1"));
    fireEvent.change(screen.getByTestId("ci-source"), { target: { value: "asset-1" } });
    fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "new backdrop" } });
    // pick the character → its sets load into the set picker
    fireEvent.change(screen.getByTestId("ci-character"), { target: { value: "ch1" } });
    await waitFor(() => expect(listReferenceSets).toHaveBeenCalledWith("p1", "ch1"));
    const setSel = await screen.findByTestId("ci-refset") as HTMLSelectElement;
    expect(setSel.textContent).toContain("Hero refs (2 refs)");
    fireEvent.change(setSel, { target: { value: "rs1" } });
    // preview returns the resolved contents — they are pinned into commit
    previewCommand.mockResolvedValueOnce({
      summary: "s", warnings: [],
      affected: [], estimated_cost: { compute: "local", currency_usd: 0, requires_approval: false },
      cost_estimate: { est_cost_usd: 0.02, unit: "per_image", currency: "USD",
        is_estimate: true, simulated: true, settlement: "usage-based" },
      current_revision: 7, launches_generation: true,
      resolved_references: [
        { asset_id: "asset-ref", weight: null, sha256: "a".repeat(64) },
        { asset_id: "asset-9", weight: null, sha256: "b".repeat(64) },
      ],
      resolved_set: { id: "rs1", seed: 42, revision: 1 },
    });
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    const prev = previewCommand.mock.calls[0][2] as Record<string, unknown>;
    expect(prev.reference_set_id).toBe("rs1");
    expect(prev.reference_asset_ids).toBeUndefined();
    expect(prev.reference_weights).toBeUndefined();
    fireEvent.click(screen.getByTestId("ci-approve"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    const committed = commitCommand.mock.calls[0][3] as Record<string, unknown>;
    expect(committed.reference_set_id).toBe("rs1");
    expect(committed.expected_references).toEqual([
      { asset_id: "asset-ref", weight: null, sha256: "a".repeat(64) },
      { asset_id: "asset-9", weight: null, sha256: "b".repeat(64) },
    ]);
    expect(committed.expected_set).toEqual({ id: "rs1", seed: 42, revision: 1 });
    // identity honesty copy is shown once a set is selected
    expect(document.body.textContent).toContain("do not guarantee identity");
  });

  it("keeps non-img2img providers disabled in edit mode", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    fireEvent.click(screen.getByTestId("ci-mode-img2img"));
    const sel = screen.getByTestId("ci-provider") as HTMLSelectElement;
    const opts = Array.from(sel.options);
    expect(opts.find((o) => o.value === "openai-images")!.disabled).toBe(false);
    expect(opts.find((o) => o.value === "gemini-images")!.disabled).toBe(true);
  });

  it("resets edit-only fields when switching back to generate mode", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    fireEvent.click(screen.getByTestId("ci-mode-img2img"));
    fireEvent.change(screen.getByTestId("ci-source"), { target: { value: "asset-1" } });
    fireEvent.click(screen.getByTestId("ci-mode-generate"));
    expect(screen.queryByTestId("ci-img2img-inputs")).toBeNull();
    fireEvent.click(screen.getByTestId("ci-mode-img2img"));
    await waitFor(() => screen.getByTestId("ci-img2img-inputs"));
    expect((screen.getByTestId("ci-source") as HTMLSelectElement).value).toBe("");
  });
  it("inpaint sends mask + operation on generate.image.edit", async () => {
    renderPanel();
    await waitFor(() => screen.getByText("OpenAI Images (simulated API)"));
    fireEvent.click(screen.getByTestId("ci-mode-inpaint"));
    await waitFor(() => screen.getByTestId("ci-inpaint-inputs"));
    fireEvent.change(screen.getByTestId("ci-provider"), { target: { value: "openai-images" } });
    await waitFor(() => screen.getByText("gpt-image-1"));
    fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "fill left half" } });
    fireEvent.change(screen.getByTestId("ci-source"), { target: { value: "asset-1" } });
    // Mask required before preview can arm.
    expect((screen.getByTestId("ci-preview-btn") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByTestId("ci-mask"), { target: { value: "asset-ref" } });
    expect((screen.getByTestId("ci-preview-btn") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    expect(previewCommand.mock.calls[0][1]).toBe("generate.image.edit");
    const payload = previewCommand.mock.calls[0][2] as Record<string, unknown>;
    expect(payload.operation).toBe("inpaint");
    expect(payload.source_asset_id).toBe("asset-1");
    expect(payload.mask_asset_id).toBe("asset-ref");
  });

  it("outpaint sends explicit canvas + placement; only implemented providers select", async () => {
    renderPanel();
    await waitFor(() => screen.getByTestId("ci-unavailable"));
    fireEvent.click(screen.getByTestId("ci-mode-outpaint"));
    await waitFor(() => screen.getByTestId("ci-outpaint-inputs"));
    const sel = screen.getByTestId("ci-provider") as HTMLSelectElement;
    expect(Array.from(sel.options).find((o) => o.value === "bfl-flux")!.disabled).toBe(false);
    expect(Array.from(sel.options).find((o) => o.value === "openai-images")!.disabled).toBe(true);
    fireEvent.change(sel, { target: { value: "bfl-flux" } });
    await waitFor(() => screen.getByText("flux-pro-1.0-expand"));
    fireEvent.change(screen.getByTestId("ci-source"), { target: { value: "asset-1" } });
    fireEvent.change(screen.getByTestId("ci-prompt"), { target: { value: "wider sky" } });
    expect((screen.getByTestId("ci-preview-btn") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByTestId("ci-outw"), { target: { value: "128" } });
    fireEvent.change(screen.getByTestId("ci-outh"), { target: { value: "96" } });
    expect((screen.getByTestId("ci-preview-btn") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId("ci-preview-btn"));
    await waitFor(() => screen.getByTestId("ci-approval"));
    const payload = previewCommand.mock.calls[0][2] as Record<string, unknown>;
    expect(payload.operation).toBe("outpaint");
    expect(payload.out_width).toBe(128);
    expect(payload.placement).toBe("center");
    expect(screen.getByTestId("ci-approval-source").textContent).toContain("128");
  });
});