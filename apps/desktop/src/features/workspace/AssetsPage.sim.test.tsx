// C2.0d — simulated-provenance honesty on generated assets: the badge
// and the details expander must never present mock/cloud output as
// genuine provider content.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { AssetsPage } from "./AssetsPage";

const apiFetch = vi.fn();
const commitCommand = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
  fetchAssetObjectUrl: () => Promise.resolve("blob:mock"),
  getBackendInfo: () => Promise.resolve({ baseUrl: "http://x", token: "t" }),
}));
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
  pickFile: vi.fn(),
  pickDirectory: vi.fn(),
}));

const store: Record<string, unknown> = {
  currentProject: () => ({ id: "p1", revision: 3 }),
  assetsPage: {
    total: 2, offset: 0, limit: 100,
    items: [
      {
        id: "a-sim", project_id: "p1", name: "beat.txt", kind: "document",
        origin: "generated", scope: "project", status: "ok", uri: "cas://x",
        sha256: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        byte_size: 120, current_version_id: "v1", created_at: "2026-01-01",
        provenance_json: {
          adapter_id: "cloud.text.rest", provenance_confidence: "simulated",
          remote: { remote_job_id: "resp_x", provider_key: "openai", transport: "simulated" },
        },
        media_json: { source: "cloud-rest-simulated", is_mock: false },
      },
      {
        id: "a-real", project_id: "p1", name: "scan.png", kind: "image",
        origin: "imported", scope: "project", status: "ok", uri: "cas://y",
        sha256: "aaaa", byte_size: 5000, current_version_id: "v2", created_at: "2026-01-01",
        provenance_json: { provenance_confidence: "local" }, media_json: {},
      },
    ],
  },
  assetKindFilter: "",
  setAssetKindFilter: vi.fn(),
  refreshAssets: vi.fn(() => Promise.resolve()),
  refreshProjectData: vi.fn(() => Promise.resolve()),
};
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

beforeEach(() => {
  cleanup();
  vi.clearAllMocks();
  apiFetch.mockResolvedValue([]);
});

describe("AssetsPage — simulated provenance honesty", () => {
  it("labels simulated assets and shows the banner in details", async () => {
    render(createElement(AssetsPage));
    await waitFor(() => screen.getByText("beat.txt"));
    expect(screen.getByText("simulated")).toBeTruthy();
    // imported asset carries no simulated chip
    expect(screen.getAllByText("simulated")).toHaveLength(1);
    fireEvent.click(screen.getAllByText("Details")[0]);
    const detail = screen.getByTestId("asset-detail-a-sim");
    expect(detail.textContent).toContain("SIMULATION — No external AI provider contacted.");
    expect(detail.textContent).toContain("resp_x");
    expect(detail.textContent).toContain("(synthetic)");
    expect(detail.textContent).toContain("cloud.text.rest");
  });

  it("non-simulated asset details show no simulation banner", async () => {
    render(createElement(AssetsPage));
    await waitFor(() => screen.getByText("scan.png"));
    fireEvent.click(screen.getAllByText("Details")[1]);
    const detail = screen.getByTestId("asset-detail-a-real");
    expect(detail.textContent).not.toContain("SIMULATION");
    expect(detail.textContent).toContain("local");
  });
});
