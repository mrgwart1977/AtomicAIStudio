// C2 exit gate Stage A — enablement surface: honesty, explicit gestures,
// kill-switch state. All backend calls mocked.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { LiveGatePanel } from "./LiveGatePanel";

const apiFetch = vi.fn();
const commitCommand = vi.fn();

vi.mock("../../api/client", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
}));

const DETAILS = {
  deepseek: { key: "deepseek", label: "DeepSeek", models: [
    { id: "deepseek-chat", capability: "text", cost_table: {} },
  ], strategies: [] },
  xai: { key: "xai", label: "xAI", models: [
    { id: "grok-4", capability: "text", cost_table: {} },
  ], strategies: [] },
} as never;

function status(over?: Record<string, unknown>) {
  return {
    stage: "A — safety gates only; no live dispatch exists",
    global_disabled: false, global_disabled_at: null,
    caps_usd: { per_request: "0.10", per_provider: "0.15", global: "0.75" },
    global_exposure_usd: 0, timeouts: {},
    providers: {
      deepseek: { enabled: false, model_id: null, credential_ref: null,
        granted_at: null, revoked_at: null, revoked_reason: null,
        credential_configured: true, dispatch_ready: false,
        blocked_reason: "not_enabled", live_exposure_usd: 0 },
      xai: { enabled: false, model_id: null, credential_ref: null,
        granted_at: null, revoked_at: null, revoked_reason: null,
        credential_configured: false, dispatch_ready: false,
        blocked_reason: "not_enabled", live_exposure_usd: 0 },
    },
    ...over,
  };
}

const PROPS = { pid: "p1", revision: 3, details: DETAILS,
                onCommitted: vi.fn() };

describe("LiveGatePanel", () => {
  beforeEach(() => {
    cleanup(); vi.clearAllMocks();
    apiFetch.mockImplementation((u: string) =>
      u.includes("cloud/live-status") ? Promise.resolve(status())
                                      : Promise.reject(new Error("unmocked")));
    commitCommand.mockResolvedValue({ job_id: null });
  });

  it("declares Stage A honesty — no live dispatch exists", async () => {
    render(createElement(LiveGatePanel, PROPS));
    await screen.findByTestId("live-provider-rows");
    expect(screen.getByText(/No live dispatch exists in this build/)).toBeTruthy();
    expect(screen.getByText(/0\.10\/request/)).toBeTruthy();
    expect(screen.getByTestId("live-blocked-xai").textContent)
      .toContain("not_enabled");
  });

  it("enable rides cloud.live.enable with provider/model/credential_ref", async () => {
    render(createElement(LiveGatePanel, PROPS));
    fireEvent.click(await screen.findByTestId("live-enable-deepseek"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    expect(commitCommand).toHaveBeenCalledWith("p1", 3, "cloud.live.enable",
      { provider_key: "deepseek", model_id: "deepseek-chat",
        credential_ref: "deepseek" });
  });

  it("disable + kill switch + resume dispatch correct commands", async () => {
    apiFetch.mockImplementation((u: string) =>
      u.includes("cloud/live-status")
        ? Promise.resolve(status({
            providers: { deepseek: { ...status().providers.deepseek,
              enabled: true, model_id: "deepseek-chat", dispatch_ready: true,
              blocked_reason: null } } }))
        : Promise.reject(new Error("unmocked")));
    render(createElement(LiveGatePanel, PROPS));
    fireEvent.click(await screen.findByTestId("live-disable-deepseek"));
    await waitFor(() => expect(commitCommand)
      .toHaveBeenCalledWith("p1", 3, "cloud.live.disable",
                            { provider_key: "deepseek" }));
    fireEvent.click(screen.getByTestId("live-kill"));
    await waitFor(() => expect(commitCommand)
      .toHaveBeenCalledWith("p1", 3, "cloud.live.disable_all", {}));
  });

  it("kill-active banner + resume replaces enable controls", async () => {
    apiFetch.mockImplementation((u: string) =>
      u.includes("cloud/live-status")
        ? Promise.resolve(status({ global_disabled: true,
          global_disabled_at: "2026-11-12T00:00:00Z" }))
        : Promise.reject(new Error("unmocked")));
    render(createElement(LiveGatePanel, PROPS));
    expect(await screen.findByTestId("live-kill-banner")).toBeTruthy();
    expect(screen.queryByTestId("live-enable-deepseek")).toBeNull();
    fireEvent.click(screen.getByTestId("live-resume"));
    await waitFor(() => expect(commitCommand)
      .toHaveBeenCalledWith("p1", 3, "cloud.live.resume", {}));
  });

  it("surfaces enable refusals as error text", async () => {
    commitCommand.mockRejectedValueOnce(new Error("422: credential"));
    render(createElement(LiveGatePanel, PROPS));
    fireEvent.click(await screen.findByTestId("live-enable-deepseek"));
    await waitFor(() => screen.getByText(/422: credential/));
  });
});
