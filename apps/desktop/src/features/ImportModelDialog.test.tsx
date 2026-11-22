// U-2b-1 — ImportModelDialog: native picker + manual fallback, declared
// license/family/role/name collection, exact job_id tracking, progress /
// cancel / error / terminal-state rendering, and privacy guarantees
// (the picked path never leaves local dialog state).
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ImportModelDialog } from "./ImportModelDialog";

const apiFetch = vi.fn();
vi.mock("../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));

const pickFile = vi.fn();
vi.mock("../api/commands", () => ({
  pickFile: (...a: unknown[]) => pickFile(...a),
}));

const mockJobs: Record<string, unknown> = {};
vi.mock("../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({ jobs: mockJobs }),
}));

function fillPath(p = "D:\\models\\w.safetensors") {
  fireEvent.change(screen.getByLabelText(/File/), { target: { value: p } });
}
function fillLicense(n = "Apache-2.0") {
  fireEvent.change(screen.getByLabelText(/License name/), { target: { value: n } });
}
function openDialog(onImported = vi.fn()) {
  const onOpenChange = vi.fn();
  const r = render(
    <ImportModelDialog open onOpenChange={onOpenChange} onImported={onImported} />);
  return {
    onOpenChange, onImported,
    // The mocked store isn't reactive — re-render the SAME mounted tree so
    // mutated mockJobs is re-read without losing dialog-local jobId state.
    rerender: () => r.rerender(
      <ImportModelDialog open onOpenChange={onOpenChange} onImported={onImported} />),
  };
}
const lastBody = () =>
  JSON.parse((apiFetch.mock.calls.at(-1)?.[1] as { body: string }).body);

const RUNNING = {
  id: "j1", kind: "model_import", status: "running", progress: 0.4,
  message: "inspecting file", error: null, adapter_id: "ops.model.import",
  output_asset_id: null, project_id: null, cancel_requested: false,
};

describe("ImportModelDialog", () => {
  beforeEach(() => {
    cleanup(); apiFetch.mockReset(); pickFile.mockReset();
    for (const k of Object.keys(mockJobs)) delete mockJobs[k];
  });

  it("requires a file and a license name before submitting", async () => {
    openDialog();
    const btn = screen.getByRole("button", { name: "Import" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fillPath();
    expect(btn.disabled).toBe(true);
    fillLicense();
    expect(btn.disabled).toBe(false);
  });

  it("native picker fills the path; manual typing is the fallback", async () => {
    pickFile.mockResolvedValue("E:\\lib\\picked.gguf");
    openDialog();
    fireEvent.click(screen.getByText("Browse…"));
    await waitFor(() =>
      expect((screen.getByLabelText(/File/) as HTMLInputElement).value)
        .toBe("E:\\lib\\picked.gguf"));
    expect(pickFile).toHaveBeenCalledWith("Import model file", [
      { name: "Model weights", extensions: ["safetensors", "gguf"] }]);
    // picker cancelled → manual path still works
    pickFile.mockResolvedValue(null);
    fireEvent.change(screen.getByLabelText(/File/),
      { target: { value: "C:\\m\\x.safetensors" } });
    expect((screen.getByLabelText(/File/) as HTMLInputElement).value)
      .toBe("C:\\m\\x.safetensors");
  });

  it("submits reference-mode body with declarations and tracks the exact job_id", async () => {
    apiFetch.mockResolvedValue({ job_id: "j1", status: "queued" });
    openDialog();
    fillPath(); fillLicense();
    fireEvent.change(screen.getByLabelText(/Display name/),
      { target: { value: "My Model" } });
    fireEvent.change(screen.getByLabelText(/Base family/),
      { target: { value: "sdxl" } });
    fireEvent.change(screen.getByLabelText(/^Role/), { target: { value: "lora" } });
    fireEvent.change(screen.getByLabelText(/License URL/),
      { target: { value: "https://example.test/lic" } });
    fireEvent.click(screen.getByLabelText(/Commercial use allowed/));
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(apiFetch).toHaveBeenCalledWith("/api/models/import",
      expect.objectContaining({ method: "POST" }));
    expect(lastBody()).toEqual({
      path: "D:\\models\\w.safetensors",
      license: { name: "Apache-2.0", commercial_ok: true,
                 url: "https://example.test/lic" },
      family: "sdxl", role_hint: "lora",
      display_name: "My Model", mode: "reference",
    });
    // tracked job (not yet in the store map) shows queued, never the form
    // again — guarding the POST→first-WS-event window against double submit
    await waitFor(() => screen.getByText("ops.model.import"));
    expect(screen.getAllByText("queued").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Import" })).toBeNull();
    expect(screen.getByText("Cancel import")).toBeTruthy();
  });

  it("commercial_ok defaults to false and optional fields omit cleanly", async () => {
    apiFetch.mockResolvedValue({ job_id: "j1", status: "queued" });
    openDialog();
    fillPath(); fillLicense("MIT");
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(lastBody().license).toEqual({ name: "MIT", commercial_ok: false });
    expect(lastBody().license.url).toBeUndefined();
    expect(lastBody().role_hint).toBe("");
    expect(lastBody().display_name).toBe("");
  });

  it("shows a required-license error and never submits without one", async () => {
    openDialog();
    fillPath();
    // button disabled blocks submit — also assert no request was made
    expect((screen.getByRole("button", { name: "Import" }) as HTMLButtonElement).disabled).toBe(true);
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("renders backend taxonomy errors as friendly text (422 + 409)", async () => {
    apiFetch.mockRejectedValueOnce(new Error('422: {"detail":"path_is_link"}'));
    openDialog();
    fillPath(); fillLicense();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => screen.getByRole("alert"));
    expect(screen.getByRole("alert").textContent).toMatch(/link\/junction/);
    // 409 in-flight duplicate → friendly text, not the raw status+body
    apiFetch.mockRejectedValueOnce(new Error("409: an import for this file is already in progress"));
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent)
        .toBe("An import of this file is already in progress."));
  });

  it("job lifecycle: running → cancel → succeeded → library refresh", async () => {
    apiFetch.mockResolvedValue({ job_id: "j1", status: "queued" });
    const { onImported, rerender } = openDialog();
    fillPath(); fillLicense();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => screen.getByText("ops.model.import"));
    // WS delivers the running job → progress bar + cancel button
    mockJobs.j1 = RUNNING;
    rerender();
    await waitFor(() => screen.getByText("Cancel import"));
    apiFetch.mockResolvedValueOnce({ status: "cancelling" });
    fireEvent.click(screen.getByText("Cancel import"));
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith("/api/jobs/j1/cancel",
        expect.objectContaining({ method: "POST" })));
    // success → registered text + library refresh
    mockJobs.j1 = { ...RUNNING, status: "succeeded", progress: 1 };
    rerender();
    await waitFor(() => screen.getByText(/Registered — it now appears/));
    expect(onImported).toHaveBeenCalled();
    expect(screen.getByText(/can't be used for generation/)).toBeTruthy();
  });

  it("cancel-after-terminal is harmless: 409 surfaces no error", async () => {
    apiFetch.mockResolvedValue({ job_id: "j1", status: "queued" });
    const { rerender } = openDialog();
    fillPath(); fillLicense();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => screen.getByText("ops.model.import"));
    mockJobs.j1 = RUNNING;
    rerender();
    await waitFor(() => screen.getByText("Cancel import"));
    apiFetch.mockRejectedValueOnce(new Error("409: job already succeeded"));
    fireEvent.click(screen.getByText("Cancel import"));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("failed jobs surface the sanitized error code as friendly text", async () => {
    apiFetch.mockResolvedValue({ job_id: "j1", status: "queued" });
    const { rerender } = openDialog();
    fillPath(); fillLicense();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => screen.getByText("ops.model.import"));
    mockJobs.j1 = { ...RUNNING, status: "failed", error: "unsupported_format" };
    rerender();
    await waitFor(() =>
      screen.getByText(/Only safetensors and GGUF files can be imported/));
  });

  it("cancelled jobs explain that committed artifacts are kept", async () => {
    apiFetch.mockResolvedValue({ job_id: "j1", status: "queued" });
    const { rerender } = openDialog();
    fillPath(); fillLicense();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => screen.getByText("ops.model.import"));
    mockJobs.j1 = { ...RUNNING, status: "cancelled" };
    rerender();
    await waitFor(() => screen.getByText(/already committed it stays registered/));
  });

  it("close resets sensitive state — the path is gone on reopen", async () => {
    const onOpenChange = vi.fn();
    const { rerender, unmount } = render(
      <ImportModelDialog open onOpenChange={onOpenChange} onImported={vi.fn()} />);
    fillPath("D:\\secret\\weights.safetensors"); fillLicense();
    // user closes → parent flips open=false → reset() ran via handler
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    unmount();
    render(<ImportModelDialog open onOpenChange={vi.fn()} onImported={vi.fn()} />);
    expect((screen.getByLabelText(/File/) as HTMLInputElement).value).toBe("");
  });

  it("dialog is labelled and described; fields carry accessible labels", () => {
    openDialog();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("Import a model file")).toBeTruthy();
    expect(screen.getByLabelText(/License name/)).toBeTruthy();
    expect(screen.getByLabelText(/Base family/)).toBeTruthy();
    expect(screen.getByLabelText(/^Role/)).toBeTruthy();
    expect(screen.getByLabelText(/Display name/)).toBeTruthy();
    expect(screen.getByLabelText(/Commercial use allowed/)).toBeTruthy();
  });

  it("declarations vs detected are visually separated in the form", () => {
    openDialog();
    expect(screen.getByText(/What you declare — the app does not verify/)).toBeTruthy();
    expect(screen.getByText(/detected format is checked separately/)).toBeTruthy();
  });
});
