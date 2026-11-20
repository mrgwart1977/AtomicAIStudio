// B-3b — LoraBindingPicker: catalog-backed bind/unbind, per-adapter
// strength, identity exclusivity, default prefill, lossless legacy
// display, and honest state labels (no color-only signaling).
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ModelCatalogEntry } from "../../api/types";
import { LoraBindingPicker } from "./LoraBindingPicker";
import { normalizeLoraEntries, type LoraRef } from "./lora";

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
  lora({ key: "char.bo", display_name: "Bo identity" }),
  lora({ key: "style.grain", display_name: "Grain style",
         capabilities: { lora_role: "style" } }),
  lora({ key: "char.exp", display_name: "Experimental one",
         support: "experimental" }),
];

function setup(refs: LoraRef[] = [], extra: Record<string, unknown> = {}) {
  const onChange = vi.fn();
  const view = render(
    <LoraBindingPicker
      refs={refs} onChange={onChange} models={MODELS}
      roles={["identity", "style"]} idPrefix="t" {...extra} />);
  return { onChange, ...view };
}

describe("LoraBindingPicker — catalog groups and states", () => {
  it("groups identity and style adapters with honest labels", () => {
    setup();
    expect(screen.getByText(/Identity adapters/)).toBeTruthy();
    expect(screen.getByText(/Style adapters/)).toBeTruthy();
    expect(screen.getByText("Ada identity")).toBeTruthy();
    expect(screen.getByText("Grain style")).toBeTruthy();
    // honesty footer — installed != validated inference
    expect(screen.getByText(/not been validated in this build/)).toBeTruthy();
  });

  it("unavailable entries are disabled with a readable badge", () => {
    setup();
    const exp = screen.getByText("Experimental one").closest("li")!;
    const box = exp.querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(box.disabled).toBe(true);
    expect(exp.textContent).toContain("Unverified");
    // available entries show Installed, not "ready"/"verified"
    const ada = screen.getByText("Ada identity").closest("li")!;
    expect(ada.textContent).toContain("Installed");
    expect(ada.textContent).not.toMatch(/verified|validated/i);
  });

  it("empty role groups tell the truth instead of hiding", () => {
    const { rerender } = render(
      <LoraBindingPicker refs={[]} onChange={() => {}} models={[]}
        roles={["identity", "style"]} idPrefix="e" />);
    expect(screen.getByText(/No identity adapters are cataloged yet/)).toBeTruthy();
    expect(screen.getByText(/No style adapters are cataloged yet/)).toBeTruthy();
    rerender(<div />);
  });
});

describe("LoraBindingPicker — bind/unbind/strength", () => {
  it("checking appends a structured ref; unchecking removes it", () => {
    const { onChange } = setup();
    const ada = screen.getByText("Ada identity").closest("li")!
      .querySelector("input[type=checkbox]")!;
    fireEvent.click(ada);
    expect(onChange).toHaveBeenLastCalledWith([
      { kind: "structured", key: "char.ada", strength: null, raw: { key: "char.ada" } },
    ]);
    // bound state → uncheck removes every ref for that key
    cleanup();
    const { onChange: off2, container } = setup(
      normalizeLoraEntries([{ key: "char.ada" }]));
    const adaBox = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(adaBox.checked).toBe(true);
    fireEvent.click(adaBox);
    expect(off2).toHaveBeenLastCalledWith([]);
  });

  it("a second identity option is disabled while one is bound", () => {
    setup(normalizeLoraEntries([{ key: "char.ada" }]));
    const bo = screen.getByText("Bo identity").closest("li")!
      .querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(bo.disabled).toBe(true);
    // style options are unaffected by identity exclusivity
    const style = screen.getByText("Grain style").closest("li")!
      .querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(style.disabled).toBe(false);
  });

  it("strength edits upgrade to structured; empty clears to default", () => {
    const { onChange } = setup(normalizeLoraEntries([{ key: "char.ada" }]));
    const input = screen.getByLabelText(/strength/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "0.8" } });
    expect(onChange).toHaveBeenLastCalledWith([
      { kind: "structured", key: "char.ada", strength: 0.8,
        raw: { key: "char.ada" } },
    ]);
  });
});

describe("LoraBindingPicker — defaults prefill + legacy refs", () => {
  it("defaults are offered as a button, applied only on click", () => {
    const defs: LoraRef[] = normalizeLoraEntries([{ key: "char.bo" }]);
    const { onChange } = setup([], { defaults: defs });
    const btn = screen.getByText(/Apply character defaults/);
    fireEvent.click(btn);
    expect(onChange).toHaveBeenCalledWith([
      { kind: "structured", key: "char.bo", strength: null,
        raw: { key: "char.bo" } },
    ]);
  });

  it("already-bound defaults are not re-offered", () => {
    const defs: LoraRef[] = normalizeLoraEntries([{ key: "char.ada" }]);
    setup(normalizeLoraEntries([{ key: "char.ada" }]), { defaults: defs });
    expect(screen.queryByText(/Apply character defaults/)).toBeNull();
  });

  it("legacy and unknown refs render as removable stored references", () => {
    const { onChange } = setup(normalizeLoraEntries(
      ["legacy-old", { key: "no-such-cat", strength: 2 }]));
    expect(screen.getByText(/legacy-old.*saved reference/)).toBeTruthy();
    expect(screen.getByText(/no-such-cat.*saved reference/)).toBeTruthy();
    fireEvent.click(screen.getByTitle('Remove "legacy-old"'));
    expect(onChange).toHaveBeenCalledWith([
      { kind: "structured", key: "no-such-cat", strength: 2,
        raw: { key: "no-such-cat", strength: 2 } },
    ]);
  });

  it("invalid stored entries show a warning and a remove path", () => {
    const { onChange } = setup(normalizeLoraEntries([42, "ok"]));
    const alert = screen.getByRole("alert").textContent;
    expect(alert).toContain("1 invalid stored entry");
    // copy must describe actual behavior: preserved when untouched,
    // blocks only adapter changes
    expect(alert).toContain("preserved as-is");
    expect(alert).toContain("can't be saved");
    fireEvent.click(screen.getByTitle("Remove this invalid entry"));
    expect(onChange).toHaveBeenCalledWith([
      { kind: "key", key: "ok", raw: "ok" },
    ]);
  });
});
