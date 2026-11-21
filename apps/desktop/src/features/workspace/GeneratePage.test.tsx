// B-3d — GeneratePage LoRA request integration. Verifies the explicit
// selection flows into the generate.image payload exactly as composed,
// never auto-selects, clears on context change, and submits nothing
// on its own. commitCommand is mocked — no job is ever queued.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ModelCatalogEntry } from "../../api/types";

const apiFetch = vi.fn();
const loraPreview = vi.fn();
const commitCommand = vi.fn();

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  apiFetch: (...a: unknown[]) => apiFetch(...a),
  getBackendInfo: () => Promise.resolve(null),
  loraPreview: (...a: unknown[]) => loraPreview(...a),
}));

vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
}));

const store = {
  currentProjectId: "p1",
  currentProject() {
    return { id: this.currentProjectId, revision: 7, name: "Proj" };
  },
  models: [] as ModelCatalogEntry[],
  gpuState: null,
  storyboard: [] as unknown[],
  jobs: {},
  generateShotId: null,
  refreshModels: vi.fn(async () => {}),
  refreshStoryboard: vi.fn(async () => {}),
  entities: { character: [] as unknown[] },
  refreshEntities: vi.fn(async () => {}),
};

vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

import { GeneratePage } from "./GeneratePage";

function model(over: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry {
  return {
    key: "sd15.fp16", display_name: "SD 1.5 fp16", family: "sd1.5",
    version: "1.0", adapter_id: "gen.image.sdcpp",
    compatible_adapters: [], format: "safetensors", source: {},
    license: { name: "MIT", url: "", commercial_ok: true, notes: "" },
    files: [], download_bytes: 0, requirements: {},
    capabilities: { lora: true, negative_prompt: true,
                    samplers: ["euler_a"] },
    runtime_key: "sdcpp", support: "tested", modality: "image",
    modes: [], notes: "", install: { status: "installed" },
    compatibility: { status: "compatible", reasons: [], warnings: [],
                     suggested: {}, estimate: { vram_bytes: null, basis: "" } },
    ...over,
  };
}

function loraEntry(over: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry {
  return model({
    key: "char.ada", display_name: "Ada identity", modality: "lora",
    capabilities: { lora_role: "identity" }, ...over,
  });
}

function shot(id: string, title: string, cast: unknown[] = [],
              inherited: unknown[] = []) {
  return {
    shot: { id, scene_id: "sc1", title, position: 0 },
    cast, inherited,
  };
}

const BOARD = [
  { scene: { id: "sc1", title: "Scene 1", position: 0 },
    shots: [
      shot("s1", "Wide", [{
        id: "cr1", character_id: "c1", position: 0,
        character: { id: "c1", name: "Ada", status: "active" },
        lora_json: [{ key: "char.ada", strength: 0.7 }],
      }]),
      shot("s2", "Close", []),
    ] },
];

function fillPrompt() {
  fireEvent.change(screen.getByPlaceholderText(/cozy cabin/i),
                   { target: { value: "a red door" } });
}

function attachShot(id: string) {
  fireEvent.change(screen.getByLabelText(/attach result to a shot/i),
                   { target: { value: id } });
}

beforeEach(() => {
  apiFetch.mockReset();
  loraPreview.mockReset();
  commitCommand.mockReset().mockResolvedValue({ ok: true });
  apiFetch.mockImplementation((path: string) =>
    Promise.resolve(path.includes("/assets") ? { items: [] } : []));
  store.models = [
    model(),
    loraEntry(),
    loraEntry({ key: "style.grain", display_name: "Grain style",
                capabilities: { lora_role: "style" } }),
  ];
  store.storyboard = BOARD;
  store.currentProjectId = "p1";
  store.entities.character = [];
});

afterEach(cleanup);

describe("GeneratePage — B-3d adapter selection", () => {
  it("does not select adapters or submit anything automatically", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    // stored cast binding renders as an offer — never preselected
    expect(await screen.findByTitle("Select char.ada for this request only"))
      .toBeTruthy();
    const idSel = screen.getByLabelText("Identity LoRA adapter") as HTMLSelectElement;
    expect(idSel.value).toBe("");
    expect(commitCommand).not.toHaveBeenCalled();
    expect(loraPreview).not.toHaveBeenCalled();
  });

  it("carries the explicit identity+style selection into the generate payload", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fillPrompt();
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    fireEvent.change(screen.getByLabelText("Style LoRA adapter"),
                     { target: { value: "style.grain" } });
    fireEvent.click(screen.getByText("Generate"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "generate.image",
      expect.objectContaining({
        model_key: "sd15.fp16",
        shot_id: "s1",
        loras: [
          { key: "char.ada", strength: 1.0 },
          { key: "style.grain", strength: 1.0 },
        ],
      }));
  });

  it("omits the loras field entirely when nothing is selected", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fillPrompt();
    fireEvent.click(screen.getByText("Generate"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    const payload = commitCommand.mock.calls[0][3] as Record<string, unknown>;
    expect("loras" in payload ? payload.loras : undefined).toBeUndefined();
  });

  it("carries an edited strength into the payload", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fillPrompt();
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    fireEvent.change(screen.getByLabelText("Identity LoRA strength"),
                     { target: { value: "0.65" } });
    fireEvent.click(screen.getByText("Generate"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "generate.image",
      expect.objectContaining({
        loras: [{ key: "char.ada", strength: 0.65 }],
      }));
  });

  it("changing shots clears an explicit selection", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    attachShot("s2");
    const idSel = screen.getByLabelText("Identity LoRA adapter") as HTMLSelectElement;
    expect(idSel.value).toBe("");
  });

  it("changing models clears an explicit selection", async () => {
    store.models.push(
      model({ key: "sd15.other", display_name: "SD 1.5 alt" }));
    render(<GeneratePage />);
    attachShot("s1");
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    fireEvent.change(screen.getByLabelText("Model"),
                     { target: { value: "sd15.other" } });
    const idSel = screen.getByLabelText("Identity LoRA adapter") as HTMLSelectElement;
    expect(idSel.value).toBe("");
  });

  it("feeds the preview the same explicit selection the request carries", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    loraPreview.mockResolvedValue({
      accepted: true, errors: [], prompt_errors: [],
      requested: [], resolved: [], context: {},
      hashes: { adapter_sha256: {}, base_model_sha256: null,
                prompt_sha256: null, runtime_prompt_sha256: null,
                unavailable: [] },
      est_vram_lora_overhead_bytes: 0, notes: [],
    });
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText(/Accepted — the request carries/);
    expect(loraPreview).toHaveBeenCalledWith("p1", expect.objectContaining({
      shot_id: "s1",
      loras: [{ key: "char.ada", strength: 1.0 }],
    }));
  });

  it("a suggestion click selects without touching stored cast bindings", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fireEvent.click(
      await screen.findByTitle("Select char.ada for this request only"));
    const idSel = screen.getByLabelText("Identity LoRA adapter") as HTMLSelectElement;
    expect(idSel.value).toBe("char.ada");
    // no cast-binding command was issued — only the selection changed
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("explicit none previews as an empty selection even with cast bindings", async () => {
    render(<GeneratePage />);
    attachShot("s1");   // Ada's cast binding exists — still explicit []
    loraPreview.mockResolvedValue({
      accepted: true, errors: [], prompt_errors: [],
      requested: [], resolved: [], context: {},
      hashes: { adapter_sha256: {}, base_model_sha256: null,
                prompt_sha256: null, runtime_prompt_sha256: null,
                unavailable: [] },
      est_vram_lora_overhead_bytes: 0, notes: [],
    });
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText(/Accepted — the request carries/);
    // [] — never null/omitted — so the backend cannot derive cast loras
    expect(loraPreview).toHaveBeenCalledWith("p1", expect.objectContaining({
      loras: [],
    }));
  });

  it("an invalid strength blocks Generate before any command", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fillPrompt();
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    fireEvent.change(screen.getByLabelText("Identity LoRA strength"),
                     { target: { value: "1e999" } });   // → Infinity
    fireEvent.click(screen.getByText("Generate"));
    await screen.findAllByText(/Identity LoRA strength must be a finite/);
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("an invalid strength blocks the preview check", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    fireEvent.change(screen.getByLabelText("Identity LoRA strength"),
                     { target: { value: "0" } });
    const btn = screen.getByText(
      "Check what the backend would accept") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(loraPreview).not.toHaveBeenCalled();
    expect(screen.getByText(/LoRA selection is not valid/)).toBeTruthy();
  });

  it("a cleared strength field blocks submission as incomplete", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fillPrompt();
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    fireEvent.change(screen.getByLabelText("Identity LoRA strength"),
                     { target: { value: "" } });
    fireEvent.click(screen.getByText("Generate"));
    await screen.findAllByText(/Identity LoRA strength is incomplete/);
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("correcting an invalid strength restores normal payload construction", async () => {
    render(<GeneratePage />);
    attachShot("s1");
    fillPrompt();
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    fireEvent.change(screen.getByLabelText("Identity LoRA strength"),
                     { target: { value: "1e999" } });
    fireEvent.click(screen.getByText("Generate"));
    await screen.findAllByText(/must be a finite/);
    fireEvent.change(screen.getByLabelText("Identity LoRA strength"),
                     { target: { value: "1.5" } });
    fireEvent.click(screen.getByText("Generate"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "generate.image",
      expect.objectContaining({
        loras: [{ key: "char.ada", strength: 1.5 }],
      }));
  });

  it("changing projects clears an explicit selection", async () => {
    const { rerender } = render(<GeneratePage />);
    attachShot("s1");
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    store.currentProjectId = "p2";
    rerender(<GeneratePage />);
    const idSel = screen.getByLabelText("Identity LoRA adapter") as HTMLSelectElement;
    expect(idSel.value).toBe("");
  });

  it("offers scene-inherited character defaults without applying them", async () => {
    store.storyboard = [
      { scene: { id: "sc1", title: "Scene 1", position: 0 },
        shots: [shot("s1", "Wide", [], [{
          id: "sc9", character_id: "c2", position: 0,
          character: { id: "c2", name: "Bia", status: "active" },
          lora_json: null,
        }])] },
    ];
    store.entities.character = [{
      id: "c2", kind: "character", name: "Bia",
      data: { loras: [{ key: "char.ada", strength: 0.8 }] },
    }];
    render(<GeneratePage />);
    attachShot("s1");
    const chip = await screen.findByTitle("Select char.ada for this request only");
    expect(chip.textContent).toMatch(/Bia default/);
    const idSel = screen.getByLabelText("Identity LoRA adapter") as HTMLSelectElement;
    expect(idSel.value).toBe("");            // offered, never applied
    fireEvent.click(chip);
    expect(idSel.value).toBe("char.ada");
  });

  it("duplicate suggestion keys keep their per-source labels", async () => {
    // char.ada is BOTH bound to Ada on the shot and Bia's character
    // default — two distinct, correctly-attributed offers, not one
    // misleadingly-labeled chip.
    store.storyboard = [
      { scene: { id: "sc1", title: "Scene 1", position: 0 },
        shots: [shot("s1", "Wide", [{
          id: "cr1", character_id: "c1", position: 0,
          character: { id: "c1", name: "Ada", status: "active" },
          lora_json: [{ key: "char.ada", strength: 0.7 }],
        }], [{
          id: "sc9", character_id: "c2", position: 0,
          character: { id: "c2", name: "Bia", status: "active" },
          lora_json: null,
        }])] },
    ];
    store.entities.character = [{
      id: "c2", kind: "character", name: "Bia",
      data: { loras: [{ key: "char.ada", strength: 0.8 }] },
    }];
    render(<GeneratePage />);
    attachShot("s1");
    const chips = await screen.findAllByTitle(
      "Select char.ada for this request only");
    const labels = chips.map((c) => c.textContent ?? "");
    expect(labels.some((l) => /bound to Ada/.test(l))).toBe(true);
    expect(labels.some((l) => /Bia default/.test(l))).toBe(true);
  });
});
