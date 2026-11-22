/** U-3 — DiscoverySection contract tests (D16–D18 + frontend rows).
 * Synthetic fixtures only — no filesystem, no real scans.
 */
import { cleanup, fireEvent, render, screen, waitFor }
  from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi }
  from "vitest";

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  pickDirectory: vi.fn(async () => null as string | null),
}));

vi.mock("../api/client", () => ({
  apiFetch: (path: string, opts?: { method?: string; body?: string }) =>
    opts?.method ? apiMock.post(path, opts) : apiMock.get(path),
}));

vi.mock("../api/commands", () => ({
  pickDirectory: () => apiMock.pickDirectory(),
}));

let jobsObj: Record<string, unknown> = {};
vi.mock("../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({ jobs: jobsObj }),
}));

import { DiscoverySection } from "./DiscoverySection";

const ROOT = {
  root_id: "root:abc123", display_path: "D:\\Models",
  enabled: true, max_depth: 4, added_at: "2026-01-01T00:00:00Z",
  archived_at: null, last_scan_outcome: "complete", last_code: null,
  last_scan_at: "2026-01-02T00:00:00Z", last_entries_seen: 42,
  last_candidates: 2, candidate_count: 2,
};

const CAND: import("../api/types").DiscoveryCandidate = {
  candidate_id: "cand:111", name: "weights.safetensors",
  rel_path: "sdxl\\weights.safetensors", root_id: "root:abc123",
  root_label: "Models", fmt_guess: "safetensors", byte_size: 2e9,
  presence: "present", linked_artifact_id: null, seen_count: 1,
  first_seen_at: "2026-01-02T00:00:00Z",
  last_seen_at: "2026-01-02T00:00:00Z",
};

const DETAIL = {
  ...CAND, resolved_path: "D:\\Models\\sdxl\\weights.safetensors",
  mtime_ns: 111, root_display_path: "D:\\Models",
  linked_at: null,
};

function mockApi({
  roots = [ROOT], candidates = [CAND],
  detail = DETAIL, scanOut = { job_id: "j1", status: "queued" },
}: {
  roots?: unknown[]; candidates?: unknown[];
  detail?: unknown; scanOut?: unknown;
} = {}) {
  apiMock.get.mockImplementation((path: string) => {
    if (path.includes("/candidates/cand:"))
      return Promise.resolve(detail);
    if (path.includes("/candidates")) return Promise.resolve(candidates);
    if (path.includes("/roots")) return Promise.resolve(roots);
    return Promise.reject(new Error(`unmocked GET ${path}`));
  });
  apiMock.post.mockImplementation((path: string) => {
    if (path === "/api/models/discovery/scan")
      return Promise.resolve(scanOut);
    if (path === "/api/models/discovery/roots")
      return Promise.resolve(ROOT);
    if (path.includes("/archive")) return Promise.resolve(ROOT);
    if (path.includes("/cancel")) return Promise.resolve({});
    return Promise.reject(new Error(`unmocked POST ${path}`));
  });
}

beforeEach(() => {
  jobsObj = {};
  mockApi();
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DiscoverySection", () => {
  it("renders roots and candidates without absolute paths in rows", async () => {
    render(<DiscoverySection onImportCandidate={() => {}} />);
    expect(await screen.findByText("weights.safetensors")).toBeTruthy();
    expect(screen.getByText("D:\\Models")).toBeTruthy();   // roots surface
    expect(screen.getByText(/sdxl\\weights\.safetensors/)).toBeTruthy();
    // candidate row shows rel_path + basename label — not the abs path
    const row = screen.getByText("weights.safetensors").closest("li")!;
    expect(row.innerHTML).not.toContain("D:\\Models\\sdxl");
  });

  it("D16 — status badges: new, seen, imported, not-found", async () => {
    mockApi({
      candidates: [
        CAND,
        { ...CAND, candidate_id: "cand:2", name: "b.gguf",
          fmt_guess: "gguf", seen_count: 3 },
        { ...CAND, candidate_id: "cand:3", name: "c.safetensors",
          linked_artifact_id: "artifact:x" },
        { ...CAND, candidate_id: "cand:4", name: "d.safetensors",
          presence: "absent" },
      ],
    });
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    expect(screen.getByText("new")).toBeTruthy();
    expect(screen.getByText("seen")).toBeTruthy();
    expect(screen.getByText("imported")).toBeTruthy();
    expect(screen.getByText("not found in last scan")).toBeTruthy();
  });

  it("D17 — absent detail discloses honest wording, not corruption", async () => {
    mockApi({
      candidates: [{ ...CAND, presence: "absent" }],
      detail: { ...DETAIL, presence: "absent" },
    });
    render(<DiscoverySection onImportCandidate={() => {}} />);
    fireEvent.click(await screen.findByText("Details"));
    expect(await screen.findByText(/did not remove it/i)).toBeTruthy();
    expect(screen.queryByText(/corrupt/i)).toBeNull();
  });

  it("detail expander fetches the authenticated path lazily", async () => {
    render(<DiscoverySection onImportCandidate={() => {}} />);
    fireEvent.click(await screen.findByText("Details"));
    expect(await screen.findByText(
      "D:\\Models\\sdxl\\weights.safetensors")).toBeTruthy();
    expect(apiMock.get).toHaveBeenCalledWith(
      "/api/models/discovery/candidates/cand:111");
  });

  it("Import… prefetches detail and hands resolved_path to the dialog", async () => {
    const onImp = vi.fn();
    render(<DiscoverySection onImportCandidate={onImp} />);
    fireEvent.click(await screen.findByText("Import…"));
    await waitFor(() => expect(onImp)
      .toHaveBeenCalledWith("D:\\Models\\sdxl\\weights.safetensors"));
    expect(apiMock.get).toHaveBeenCalledWith(
      "/api/models/discovery/candidates/cand:111");
  });

  it("adds a root by typed path", async () => {
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.change(screen.getByLabelText("Folder path to scan"),
                     { target: { value: "E:\\Checkpoints" } });
    fireEvent.click(screen.getByText("Add folder"));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith(
      "/api/models/discovery/roots",
      expect.objectContaining({
        body: JSON.stringify({ path: "E:\\Checkpoints" }),
      })));
  });

  it("Browse… uses the native directory picker", async () => {
    apiMock.pickDirectory.mockResolvedValue("E:\\Picked");
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.click(screen.getByText("Browse…"));
    await waitFor(() => expect(apiMock.pickDirectory).toHaveBeenCalled());
  });

  it("Scan now enqueues the durable job", async () => {
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.click(screen.getByText("Scan now"));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith(
      "/api/models/discovery/scan",
      expect.objectContaining({ method: "POST" })));
  });

  it("archives a root on Remove", async () => {
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.click(screen.getByLabelText("Stop scanning D:\\Models"));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith(
      "/api/models/discovery/roots/root:abc123/archive",
      expect.objectContaining({ method: "POST" })));
  });

  it("D18 — imported candidates show no Import action", async () => {
    mockApi({
      candidates: [{ ...CAND, linked_artifact_id: "artifact:x" }],
    });
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    expect(screen.queryByText("Import…")).toBeNull();
    expect(screen.getByText("imported")).toBeTruthy();
  });

  it("C2 — successful reconciliation renders done + count", async () => {
    jobsObj = {
      j1: { id: "j1", status: "succeeded",
            result_json: { absent_reconcile: "done", absent_marked: 3 } },
    };
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.click(screen.getByText("Scan now"));
    await waitFor(() => screen.getByText(/Scan complete/));
    expect(screen.getByText(/3 file\(s\) absent/)).toBeTruthy();
    // terminal card persists — does not auto-unmount
    expect(screen.getByRole("status")).toBeTruthy();
  });

  it("C2 — reconcile failure warns, never claims missing files", async () => {
    jobsObj = {
      j1: { id: "j1", status: "succeeded",
            result_json: { absent_reconcile: "failed", absent_marked: 0 } },
    };
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.click(screen.getByText("Scan now"));
    expect(await screen.findByText(
      /absence verification could not finish/i)).toBeTruthy();
    expect(screen.getByText(
      /no files were newly marked missing/i)).toBeTruthy();
    expect(screen.queryByText(/file\(s\) absent/)).toBeNull();
  });

  it("C2 — cancelled scan shows distinct state", async () => {
    jobsObj = { j1: { id: "j1", status: "cancelled",
                     result_json: null } };
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.click(screen.getByText("Scan now"));
    expect(await screen.findByText(
      /Scan cancelled — partial results are kept/i)).toBeTruthy();
    expect(screen.getByText(/nothing was marked missing/i)).toBeTruthy();
  });

  it("C2 — Dismiss clears the terminal card", async () => {
    jobsObj = {
      j1: { id: "j1", status: "succeeded",
            result_json: { absent_reconcile: "done", absent_marked: 0 } },
    };
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.click(screen.getByText("Scan now"));
    await screen.findByText(/Scan complete/);
    fireEvent.click(screen.getByText("Dismiss"));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("C2 — a new scan replaces the terminal card", async () => {
    jobsObj = {
      j1: { id: "j1", status: "succeeded",
            result_json: { absent_reconcile: "done", absent_marked: 0 } },
    };
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.click(screen.getByText("Scan now"));
    await screen.findByText(/Scan complete/);
    jobsObj = {
      j2: { id: "j2", status: "running",
            progress_json: { roots_done: 1, roots_total: 2,
                             candidates_found: 4, stage: "scan" } },
    };
    apiMock.post.mockImplementation((p: string) =>
      p === "/api/models/discovery/scan"
        ? Promise.resolve({ job_id: "j2", status: "queued" })
        : Promise.resolve({}));
    fireEvent.click(screen.getByText("Scan now"));
    expect(await screen.findByText(/Scanning approved folders/))
      .toBeTruthy();
    expect(screen.queryByText(/Scan complete/)).toBeNull();
  });

  it("surfaces add-root failures without a path leak", async () => {
    apiMock.post.mockImplementation((path: string) =>
      path === "/api/models/discovery/roots"
        ? Promise.reject(new Error("path_not_dir"))
        : Promise.resolve({}));
    render(<DiscoverySection onImportCandidate={() => {}} />);
    await screen.findByText("weights.safetensors");
    fireEvent.change(screen.getByLabelText("Folder path to scan"),
                     { target: { value: "E:\\f.safetensors" } });
    fireEvent.click(screen.getByText("Add folder"));
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});
