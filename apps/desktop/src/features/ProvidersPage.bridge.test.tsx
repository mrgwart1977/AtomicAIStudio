// C1.5 assisted-workflow UI tests — capability display honesty, staleness
// warnings, exact-destination display, and the mocked end-to-end T2 flow.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { ProvidersPage } from "./ProvidersPage";

// ---- mocks -------------------------------------------------------------
const apiFetch = vi.fn();
const commitCommand = vi.fn();
const pickFile = vi.fn();
const openUrl = vi.fn();
const writeText = vi.fn();
const readText = vi.fn();

vi.mock("../api/client", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));
const pickDirectory = vi.fn();
vi.mock("../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
  pickFile: (...a: unknown[]) => pickFile(...a),
  pickDirectory: (...a: unknown[]) => pickDirectory(...a),
  previewCommand: vi.fn(),
}));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: (...a: unknown[]) => openUrl(...a) }));

// Switchable store fixture — per-test project presence.
const store: Record<string, unknown> = {
  currentProjectId: "p1",
  projects: [{ id: "p1", revision: 3 }],
};
vi.mock("../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

const SUMMARY = {
  key: "testprov", label: "TestProv", category: "image", account: "required",
  status: "assisted", status_label: "Assisted", tier: "T2",
  method: "assisted_workflow", access_class: "C", enabled: true,
  credential_configured: false, credential_unreadable: false,
  capabilities: ["image_generation"], is_mock_only: false, status_reason: "",
};

// Mutable per-test — watch tests flip import_dir on/off.
const detailSettings: Record<string, unknown> = {};

const DETAIL = {
  ...SUMMARY,
  auth_methods: [], credential_fields: [], base_url: null,
  region_selectable: false, job_model: null, pricing_model: null,
  strategies: [{
    method: "assisted_workflow", tier: "T2",
    capabilities: ["image_generation"], automation_permission: "user_driven",
    user_interaction: "manual", note: "",
  }],
  models: [], docs_url: "https://testprov.example/docs", tos_url: null,
  privacy_summary: "", notes: "", aliases: [], shared_credential_with: [],
  settings: detailSettings, region: null,
  credential: { configured: false, fields: [], set_at: null, readable: false, unreadable_reason: null },
};

const CAPS = {
  provider_key: "testprov", evidence_stamp: "CATALOG_VERIFIED 2026-02-01",
  provider_status: "ok", enabled: true,
  capabilities: [
    {
      capability: "image_generation", strategy_id: "s1", method: "assisted_workflow",
      tier: "T2", status: "documented", stale: false,
      verification: "catalog_documented", dispatch: ["prepare", "launch", "import"],
      dispatch_blocked: null, required_user_action: "site_workflow", alternate: null,
    },
    {
      capability: "video_generation", strategy_id: "s2", method: "official_api",
      tier: "T1", status: "needs_recheck", stale: true,
      verification: "catalog_documented", dispatch: [],
      dispatch_blocked: "stale", required_user_action: "reverify_strategy", alternate: null,
    },
  ],
};

// Mutable per-test — documented CLI surface (null = no documented CLI).
let detailCli: unknown = null;

// Mutable per-test — watch polling status fixture.
const watchFixture = {
  enabled: false, status: "off" as string, reason: null as string | null,
  folder_name: null as string | null, candidates: [] as unknown[],
  last_poll: null as string | null, paused_reason: null as string | null,
};

function routedFetch(url: string) {
  if (url.startsWith("/api/providers/defaults")) return { defaults: {} };
  if (url === "/api/providers/testprov/capabilities") return CAPS;
  if (url === "/api/providers/testprov") return { ...DETAIL, cli: detailCli };
  if (url.startsWith("/api/providers/testprov/watch")) return watchFixture;
  if (url.startsWith("/api/providers")) return [SUMMARY];
  if (url === "/api/projects/p1") return { id: "p1", revision: 3 };
  throw new Error(`unexpected fetch ${url}`);
}

beforeEach(() => {
  cleanup();
  vi.clearAllMocks();
  store.currentProjectId = "p1";
  store.projects = [{ id: "p1", revision: 3 }];
  apiFetch.mockImplementation(async (url: string) => routedFetch(url));
  commitCommand.mockImplementation(async (_pid: string, _rev: number, type: string) => {
    const base = {
      command_id: "c", status: "committed", revision: 4,
      idempotent_replay: false, job_id: null, warnings: [],
    };
    if (type === "bridge.prepare") return {
      ...base, bridge: {
        action: "prepare", provider_key: "testprov", prepared_text: "PREPARED TEXT",
        paste_hint: "Paste it yourself",
      },
    };
    if (type === "bridge.launch") return {
      ...base, bridge: {
        action: "launch", provider_key: "testprov", url: "https://testprov.example/app",
        open_via: "client_shell",
      },
    };
    if (type === "bridge.cli.detect") return {
      ...base, bridge: {
        action: "cli_detect", provider_key: "testprov", platform: "windows",
        status: "detected", skipped_untrusted: 0,
        candidates: [{ name: "fakecli", path: "C:/Tools/fakecli.exe", via: "PATH" }],
      },
    };
    if (type === "bridge.watch.scan") return {
      ...base, bridge: {
        action: "watch_scan", provider_key: "testprov", folder: "C:\\watch",
        truncated: false,
        candidates: [
          { name: "out1.png", path: "C:\\watch\\out1.png", size: 2048, mtime: 1, kind: "image" },
          { name: "out2.png", path: "C:\\watch\\out2.png", size: 1024, mtime: 0, kind: "image" },
        ],
      },
    };
    return { ...base, job_id: "j9", bridge: { action: "import", job_id: "j9", provider_key: "testprov" } };
  });
  pickFile.mockResolvedValue("C:\\out\\result.png");
  for (const k of Object.keys(detailSettings)) delete detailSettings[k];
  Object.assign(watchFixture, {
    enabled: false, status: "off", reason: null, folder_name: null,
    candidates: [], last_poll: null, paused_reason: null,
  });
  detailCli = null;
  writeText.mockResolvedValue(undefined);
  readText.mockResolvedValue("generated output text");
  (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
  Object.defineProperty(window.navigator, "clipboard", {
    value: { writeText, readText }, configurable: true,
  });
});

async function openProvider() {
  render(createElement(ProvidersPage));
  const row = await screen.findByText("TestProv");
  fireEvent.click(row);
  await screen.findByText("Capability report");
}

describe("ProvidersPage — capability report", () => {
  it("renders per-feature status honestly: documented ≠ verified, stale warned", async () => {
    await openProvider();
    expect(screen.getByText("Documented (unverified)")).toBeTruthy();
    expect(screen.getByText("Verification stale — re-check required")).toBeTruthy();
    expect(screen.getByText(/Verification is stale/)).toBeTruthy();
    // T1 stale row never claims connectivity
    expect(screen.queryByText(/[Cc]onnected|[Oo]perational/)).toBeNull();
    expect(screen.getByText(/Required action: re-verify/)).toBeTruthy();
  });
});

describe("ProvidersPage — assisted workflow (mocked end-to-end)", () => {
  it("prepare → copy → show destination → explicit open → import", async () => {
    await openProvider();

    // 1. prepare + copy (explicit click — clipboard write only in gesture)
    fireEvent.change(screen.getByPlaceholderText(/Prompt to take to/), {
      target: { value: "a castle at dusk" },
    });
    fireEvent.click(screen.getByText("Prepare & copy prompt"));
    await waitFor(() =>
      expect(commitCommand).toHaveBeenCalledWith(
        "p1", 3, "bridge.prepare",
        expect.objectContaining({ provider_key: "testprov", prompt: "a castle at dusk" })));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("PREPARED TEXT"));
    expect(screen.getByText("PREPARED TEXT")).toBeTruthy();

    // 2. launch — commit validates+audits; URL displayed, NOTHING opened yet
    fireEvent.click(screen.getByText("Get provider site link"));
    await screen.findByText("https://testprov.example/app");
    expect(commitCommand).toHaveBeenCalledWith(
      "p1", 3, "bridge.launch", { provider_key: "testprov" });
    expect(openUrl).not.toHaveBeenCalled(); // display-before-launch

    // 3. explicit second gesture opens the exact displayed destination
    fireEvent.click(screen.getByText("Open this site in my browser"));
    await waitFor(() => expect(openUrl).toHaveBeenCalledTimes(1));
    expect(openUrl).toHaveBeenCalledWith("https://testprov.example/app");

    // 4. file import via native picker — user-reported provenance message
    fireEvent.click(screen.getByText("Import file…"));
    await waitFor(() =>
      expect(commitCommand).toHaveBeenCalledWith(
        "p1", 3, "bridge.import",
        { provider_key: "testprov", source_path: "C:\\out\\result.png" }));
    await screen.findByText(/user-reported external/);

    // 5. clipboard import — readText only inside the click handler
    fireEvent.click(screen.getByText("Import clipboard text"));
    await waitFor(() =>
      expect(commitCommand).toHaveBeenCalledWith(
        "p1", 3, "bridge.import",
        { provider_key: "testprov", content: "generated output text" }));
  });

  it("blocks bridge actions without an open project — audited scope required", async () => {
    store.currentProjectId = null;
    store.projects = [];
    await openProvider();
    expect(screen.getByText(/Open a project to use assisted workflows/)).toBeTruthy();
    expect(screen.queryByText("Prepare & copy prompt")).toBeNull();
    expect(screen.queryByText("Get provider site link")).toBeNull();
  });
});

describe("ProvidersPage — ImportWatcher Stage A", () => {
  it("offers folder designation when none is set", async () => {
    await openProvider();
    pickDirectory.mockResolvedValue("C:\\watch");
    fireEvent.click(screen.getByText("Designate folder…"));
    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith("/api/providers/testprov/config",
        expect.objectContaining({
          method: "PUT",
          body: expect.stringContaining('"import_dir":"C:\\\\watch"'),
        })));
    expect(commitCommand).not.toHaveBeenCalled(); // designation alone scans nothing
  });

  it("manual check lists candidates; per-file click imports", async () => {
    detailSettings.import_dir = "C:\\watch";
    await openProvider();
    expect(screen.getByText("C:\\watch")).toBeTruthy();

    fireEvent.click(screen.getByText("Check folder"));
    await screen.findByText("out1.png");
    expect(commitCommand).toHaveBeenCalledWith(
      "p1", 3, "bridge.watch.scan", { provider_key: "testprov" });
    expect(screen.getByText(/does not verify these came from/)).toBeTruthy();

    // per-file explicit import — the only path from candidate to asset
    const btns = screen.getAllByText("Import");
    fireEvent.click(btns[0]);
    await waitFor(() =>
      expect(commitCommand).toHaveBeenCalledWith(
        "p1", 3, "bridge.import",
        expect.objectContaining({
          provider_key: "testprov", source_path: "C:\\watch\\out1.png",
          kind: "image", name: "out1.png",
        })));
    await screen.findByText(/provenance: user-reported external/);
    await waitFor(() => expect(screen.queryByText("out1.png")).toBeNull());
    expect(screen.getByText("out2.png")).toBeTruthy();
  });

  it("auto-check toggle sends audited bridge.watch.enable", async () => {
    detailSettings.import_dir = "C:\\watch";
    await openProvider();
    fireEvent.click(screen.getByText("Auto-check"));
    await waitFor(() =>
      expect(commitCommand).toHaveBeenCalledWith(
        "p1", 3, "bridge.watch.enable", { provider_key: "testprov" }));
  });

  it("enabled polling shows status + candidates; stop sends disable", async () => {
    detailSettings.import_dir = "C:\\watch";
    Object.assign(watchFixture, {
      enabled: true, status: "ok", folder_name: "watch",
      last_poll: "2026-01-01T00:00:00+00:00",
      candidates: [
        { name: "polled.png", path: "C:\\watch\\polled.png",
          size: 512, mtime: 1, kind: "image" },
      ],
    });
    await openProvider();
    await screen.findByText(/Auto-checking about once a minute/);
    await screen.findByText("polled.png");

    fireEvent.click(screen.getByText("Stop auto-check"));
    await waitFor(() =>
      expect(commitCommand).toHaveBeenCalledWith(
        "p1", 3, "bridge.watch.disable", { provider_key: "testprov" }));
  });

  it("auto-refresh polls watch status ~12s while enabled — never scans", async () => {
    detailSettings.import_dir = "C:\\watch";
    Object.assign(watchFixture, { enabled: true, status: "ok" });
    const spy = vi.spyOn(globalThis, "setInterval");
    await openProvider();
    // exactly one refresh interval in the 10–15s band once auto-check is on
    const ticks = spy.mock.calls
      .map((c) => ({ cb: c[0] as () => void, ms: c[1] as number }))
      .filter((c) => c.ms >= 10000 && c.ms <= 15000);
    expect(ticks.length).toBe(1);
    apiFetch.mockClear();
    await act(async () => { ticks[0].cb(); });
    const gets = apiFetch.mock.calls.filter(
      (c) => String(c[0]).includes("/watch"));
    expect(gets.length).toBe(1);
    // read-only GET — the refresh must never trigger a scan command
    expect(commitCommand).not.toHaveBeenCalledWith(
      "p1", 3, "bridge.watch.scan", expect.anything());
    spy.mockRestore();
  });

  it("no background refresh while auto-check is off", async () => {
    detailSettings.import_dir = "C:\\watch";
    const spy = vi.spyOn(globalThis, "setInterval");
    await openProvider();
    const ticks = spy.mock.calls.filter(
      (c) => typeof c[1] === "number" && c[1] >= 10000 && c[1] <= 15000);
    expect(ticks.length).toBe(0);
    spy.mockRestore();
  });

  it("degraded folder surfaces the reason honestly", async () => {
    detailSettings.import_dir = "C:\\watch";
    Object.assign(watchFixture, {
      enabled: true, status: "degraded",
      reason: "designated folder is unavailable or no longer valid",
    });
    await openProvider();
    await screen.findByText(/Auto-check paused/);
    expect(screen.getByText(/unavailable or no longer valid/)).toBeTruthy();
  });
});

describe("C1.5 §5 — local CLI detection", () => {
  it("shows Local CLI block only when the catalog documents one", async () => {
    await openProvider();
    expect(screen.queryByText("Local CLI")).toBeNull();
    cleanup();
    detailCli = {
      executables: ["fakecli"], platforms: ["windows"],
      install_hint_dirs: [], docs_url: "https://x.example/cli",
      note: "test cli",
    };
    await openProvider();
    expect(screen.getByText("Local CLI")).toBeTruthy();
    expect(screen.getByText(/fakecli/)).toBeTruthy();
  });

  it("check installation commits bridge.cli.detect and shows unverified result", async () => {
    detailCli = {
      executables: ["fakecli"], platforms: ["windows"],
      install_hint_dirs: [], docs_url: "https://x.example/cli",
      note: "test cli",
    };
    await openProvider();
    fireEvent.click(screen.getByText("Check installation"));
    await waitFor(() =>
      expect(commitCommand).toHaveBeenCalledWith(
        "p1", 3, "bridge.cli.detect", { provider_key: "testprov" }));
    await screen.findByText(/fakecli\.exe/);
    expect(screen.getByText(/not proof this is the genuine provider tool/)).toBeTruthy();
  });
});
