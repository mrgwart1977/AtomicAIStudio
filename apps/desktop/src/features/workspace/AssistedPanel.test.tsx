// C1.5 — assisted text workflow tests: eligibility, capability honesty,
// explicit-consent prepare/copy/open, exact-share transparency, import
// paths (paste/file/watch), assisted provenance, and zero automatic
// egress or automation.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { AssistedPanel, assistStrategies } from "./AssistedPanel";

const apiFetch = vi.fn();
const commitCommand = vi.fn();
const pickFile = vi.fn();
const openBridgeResult = vi.fn();

vi.mock("../../api/client", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
  pickFile: (...a: unknown[]) => pickFile(...a),
}));
vi.mock("../../api/bridge", () => ({
  openBridgeResult: (...a: unknown[]) => openBridgeResult(...a),
}));

const refreshAssets = vi.fn();
const store: Record<string, unknown> = {
  refreshAssets,
  entities: {
    character: [
      { id: "c1", name: "Mara Voss", summary: "Retired pilot.", status: "active" },
    ],
    world: [],
    location: [],
  },
  storyboard: [
    { scene: { id: "s1", title: "Docking Bay", description: "Cold blue light.", intent: "Unease" },
      location_name: "", cast: [], shots: [] },
  ],
};
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

const STRAT_T2 = {
  method: "assisted_workflow", tier: "T2", capabilities: ["text.generate"],
  automation_permission: "user_driven", user_interaction: "site", note: "",
  connection: { launch_url: "https://sudowrite.com" },
};
const STRAT_T3 = {
  method: "import_export", tier: "T3", capabilities: ["text.roundtrip"],
  automation_permission: "user_driven", user_interaction: "file", note: "",
};

const DETAILS: Record<string, unknown> = {
  sudowrite: {
    key: "sudowrite", label: "Sudowrite", access_class: "E",
    strategies: [STRAT_T2, { ...STRAT_T3, capabilities: ["text.import"] }],
    tos_url: "https://sudowrite.com/terms",
    privacy_summary: "Prompt text goes to Sudowrite's site.",
    status_reason: "No public API — assisted only.",
    settings: {},
  },
  hemingway: {
    key: "hemingway", label: "Hemingway Editor Plus", access_class: "E",
    strategies: [STRAT_T3],
    docs_url: "https://www.hemingwayapp.com/",
    tos_url: null, privacy_summary: "Paste text into their editor.",
    status_reason: "No API.",
    settings: {},
  },
  jasper: {
    key: "jasper", label: "Jasper", access_class: "B",
    strategies: [
      { method: "official_api", tier: "T1", capabilities: ["text.generate"],
        automation_permission: "api", user_interaction: "none", note: "Business only" },
      STRAT_T2,
    ],
    tos_url: null, privacy_summary: "Sends prompt to Jasper.",
    status_reason: "", settings: {},
  },
  openai: {
    key: "openai", label: "OpenAI", access_class: "A",
    strategies: [{ method: "official_api", tier: "T1", capabilities: ["text.generate"],
                   automation_permission: "api", user_interaction: "none", note: "" }],
    tos_url: null, privacy_summary: "", status_reason: "", settings: {},
  },
  banned: {
    key: "banned", label: "Banned Co", access_class: "E",
    strategies: [{ ...STRAT_T2, automation_permission: "prohibited" }],
    tos_url: null, privacy_summary: "", status_reason: "", settings: {},
  },
  watchcfg: {
    key: "watchcfg", label: "Watch Co", access_class: "E",
    strategies: [STRAT_T3],
    tos_url: null, privacy_summary: "", status_reason: "",
    settings: { import_dir: "D:/exports" },
  },
};

const CAPS_SUDO = {
  provider_key: "sudowrite", evidence_stamp: "2026-01",
  capabilities: [{
    capability: "text.generate", strategy_id: "t2", method: "assisted_workflow",
    tier: "T2", status: "documented", stale: false,
    verification: "catalog_documented",
    dispatch: ["prepare", "launch", "import"], dispatch_blocked: null,
    required_user_action: "site_workflow", alternate: null,
  }],
};
const CAPS_HEMI = {
  provider_key: "hemingway", evidence_stamp: "2026-01",
  capabilities: [{
    capability: "text.roundtrip", strategy_id: "t3", method: "import_export",
    tier: "T3", status: "documented", stale: false,
    verification: "catalog_documented",
    dispatch: ["import"], dispatch_blocked: null,
    required_user_action: "file_import", alternate: null,
  }],
};
const CAPS_WATCH = {
  provider_key: "watchcfg", evidence_stamp: "2026-01",
  capabilities: [{
    capability: "text.roundtrip", strategy_id: "t3", method: "import_export",
    tier: "T3", status: "documented", stale: false,
    verification: "catalog_documented",
    dispatch: ["import"], dispatch_blocked: null,
    required_user_action: "file_import", alternate: null,
  }],
};
const CAPS_STALE = {
  provider_key: "sudowrite", evidence_stamp: "2026-01",
  capabilities: [{
    capability: "text.generate", strategy_id: "t2", method: "assisted_workflow",
    tier: "T2", status: "needs_recheck", stale: true,
    verification: "catalog_documented",
    dispatch: [], dispatch_blocked: "stale",
    required_user_action: "reverify_strategy", alternate: null,
  }],
};

const PROPS = {
  pid: "p1", revision: 7,
  providers: [{ key: "sudowrite", label: "Sudowrite" }],
  details: DETAILS as never,
  onCommitted: vi.fn(),
};

function capsFor(key: string) {
  if (key === "sudowrite") return Promise.resolve(CAPS_SUDO);
  if (key === "hemingway") return Promise.resolve(CAPS_HEMI);
  if (key === "watchcfg") return Promise.resolve(CAPS_WATCH);
  return Promise.resolve({ provider_key: key, capabilities: [] });
}

async function select(key: string) {
  fireEvent.change(screen.getByLabelText("Assisted provider"), { target: { value: key } });
  await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(`/api/providers/${key}/capabilities`));
  // provider-agnostic readiness signal — the step rail only renders
  // for T2 providers; caps-dependent controls are awaited per-test.
  await waitFor(() => screen.getByTestId("assist-share-preview"));
}

describe("AssistedPanel — eligibility & honesty", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    apiFetch.mockImplementation((url: string) =>
      url.startsWith("/api/providers/")
        ? capsFor(url.replace("/capabilities", "").split("/").pop() ?? "")
        : Promise.reject());
  });

  it("offers only providers with a documented assisted text path", async () => {
    render(createElement(AssistedPanel, PROPS));
    expect(await screen.findByText("Sudowrite (assisted)")).toBeTruthy();
    expect(screen.getByText("Hemingway Editor Plus (assisted)")).toBeTruthy();
    expect(screen.getByText("Jasper (assisted)")).toBeTruthy();
    expect(screen.queryByText("OpenAI (assisted)")).toBeNull();
    expect(screen.queryByText("Banned Co (assisted)")).toBeNull();
  });

  it("never calls a provider API and collects no credentials", async () => {
    render(createElement(AssistedPanel, PROPS));
    await select("sudowrite");
    // nothing is committed without a user gesture
    expect(commitCommand).not.toHaveBeenCalled();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByLabelText(/api key|secret|token|password/i)).toBeNull();
    expect(screen.getAllByText(/no API integration/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/separate account, subscription, or fees/)).toBeTruthy();
  });

  it("shows provider facts: privacy, status, and a terms link", async () => {
    render(createElement(AssistedPanel, PROPS));
    await select("sudowrite");
    const facts = await screen.findByTestId("assist-provider-facts");
    expect(facts.textContent).toContain("assisted workflow only");
    expect(facts.textContent).toContain("Prompt text goes to Sudowrite");
    expect(facts.textContent).toContain("No public API");
    expect(facts.querySelector("a")?.getAttribute("href")).toBe("https://sudowrite.com/terms");
  });
});

describe("AssistedPanel — prepare → review → share", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    apiFetch.mockImplementation((url: string) =>
      url.startsWith("/api/providers/")
        ? capsFor(url.replace("/capabilities", "").split("/").pop() ?? "")
        : Promise.reject());
  });

  it("composes project context + prompt into the exact share preview", async () => {
    render(createElement(AssistedPanel, PROPS));
    await select("sudowrite");
    fireEvent.change(screen.getByLabelText("Your text / instructions"),
      { target: { value: "Write the docking scene." } });
    fireEvent.click(screen.getByLabelText(/character: Mara Voss/));
    fireEvent.click(screen.getByLabelText(/scene: Docking Bay/));
    const preview = screen.getByTestId("assist-share-preview");
    expect(preview.textContent).toContain("### character: Mara Voss");
    expect(preview.textContent).toContain("Retired pilot.");
    expect(preview.textContent).toContain("### scene: Docking Bay");
    expect(preview.textContent).toContain("Intent: Unease");
    expect(preview.textContent).toContain("Write the docking scene.");
    // still nothing committed — composing is local-only
    expect(commitCommand).not.toHaveBeenCalled();
  });

  it("prepare → review → copy → open, each behind explicit clicks", async () => {
    const clipboard = { writeText: vi.fn().mockResolvedValue(undefined) };
    Object.defineProperty(navigator, "clipboard", { value: clipboard, configurable: true });
    commitCommand.mockImplementation((_pid: string, _rev: number, ct: string) => {
      if (ct === "bridge.prepare")
        return Promise.resolve({ job_id: null, bridge: { action: "prepare", prepared_text: "PREPARED-XYZ", char_count: 11 } });
      if (ct === "bridge.launch")
        return Promise.resolve({ job_id: null, bridge: { action: "launch", open_via: "client_shell", url: "https://sudowrite.com" } });
      return Promise.resolve({ job_id: "j1", bridge: null });
    });
    openBridgeResult.mockResolvedValue("https://sudowrite.com");

    render(createElement(AssistedPanel, PROPS));
    await select("sudowrite");
    fireEvent.change(screen.getByLabelText("Your text / instructions"),
      { target: { value: "Write a beat." } });
    fireEvent.click(await screen.findByTestId("assist-prepare"));
    await waitFor(() => screen.getByTestId("assist-prepared-text"));
    const p = commitCommand.mock.calls[0];
    expect(p[2]).toBe("bridge.prepare");
    expect((p[3] as { provider_key: string }).provider_key).toBe("sudowrite");
    expect(screen.getByTestId("assist-prepared-text").textContent).toBe("PREPARED-XYZ");

    fireEvent.click(screen.getByTestId("assist-copy"));
    await waitFor(() => expect(clipboard.writeText).toHaveBeenCalledWith("PREPARED-XYZ"));

    fireEvent.click(screen.getByTestId("assist-open"));
    await waitFor(() => expect(openBridgeResult).toHaveBeenCalled());
    expect(commitCommand.mock.calls[1][2]).toBe("bridge.launch");
    await waitFor(() => screen.getByText(/Opened https:\/\/sudowrite\.com/));
  });

  it("blocks prepare when the strategy evidence is stale", async () => {
    apiFetch.mockImplementation((url: string) =>
      url.includes("capabilities") ? Promise.resolve(CAPS_STALE) : Promise.reject());
    render(createElement(AssistedPanel, PROPS));
    await select("sudowrite");
    expect(await screen.findByTestId("assist-stale")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Your text / instructions"),
      { target: { value: "x" } });
    // stale evidence → no dispatch at all: the prepare control is not
    // even offered, and nothing commits.
    expect(screen.queryByTestId("assist-prepare")).toBeNull();
    expect(commitCommand).not.toHaveBeenCalled();
  });
});

describe("AssistedPanel — return & import", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    apiFetch.mockImplementation((url: string) =>
      url.startsWith("/api/providers/")
        ? capsFor(url.replace("/capabilities", "").split("/").pop() ?? "")
        : Promise.reject());
    commitCommand.mockResolvedValue({ job_id: "job-12345", bridge: null });
  });

  it("imports pasted text via bridge.import with user-reported provenance", async () => {
    render(createElement(AssistedPanel, PROPS));
    await select("sudowrite");
    fireEvent.change(await screen.findByTestId("assist-paste"),
      { target: { value: "provider output text" } });
    fireEvent.click(screen.getByTestId("assist-import-paste"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    const p = commitCommand.mock.calls[0];
    expect(p[2]).toBe("bridge.import");
    expect(p[3]).toMatchObject({ provider_key: "sudowrite", kind: "text",
                                 content: "provider output text" });
    await waitFor(() => screen.getByText(/Import queued \(job job-1234/));
    expect(PROPS.onCommitted).toHaveBeenCalled();
    expect(refreshAssets).toHaveBeenCalled();
  });

  it("imports a picked file via bridge.import source_path", async () => {
    pickFile.mockResolvedValue("D:/exports/result.txt");
    render(createElement(AssistedPanel, PROPS));
    await select("sudowrite");
    fireEvent.click(await screen.findByTestId("assist-import-file"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalled());
    // the picker offers only plain-text formats — no binary containers
    const exts = pickFile.mock.calls[0][1][0].extensions as string[];
    expect(exts).not.toContain("docx");
    expect(exts).not.toContain("pdf");
    expect(exts).toContain("txt");
    expect(exts).toContain("md");
    expect(commitCommand.mock.calls[0][3]).toMatchObject(
      { provider_key: "sudowrite", source_path: "D:/exports/result.txt", kind: "text" });
  });

  it("ImportWatcher: scans the designated folder and imports per-file only on click", async () => {
    commitCommand.mockImplementation((_p: string, _r: number, ct: string) => {
      if (ct === "bridge.watch.scan")
        return Promise.resolve({ job_id: null, bridge: {
          action: "watch_scan", provider_key: "watchcfg",
          candidates: [
            { name: "out.txt", path: "D:/exports/out.txt", size: 5, mtime: 1, kind: "text" },
            { name: "essay.docx", path: "D:/exports/essay.docx", size: 9, mtime: 1, kind: "text" },
          ] } });
      return Promise.resolve({ job_id: "j9", bridge: null });
    });
    render(createElement(AssistedPanel, PROPS));
    await select("watchcfg");
    fireEvent.click(await screen.findByTestId("assist-watch-scan"));
    await waitFor(() => screen.getByText("out.txt"));
    // binary documents are not offered — the backend gate refuses them
    expect(screen.queryByText("essay.docx")).toBeNull();
    expect(screen.getByText(/binary document file\(s\) skipped/)).toBeTruthy();
    fireEvent.click(screen.getByText("Import"));
    await waitFor(() => expect(commitCommand.mock.calls.length).toBe(2));
    const p = commitCommand.mock.calls[1];
    expect(p[2]).toBe("bridge.import");
    expect(p[3]).toMatchObject({ source_path: "D:/exports/out.txt",
                                 from_watch: true, kind: "text" });
  });

  it("import-only providers show the manual path — no prepare/launch buttons", async () => {
    render(createElement(AssistedPanel, PROPS));
    await select("hemingway");
    expect(await screen.findByTestId("assist-import-only")).toBeTruthy();
    expect(screen.queryByTestId("assist-prepare")).toBeNull();
    expect(screen.queryByTestId("assist-open")).toBeNull();
    expect(screen.getByTestId("assist-import-paste")).toBeTruthy();
    // no bridge.prepare / bridge.launch dispatch is possible for a
    // T3-only provider — the only commands the panel may ever send are
    // import + watch.scan.
    const dispatched = commitCommand.mock.calls.map((c) => c[2]);
    expect(dispatched).not.toContain("bridge.prepare");
    expect(dispatched).not.toContain("bridge.launch");
  });

  it("surfaces commit errors without importing", async () => {
    commitCommand.mockRejectedValueOnce(new Error("422: content is required"));
    render(createElement(AssistedPanel, PROPS));
    await select("sudowrite");
    fireEvent.change(await screen.findByTestId("assist-paste"), { target: { value: "x" } });
    fireEvent.click(screen.getByTestId("assist-import-paste"));
    await waitFor(() => screen.getByText(/422: content is required/));
    expect(PROPS.onCommitted).not.toHaveBeenCalled();
  });
});
