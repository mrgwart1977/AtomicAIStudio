// C4.4 accessibility regressions — verified defects fixed in this pass:
//   1. RenameDialog's name field had NO accessible name (bare input).
//   2. ProvidersPage search box, per-task default selects, and the
//      assisted-workflow textarea relied on placeholder/sibling text —
//      placeholders are not accessible names (WCAG 4.1.2).
//   3. Page-level error blocks appeared silently — no role="alert", so
//      screen readers never announced them (WCAG 4.1.3).
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { RenameDialog } from "./components/RenameDialog";
import { PromptBar } from "./components/PromptBar";
import { ProvidersPage } from "./features/ProvidersPage";

const apiFetch = vi.fn();
vi.mock("./api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
  fetchAssetObjectUrl: vi.fn(async () => null),
}));
vi.mock("./api/commands", () => ({
  commitCommand: vi.fn(),
  pickFile: vi.fn(),
  pickDirectory: vi.fn(),
  previewCommand: vi.fn(),
}));
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: vi.fn() }));

const store: Record<string, unknown> = {
  currentProjectId: "p1",
  projects: [{ id: "p1", revision: 3, status: "active", name: "P" }],
  promptDraft: "a boat",
  setPromptDraft: vi.fn(),
  refreshProjects: vi.fn(async () => {}),
  refreshProjectData: vi.fn(async () => {}),
};
vi.mock("./stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

beforeEach(() => {
  cleanup();
  vi.clearAllMocks();
  apiFetch.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/providers/defaults")) return { defaults: {} };
    if (url.startsWith("/api/providers")) return [];
    throw new Error(`unexpected fetch ${url}`);
  });
});

describe("a11y — accessible names (WCAG 4.1.2)", () => {
  it("RenameDialog input is named by the dialog title", () => {
    render(createElement(RenameDialog, {
      open: true, onOpenChange: () => {}, title: "Rename project",
      initialValue: "Old name", onSubmit: () => {},
    }));
    expect(screen.getByRole("textbox", { name: "Rename project" })).toBeTruthy();
  });

  it("ProvidersPage search and default-provider selects are named", async () => {
    render(createElement(ProvidersPage));
    expect(screen.getByRole("textbox", { name: "Search providers" })).toBeTruthy();
    for (const task of ["text", "image", "video", "audio"])
      expect(
        screen.getByRole("combobox", { name: `${task} default provider` })
      ).toBeTruthy();
  });
});

describe("a11y — error announcements (WCAG 4.1.3)", () => {
  it("PromptBar surfaces a failed review via role=alert", async () => {
    apiFetch.mockRejectedValueOnce(new Error("backend is down"));
    render(createElement(PromptBar));
    fireEvent.click(screen.getByRole("button", { name: "Review request" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("backend is down");
  });
});

describe("a11y — regression guards", () => {
  it("dialog inputs remain reachable by name, not placeholder", () => {
    render(createElement(RenameDialog, {
      open: true, onOpenChange: () => {}, title: "Rename scene",
      initialValue: "x", onSubmit: () => {},
    }));
    // A bare <input> with only a placeholder would fail this query.
    expect(screen.queryByRole("textbox")).toBeTruthy();
    expect(screen.getByRole("textbox").getAttribute("aria-label")).toBe("Rename scene");
  });
});
