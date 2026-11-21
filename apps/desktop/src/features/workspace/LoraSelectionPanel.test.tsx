// B-3d — explicit per-request LoRA selection panel. Selection is
// explicit-only: suggestions render as click offers, never auto-apply;
// state badges show role/compat/install facts; the multi-character
// honesty line is always visible.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ModelCatalogEntry } from "../../api/types";
import { LoraSelectionPanel, type LoraSuggestion } from "./LoraSelectionPanel";
import { EMPTY_LORA_SELECTION, type LoraSelection } from "./lora";

afterEach(cleanup);

function lora(over: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry {
  return {
    key: "char.ada", display_name: "Ada identity", family: "sd1.5",
    version: "1.0", adapter_id: "gen.image.sdcpp",
    compatible_adapters: [], format: "safetensors", source: {},
    license: { name: "CC0", url: "", commercial_ok: true, notes: "" },
    files: [], download_bytes: 0, requirements: {},
    capabilities: { lora_role: "identity" },
    runtime_key: "sdcpp", support: "tested", modality: "lora",
    modes: [], notes: "", install: { status: "installed" },
    compatibility: { status: "compatible", reasons: [], warnings: [],
                     suggested: {}, estimate: { vram_bytes: null, basis: "" } },
    ...over,
  };
}

const MODELS: ModelCatalogEntry[] = [
  lora(),
  lora({ key: "style.grain", display_name: "Grain style",
         capabilities: { lora_role: "style" } }),
  lora({ key: "char.missing", display_name: "Not installed id",
         install: null }),
  lora({ key: "style.sdxl", display_name: "SDXL style",
         family: "sdxl", capabilities: { lora_role: "style" } }),
];

const SUGGESTIONS: LoraSuggestion[] = [
  { key: "char.ada", label: "bound to Ada", strength: 0.7 },
  { key: "style.grain", label: "bound to Ada", strength: null },
];

function setup(
  value: LoraSelection = EMPTY_LORA_SELECTION,
  extra: Partial<Parameters<typeof LoraSelectionPanel>[0]> = {},
) {
  const onChange = vi.fn();
  const view = render(
    <LoraSelectionPanel
      models={MODELS}
      baseFamily="sd1.5"
      hasShot={true}
      suggestions={SUGGESTIONS}
      value={value}
      onChange={onChange}
      {...extra}
    />,
  );
  return { onChange, ...view };
}

describe("LoraSelectionPanel", () => {
  it("renders identity + style pickers with honest labels", () => {
    setup();
    expect(screen.getByText("LoRA adapters — this generation only"))
      .toBeTruthy();
    expect(screen.getByLabelText("Identity LoRA adapter")).toBeTruthy();
    expect(screen.getByLabelText("Style LoRA adapter")).toBeTruthy();
    // per-request honesty + runtime boundary text
    expect(screen.getByText(/saved cast.*bindings are unchanged/i))
      .toBeTruthy();
    expect(screen.getByText(/not been validated in this build/))
      .toBeTruthy();
    // multi-character honesty — never implies spatial control
    expect(screen.getByText(/One identity LoRA per generation/))
      .toBeTruthy();
    expect(screen.getByText(/per-character spatial blending is not supported/))
      .toBeTruthy();
  });

  it("selecting an identity adapter emits {key, strength:1.0}", () => {
    const { onChange } = setup();
    fireEvent.change(screen.getByLabelText("Identity LoRA adapter"),
                     { target: { value: "char.ada" } });
    expect(onChange).toHaveBeenCalledWith(
      { identity: { key: "char.ada", strength: 1.0 }, style: null });
  });

  it("selecting a style adapter preserves the identity choice", () => {
    const { onChange } = setup({ identity: { key: "char.ada", strength: 1.0 },
                                style: null });
    fireEvent.change(screen.getByLabelText("Style LoRA adapter"),
                     { target: { value: "style.grain" } });
    expect(onChange).toHaveBeenCalledWith(
      { identity: { key: "char.ada", strength: 1.0 },
        style: { key: "style.grain", strength: 1.0 } });
  });

  it("selecting None removes the selection explicitly", () => {
    const { onChange } = setup({ identity: null,
                                style: { key: "style.grain", strength: 0.5 } });
    fireEvent.change(screen.getByLabelText("Style LoRA adapter"),
                     { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(
      { identity: null, style: null });
  });

  it("the ✕ button removes the selection explicitly", () => {
    const { onChange } = setup({ identity: { key: "char.ada", strength: 1.0 },
                                style: null });
    fireEvent.click(screen.getByLabelText("Remove Identity LoRA adapter"));
    expect(onChange).toHaveBeenCalledWith({ identity: null, style: null });
  });

  it("strength edits emit the new strength", () => {
    const { onChange } = setup({ identity: { key: "char.ada", strength: 1.0 },
                                style: null });
    fireEvent.change(screen.getByLabelText("Identity LoRA strength"),
                     { target: { value: "0.65" } });
    expect(onChange).toHaveBeenCalledWith(
      { identity: { key: "char.ada", strength: 0.65 }, style: null });
  });

  it("suggestions are offered but never auto-applied", () => {
    setup();   // value = EMPTY — nothing preselected
    const idSel = screen.getByLabelText("Identity LoRA adapter") as HTMLSelectElement;
    const stySel = screen.getByLabelText("Style LoRA adapter") as HTMLSelectElement;
    expect(idSel.value).toBe("");
    expect(stySel.value).toBe("");
    // offers render as explicit click chips, labeled with their source
    expect(screen.getAllByText(/bound to Ada/).length).toBeGreaterThan(0);
    expect(screen.getByTitle("Select char.ada for this request only"))
      .toBeTruthy();
  });

  it("clicking a suggestion applies it with the stored strength", () => {
    const { onChange } = setup();
    fireEvent.click(screen.getByTitle("Select char.ada for this request only"));
    expect(onChange).toHaveBeenCalledWith(
      { identity: { key: "char.ada", strength: 0.7 }, style: null });
  });

  it("a bare-string suggestion defaults strength to 1.0", () => {
    const { onChange } = setup();
    fireEvent.click(
      screen.getByTitle("Select style.grain for this request only"));
    expect(onChange).toHaveBeenCalledWith(
      { identity: null, style: { key: "style.grain", strength: 1.0 } });
  });

  it("shows install/compat state on the selected adapter", () => {
    setup({ identity: { key: "char.ada", strength: 1.0 }, style: null });
    expect(screen.getByText("Installed")).toBeTruthy();
    expect(screen.getByText(/sd1\.5/)).toBeTruthy();
    expect(screen.getByText(/safetensors/)).toBeTruthy();
    expect(screen.getByText(/CC0/)).toBeTruthy();
  });

  it("marks a family-mismatched selection with the refusal badge", () => {
    setup({ identity: null,
            style: { key: "style.sdxl", strength: 1.0 } });
    expect(screen.getByText("Different base family")).toBeTruthy();
  });

  it("marks a non-installed selection as Not installed", () => {
    setup({ identity: { key: "char.missing", strength: 1.0 }, style: null });
    expect(screen.getByText("Not installed")).toBeTruthy();
  });

  it("warns when the base model is not SD 1.5", () => {
    setup(EMPTY_LORA_SELECTION, { baseFamily: "sdxl" });
    expect(screen.getByText(/SD 1\.5 image models only/)).toBeTruthy();
  });

  it("tells the user identity adapters need a shot when none is attached", () => {
    setup(EMPTY_LORA_SELECTION, { hasShot: false });
    expect(screen.getByText(/Attach a shot/)).toBeTruthy();
  });

  it("renders an honest empty state with no catalog loras", () => {
    setup(EMPTY_LORA_SELECTION, { models: [] });
    expect(screen.getByText(/No LoRA adapters in the catalog/)).toBeTruthy();
  });
});
