// C5-E — Model Manager compatibility surface: five-way availability
// labels, preflight dialog content, validation checks expander, and the
// explicit benchmark opt-in (never offered for unavailable models).
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ModelManagerPage } from "./ModelManagerPage";

const apiFetch = vi.fn();
vi.mock("../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));

const MODELS = [
  {
    key: "sd15.q8", display_name: "SD 1.5 (q8)", family: "sd15", version: "1.5",
    adapter_id: "gen.image.sdcpp", compatible_adapters: [], format: "gguf",
    source: { provider: "huggingface" },
    license: { name: "CreativeML", commercial_ok: true, notes: "n" },
    files: [{ name: "w.gguf", size_bytes: 2e9, sha256: "x" }],
    download_bytes: 2e9,
    requirements: { min_vram_bytes: 4e9 }, capabilities: {},
    runtime_key: "sdcpp", support: "production", modality: "image",
    modes: ["text2image"], notes: "",
    install: { status: "installed", verified: true, byte_size: 2e9,
               install_root: "M:", rel_dir: "sd15.q8" },
    compatibility: { status: "compatible", reasons: ["fits"], warnings: [],
                     suggested: {}, estimate: { vram_bytes: 5e9 } },
  },
  {
    key: "sd15.fp16", display_name: "SD 1.5 (fp16)", family: "sd15", version: "1.5",
    adapter_id: "gen.image.sdcpp", compatible_adapters: [], format: "safetensors",
    source: { provider: "huggingface" },
    license: { name: "CreativeML", commercial_ok: true, notes: "n" },
    files: [{ name: "w.st", size_bytes: 4e9, sha256: "x" }],
    download_bytes: 4e9,
    requirements: { min_vram_bytes: 6e9 }, capabilities: {},
    runtime_key: "sdcpp", support: "production", modality: "image",
    modes: ["text2image"], notes: "",
    install: { status: "not_installed" },
    compatibility: { status: "compatible", reasons: ["fits"], warnings: [],
                     suggested: {}, estimate: { vram_bytes: 7e9 } },
  },
  {
    key: "wan.2.2.ti2v", display_name: "Wan 2.2 TI2V", family: "wan", version: "2.2",
    adapter_id: "gen.video.comfyui", compatible_adapters: [], format: "safetensors",
    source: { provider: "huggingface" },
    license: { name: "Apache-2.0", commercial_ok: true, notes: "" },
    files: [], download_bytes: 0,
    requirements: {}, capabilities: {},
    runtime_key: "comfyui", support: "planned", modality: "video",
    modes: ["text2video", "image2video"], notes: "planned",
    install: { status: "not_installed" },
    compatibility: { status: "planned", reasons: ["future milestone"],
                     warnings: [], suggested: {}, estimate: {} },
  },
];

const COMPAT_REPORT = {
  hardware_profile: { key: "mid", label: "Mid-range GPU", detail: "d",
                      gpu_count: 1, max_vram_bytes: 8e9 },
  runtimes: [],
  models: [
    {
      ...MODELS[0],
      availability: "installed_operational",
      availability_reason: null,
      runtime: { key: "sdcpp", known: true, installed_flavors: ["vulkan"],
                 flavors: [{ flavor: "vulkan", installed: true, operational: true, backend_available: true }],
                 any_operational: true, planned: false },
      estimates: { vram_bytes: 5e9, ram_bytes: 3e9, basis: "estimate" },
      resolutions: null, fps: null, durations_s: null,
      benchmark: null, benchmarkable: true, limitations: [],
    },
    {
      ...MODELS[1],
      availability: "installable",
      availability_reason: null,
      runtime: { key: "sdcpp", known: true, installed_flavors: ["vulkan"],
                 flavors: [{ flavor: "vulkan", installed: true, operational: true, backend_available: true }],
                 any_operational: true, planned: false },
      estimates: { vram_bytes: 7e9, ram_bytes: 4e9, basis: "estimate" },
      resolutions: null, fps: null, durations_s: null,
      benchmark: null, benchmarkable: false, limitations: [],
    },
    {
      ...MODELS[2],
      availability: "planned",
      availability_reason: "planned",
      runtime: { key: "comfyui", known: true, installed_flavors: [], flavors: [],
                 any_operational: false, planned: true },
      estimates: { vram_bytes: null, ram_bytes: null, basis: "estimate" },
      resolutions: null, fps: null, durations_s: null,
      benchmark: null, benchmarkable: false, limitations: [],
    },
  ],
};

const PREFLIGHT = {
  model_key: "sd15.q8", display_name: "SD 1.5 (q8)", version: "1.5",
  support: "production", download_bytes: 2e9, models_root: "M:\\models",
  disk_free_bytes: 40e9, disk_sufficient: true,
  est_vram_bytes: 5e9, est_ram_bytes: 3e9, estimate_basis: "estimate",
  runtime: { key: "sdcpp", display_name: "stable-diffusion.cpp", known: true,
             planned: false, flavors: [{ flavor: "vulkan", installed: true, operational: true }] },
  license: { name: "CreativeML", commercial_ok: true, notes: "n" },
  compatibility: { status: "compatible", reasons: [], warnings: [], suggested: {} },
  limitations: [], requires_explicit_approval: true,
  auto_download: false, installable: true,
  availability_reason: null,
};

const VALIDATION = {
  model_key: "sd15.q8", read_only: true, ran_inference: false,
  verify_hashes: false, ok: true,
  summary: { blocking: 0, warning: 0, info: 0 },
  checks: [
    { name: "catalog_metadata", ok: true, detail: "catalog entry complete" },
    { name: "runtime_binary", ok: true, detail: "runtime binary present (vulkan)" },
    { name: "hash_verification", ok: null, detail: "opt-in" },
  ],
};

const mockState: { models: unknown[] } = { models: MODELS };

vi.mock("../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({
    hardware: null, adapters: [], models: mockState.models, gpuState: null,
    jobs: {},
    refreshHardware: () => Promise.resolve(),
    refreshModels: () => Promise.resolve(),
  }),
}));

function mockApi() {
  apiFetch.mockImplementation((path: string) => {
    if (path === "/api/compatibility") return Promise.resolve(COMPAT_REPORT);
    if (path === "/api/models/dir") return Promise.resolve({ dir: "M:\\models", free_bytes: 40e9 });
    if (path.endsWith("/preflight")) return Promise.resolve(PREFLIGHT);
    if (path.endsWith("/validation")) return Promise.resolve(VALIDATION);
    if (path.endsWith("/benchmark")) return Promise.resolve({ job_id: "j1" });
    return Promise.resolve({});
  });
}

describe("ModelManagerPage — C5-E compatibility surface", () => {
  beforeEach(() => {
    cleanup(); apiFetch.mockReset(); mockApi();
    mockState.models = MODELS;
  });

  it("shows the five-way availability label, never misleading for planned", async () => {
    render(<ModelManagerPage />);
    await waitFor(() => screen.getByText("Installed & ready"));
    expect(screen.getByText("Coming in a future update")).toBeTruthy();
    expect(screen.getByText("Can be installed")).toBeTruthy();
    // planned model gets no Install or Benchmark button
    expect(screen.queryByText("Approve download & install")).toBeNull();
    expect(screen.getAllByText("Compatibility checks").length).toBe(3);
  });

  it("preflight dialog reports disk, estimates, runtime, license, approval", async () => {
    render(<ModelManagerPage />);
    await waitFor(() => screen.getByText("Can be installed"));
    fireEvent.click(screen.getByText("Install…"));
    await waitFor(() => screen.getByText("Approve download & install"));
    expect(screen.getByText(/40\.0 GB free/)).toBeTruthy();
    expect(screen.getByText(/estimates, not guarantees/)).toBeTruthy();
    expect(screen.getByText(/installed & operational/)).toBeTruthy();
    expect(screen.getAllByText(/CreativeML/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Nothing downloads until you approve/)).toBeTruthy();
  });

  it("validation expander lists read-only checks", async () => {
    render(<ModelManagerPage />);
    await waitFor(() => screen.getByText("Installed & ready"));
    fireEvent.click(screen.getAllByText("Compatibility checks")[0]);
    await waitFor(() => screen.getByText(/catalog metadata/));
    expect(screen.getByText(/runtime binary present/)).toBeTruthy();
  });

  it("benchmark button exists for installed local models only", async () => {
    render(<ModelManagerPage />);
    await waitFor(() => screen.getByText("Run benchmark…"));
    fireEvent.click(screen.getByText("Run benchmark…"));
    await waitFor(() => screen.getByText(/Runs 3 minimal generations/));
    fireEvent.click(screen.getByText("Run benchmark"));
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        "/api/models/sd15.q8/benchmark",
        expect.objectContaining({ method: "POST" })));
  });
});

// B-3a — adapters (modality "lora") get their own honest section:
// role + base-family chips, advisory state badges, install/remove via
// the existing approval flow, and a truthful empty state.
const LORA = {
  key: "char.ada", display_name: "Ada — identity adapter", family: "sd1.5",
  version: "1.0", adapter_id: "gen.image.sdcpp", compatible_adapters: [],
  format: "safetensors", source: { provider: "huggingface" },
  license: { name: "CC0", commercial_ok: true, notes: "" },
  files: [{ name: "ada.safetensors", size_bytes: 2e8, sha256: "y" }],
  download_bytes: 2e8,
  requirements: {}, capabilities: { lora_role: "identity" },
  runtime_key: "sdcpp", support: "tested", modality: "lora",
  modes: [], notes: "character identity adapter",
  install: { status: "installed", verified: true, byte_size: 2e8,
             install_root: "M:", rel_dir: "char.ada" },
  compatibility: { status: "compatible", reasons: [], warnings: [],
                   suggested: {}, estimate: {} },
};

const LORA_EXPERIMENTAL = {
  ...LORA,
  key: "style.grain", display_name: "Grain — style adapter",
  capabilities: { lora_role: "style" },
  support: "experimental",
  install: { status: "not_installed" },
};

describe("ModelManagerPage — B-3a LoRA adapters section", () => {
  beforeEach(() => {
    cleanup(); apiFetch.mockReset(); mockApi();
    mockState.models = MODELS;
  });

  it("shows the adapters section with an honest empty state + note", async () => {
    render(<ModelManagerPage />);
    await waitFor(() => screen.getByText("Adapters (LoRA)"));
    expect(screen.getByText(/No adapters are cataloged yet/)).toBeTruthy();
    expect(screen.getByText(
      /real LoRA image generation has not been validated/)).toBeTruthy();
  });

  it("lora entries render as adapters, never as generatable models", async () => {
    mockState.models = [...MODELS, LORA];
    render(<ModelManagerPage />);
    await waitFor(() => screen.getByText("Ada — identity adapter"));
    expect(screen.getByText("identity adapter")).toBeTruthy();
    expect(screen.getByText("for sd1.5 bases")).toBeTruthy();
    // adapter cards show no "Can generate" generative modes line —
    // still exactly the 3 generative entries.
    expect(screen.getAllByText(/Can generate:/)).toHaveLength(3);
    // installed adapter exposes its own Remove (one per installed card)
    expect(screen.getAllByText("Remove")).toHaveLength(2);
    expect(screen.queryByText(/No adapters are cataloged/)).toBeNull();
  });

  it("experimental-tier adapters are marked unverified, not usable", async () => {
    mockState.models = [...MODELS, LORA_EXPERIMENTAL];
    render(<ModelManagerPage />);
    await waitFor(() => screen.getByText("Grain — style adapter"));
    expect(screen.getByText("style adapter")).toBeTruthy();
    expect(screen.getByText("Unverified")).toBeTruthy();
    // a known hash + files does NOT upgrade the honest state
    expect(screen.queryByText("Installed")).toBeNull();
  });

  it("adapters install through the same explicit approval flow", async () => {
    mockState.models = [...MODELS, LORA_EXPERIMENTAL];
    render(<ModelManagerPage />);
    await waitFor(() => screen.getByText("Grain — style adapter"));
    const installs = screen.getAllByText("Install…");
    fireEvent.click(installs[installs.length - 1]);
    await waitFor(() => screen.getByText("Approve download & install"));
    expect(screen.getByText(/Nothing downloads until you approve/)).toBeTruthy();
  });
});
