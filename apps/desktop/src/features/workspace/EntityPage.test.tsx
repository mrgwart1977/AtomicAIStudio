// B-3b — EntityPage character defaults: structured picker edits
// data.loras losslessly through character.update — strings stay
// strings, dicts stay dicts, clearing persists [].
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EntityPage } from "./EntityPage";

afterEach(cleanup);

const commitCommand = vi.fn();
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
}));
vi.mock("../../api/client", () => ({
  apiFetch: vi.fn(() => Promise.resolve([])),
  listReferenceSets: vi.fn(() => Promise.resolve([])),
}));

const LORA_MODEL = {
  key: "char.ada", display_name: "Ada identity", family: "sd1.5",
  version: "1.0", adapter_id: "gen.image.sdcpp", compatible_adapters: [],
  format: "safetensors", source: {},
  license: { name: "CC0", url: "", commercial_ok: true, notes: "" },
  files: [], download_bytes: 0, requirements: {},
  capabilities: { lora_role: "identity" },
  runtime_key: "sdcpp", support: "tested", modality: "lora",
  modes: [], notes: "", install: { status: "installed" },
  compatibility: { status: "compatible", reasons: [], warnings: [],
                   suggested: {}, estimate: {} },
};

const CHAR = {
  id: "c1", name: "Ada", status: "active", summary: "", revision: 2,
  world_id: null,
  data: { loras: ["legacy-ref", { key: "char.ada", strength: 0.9 }] },
};

const state = {
  currentProject: () => ({ id: "p1", revision: 3 }),
  entities: { character: [CHAR], world: [], location: [] },
  entityRevisions: {},
  entityAssets: [],
  assetsPage: null,
  models: [LORA_MODEL],
  refreshEntities: vi.fn(async () => {}),
  refreshRevisions: vi.fn(async () => {}),
  refreshProjectData: vi.fn(async () => {}),
  refreshEntityAssets: vi.fn(async () => {}),
};

vi.mock("../../stores/appStore", () => ({
  useApp: Object.assign(
    (sel: (s: unknown) => unknown) => sel(state),
    { getState: () => state },
  ),
}));

async function openEditor() {
  render(<EntityPage etype="character" title="Characters" />);
  fireEvent.click(await screen.findByText("Ada"));
  await screen.findByText(/Default identity adapters/);
}

describe("EntityPage — character default loras", () => {
  it("stored defaults render: catalog pick bound + legacy ref visible", async () => {
    await openEditor();
    const box = screen.getByText("Ada identity").closest("li")!
      .querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(box.checked).toBe(true);
    expect(screen.getByText(/legacy-ref.*saved reference/)).toBeTruthy();
    // honesty footer present on the picker
    expect(screen.getByText(/not been validated in this build/)).toBeTruthy();
  });

  it("deliberate default edit emits data.loras losslessly through character.update", async () => {
    commitCommand.mockResolvedValue({});
    await openEditor();
    // intentional change: remove the legacy string ref, keep the
    // structured catalog binding — emit must stay lossless
    fireEvent.click(screen.getByTitle('Remove "legacy-ref"'));
    fireEvent.click(screen.getByText("Save new revision"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    const [, , type, payload] = commitCommand.mock.calls.at(-1)! as [
      string, number, string, { data: { loras: unknown[] }, character_id: string },
    ];
    expect(type).toBe("character.update");
    expect(payload.character_id).toBe("c1");
    // structured entry keeps strength — no conversion
    expect(payload.data.loras).toEqual([
      { key: "char.ada", strength: 0.9 },
    ]);
  });

  it("a save with no lora edits omits data.loras — stored value preserved", async () => {
    commitCommand.mockClear();
    commitCommand.mockResolvedValue({});
    await openEditor();
    fireEvent.click(screen.getByText("Save new revision"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    const payload = commitCommand.mock.calls.at(-1)![3] as
      { data: Record<string, unknown> };
    // untouched refs → key absent → backend merge keeps stored bytes
    expect("loras" in payload.data).toBe(false);
  });

  it("unbinding via the checkbox persists on save; clearing emits []", async () => {
    commitCommand.mockResolvedValue({});
    await openEditor();
    // uncheck the bound adapter — only the legacy ref remains
    fireEvent.click(screen.getByText("Ada identity").closest("li")!
      .querySelector("input[type=checkbox]")!);
    // remove the legacy ref chip too → data.loras: [] (explicit clear)
    fireEvent.click(screen.getByTitle('Remove "legacy-ref"'));
    fireEvent.click(screen.getByText("Save new revision"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    const payload = commitCommand.mock.calls.at(-1)![3] as
      { data: { loras: unknown[] } };
    expect(payload.data.loras).toEqual([]);
  });

  it("an unrelated profile edit omits data.loras — malformed entries preserved", async () => {
    commitCommand.mockResolvedValue({});
    const bad = {
      ...CHAR, id: "c2", name: "Eve",
      data: { loras: [42, { nokey: 1 }, "kept-ref"] },
    };
    const saved = state.entities.character;
    state.entities = { ...state.entities, character: [...saved, bad] };
    try {
      render(<EntityPage etype="character" title="Characters" />);
      fireEvent.click(await screen.findByText("Eve"));
      await screen.findByText(/Default identity adapters/);
      // invalid refs are shown honestly
      expect(screen.getByRole("alert").textContent)
        .toContain("2 invalid stored entries");
      // change only the summary — an unrelated edit
      fireEvent.change(screen.getByLabelText("Summary"),
        { target: { value: "edited" } });
      fireEvent.click(screen.getByText("Save new revision"));
      await waitFor(() => expect(commitCommand).toHaveBeenCalled());
      const payload = commitCommand.mock.calls.at(-1)![3] as
        { data: Record<string, unknown> };
      // loras key absent → merge preserves stored bytes verbatim
      expect("loras" in payload.data).toBe(false);
    } finally {
      state.entities = { ...state.entities, character: saved };
    }
  });

  it("editing defaults with invalid entries remaining blocks the save", async () => {
    commitCommand.mockResolvedValue({});
    const bad = {
      ...CHAR, id: "c3", name: "Gia",
      data: { loras: [99, "char.ada"] },
    };
    const saved = state.entities.character;
    state.entities = { ...state.entities, character: [...saved, bad] };
    try {
      commitCommand.mockClear();
      render(<EntityPage etype="character" title="Characters" />);
      fireEvent.click(await screen.findByText("Gia"));
      await screen.findByText(/Default identity adapters/);
      // user touches adapters — char.ada is a bound string ref; uncheck it
      fireEvent.click(screen.getByText("Ada identity").closest("li")!
        .querySelector("input[type=checkbox]")!);
      fireEvent.click(screen.getByText("Save new revision"));
      // the save-level block names the surface — distinct from the
      // picker's standing warning
      await waitFor(() => screen.getByText(/in default LoRAs/));
      expect(screen.getByText(/in default LoRAs/).textContent)
        .toContain("1 invalid stored adapter entry");
      expect(commitCommand).not.toHaveBeenCalled();
    } finally {
      state.entities = { ...state.entities, character: saved };
    }
  });

  it("a backend refusal surfaces as a readable error, not a silent drop", async () => {
    commitCommand.mockRejectedValue(new Error(
      "422 — loras[0]: unknown LoRA 'x' — pick an installed adapter"));
    await openEditor();
    fireEvent.click(screen.getByText("Save new revision"));
    await waitFor(() => screen.getByRole("alert"));
    expect(screen.getByRole("alert").textContent).toContain("unknown LoRA 'x'");
  });
});
