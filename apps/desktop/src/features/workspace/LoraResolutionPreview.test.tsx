// B-3c — backend-authoritative LoRA resolution preview panel.
// Read-only inspection: refused and accepted resolutions both render
// as data; results go stale on context change; nothing is submitted.
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const loraPreview = vi.fn();

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  loraPreview: (...a: unknown[]) => loraPreview(...a),
}));

import { LoraResolutionPreview } from "./LoraResolutionPreview";

const ACCEPTED = {
  accepted: true,
  errors: [],
  prompt_errors: [],
  requested: [{ shape: "string", key: "char.ada", strength: null }],
  resolved: [{
    key: "char.ada", version: "1.0", role: "identity", strength: 1,
    sha256: "a".repeat(64), staged_name: "char.ada-aaaaaaaaaaaa.safetensors",
    base_family: "sd1.5", base_model_key: "sd15.fp16",
    base_model_sha256: "b".repeat(64), bound_character_id: "c1",
  }],
  hashes: {
    adapter_sha256: { "char.ada": "a".repeat(64) },
    base_model_sha256: "b".repeat(64),
    prompt_sha256: "c".repeat(64),
    runtime_prompt_sha256: "d".repeat(64),
    unavailable: [],
  },
  est_vram_lora_overhead_bytes: 768 * 1024 * 1024,
  context: { shot_id: "s1", keyframe_id: null, model_key: "sd15.fp16",
             proposed_from_cast: true },
  notes: ["advisory resolution — nothing was queued"],
};

const REFUSED = {
  ...ACCEPTED,
  accepted: false,
  errors: ["identity LoRA 'char.ada' is not bound to a character in this shot's cast — bind it via shot.cast first"],
  resolved: [],
  hashes: { ...ACCEPTED.hashes, adapter_sha256: {},
            runtime_prompt_sha256: null,
            unavailable: ["prompt_sha256 — no prompt supplied"] },
};

function renderPanel(props?: Partial<Parameters<typeof LoraResolutionPreview>[0]>) {
  return render(
    <LoraResolutionPreview
      pid="p1" shotId="s1" modelKey="sd15.fp16" {...props} />,
  );
}

describe("LoraResolutionPreview", () => {
  beforeEach(() => loraPreview.mockReset());
  afterEach(cleanup);

  it("renders accepted adapters, pins and the honesty notes", async () => {
    loraPreview.mockResolvedValue(ACCEPTED);
    renderPanel();
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText(/Backend accepts 1 adapter/);
    expect(screen.getAllByText("char.ada").length).toBeGreaterThan(0);
    expect(screen.getByText("identity")).toBeTruthy();
    expect(screen.getByText(/bound to/)).toBeTruthy();
    expect(screen.getAllByText(/aaaaaaaaaaaa/).length).toBeGreaterThan(0);
    expect(screen.getByText(/runtime prompt \(with tags\)/)).toBeTruthy();
    // honesty + advisory notes render
    expect(screen.getByText(/not been validated in this build/)).toBeTruthy();
    expect(screen.getByText(/advisory resolution/)).toBeTruthy();
    expect(screen.getByText(/re-check after changing/)).toBeTruthy();
    // request mirrors context — no loras override → cast-derived
    expect(loraPreview).toHaveBeenCalledWith("p1", expect.objectContaining({
      model_key: "sd15.fp16", shot_id: "s1", loras: undefined }));
  });

  it("renders backend refusals as accessible errors", async () => {
    loraPreview.mockResolvedValue(REFUSED);
    renderPanel();
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText("Backend refuses this selection.");
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("not bound to a character");
  });

  it("renders prompt-guard refusals and unavailable hashes", async () => {
    loraPreview.mockResolvedValue({
      ...REFUSED, errors: [],
      prompt_errors: ["prompt: '<lora:…' looks like a LoRA control tag — adapter selection only happens through the loras field"],
    });
    renderPanel({ prompt: "a door <lora:x:1>" });
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText(/LoRA control tag/);
    expect(screen.getByText(/unavailable — prompt_sha256/)).toBeTruthy();
  });

  it("labels malformed requested entries", async () => {
    loraPreview.mockResolvedValue({
      ...REFUSED, requested: [{ shape: "invalid", key: null, strength: null }],
      errors: ["loras[0] must be a model key or a {'key', 'strength'} object"],
    });
    renderPanel({ loras: [42] });
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText("(malformed entry)");
    expect(loraPreview).toHaveBeenCalledWith("p1",
      expect.objectContaining({ loras: [42] }));
  });

  it("empties selection → accepted-without-adapters wording", async () => {
    loraPreview.mockResolvedValue({
      ...ACCEPTED, requested: [], resolved: [],
      est_vram_lora_overhead_bytes: 0,
      hashes: { ...ACCEPTED.hashes, adapter_sha256: {},
                runtime_prompt_sha256: null },
    });
    renderPanel();
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText("Accepted — the request carries no adapters.");
  });

  it("goes stale on context change — old result clears", async () => {
    loraPreview.mockResolvedValue(ACCEPTED);
    const { rerender } = renderPanel();
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText(/Backend accepts 1 adapter/);
    // switch shot → previous answer must not linger as if still valid
    rerender(
      <LoraResolutionPreview pid="p1" shotId="s2" modelKey="sd15.fp16" />,
    );
    expect(screen.queryByText(/Backend accepts/)).toBeNull();
    expect(loraPreview).toHaveBeenCalledTimes(1);
    // a fresh check on the new context re-requests
    loraPreview.mockResolvedValue(REFUSED);
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText("Backend refuses this selection.");
    expect(loraPreview).toHaveBeenLastCalledWith("p1",
      expect.objectContaining({ shot_id: "s2" }));
  });

  it("HTTP failure surfaces as an error, not a silent drop", async () => {
    loraPreview.mockRejectedValueOnce(new Error("404: not found"));
    renderPanel();
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await waitFor(() => screen.getByText(/404: not found/));
  });

  it("submits nothing else — a single preview call per click", async () => {
    loraPreview.mockResolvedValue(ACCEPTED);
    renderPanel();
    fireEvent.click(screen.getByText("Check what the backend would accept"));
    await screen.findByText(/Backend accepts/);
    expect(loraPreview).toHaveBeenCalledTimes(1);
    // the panel is labeled read-only/advisory in text, not just color
    expect(screen.getByText("read-only · advisory")).toBeTruthy();
  });
});
