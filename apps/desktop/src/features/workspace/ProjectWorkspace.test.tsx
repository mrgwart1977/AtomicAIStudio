// C3-B review-hardening — the 10-tab workspace nav must remain fully
// reachable at the 1100px minimum width: every tab is a real button
// (keyboard-focusable, screen-reader friendly) inside a horizontally
// scrollable nav — nothing is dropped, hidden, or collapsed into a
// hamburger that hides context.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { ProjectWorkspace } from "./ProjectWorkspace";

const setWorkspaceTab = vi.fn();
const setPage = vi.fn();
const setCurrentProject = vi.fn();

// Stub every tab page — the nav contract is what's under test.
vi.mock("./AssetsPage", () => ({ AssetsPage: () => null }));
vi.mock("./BackupsPage", () => ({ BackupsPage: () => null }));
vi.mock("./EntityPage", () => ({ EntityPage: () => null }));
vi.mock("./CloudPage", () => ({ CloudPage: () => null }));
vi.mock("./GeneratePage", () => ({ GeneratePage: () => null }));
vi.mock("./OverviewPage", () => ({ OverviewPage: () => null }));
vi.mock("./SettingsPage", () => ({ SettingsPage: () => null }));
vi.mock("./StoryboardPage", () => ({ StoryboardPage: () => null }));

const store: Record<string, unknown> = {
  workspaceTab: "cloud",
  currentProject: () => ({
    id: "p1", name: "A Very Long Project Name For Narrow Windows",
    kind: "movie", aspect_ratio: "16:9", fps_num: 24, fps_den: 1,
    status: "active",
  }),
  setWorkspaceTab, setPage, setCurrentProject,
};
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel(store),
}));

const TAB_LABELS = [
  "Overview", "Storyboard", "Generate", "Cloud", "Cast", "Worlds",
  "Locations", "Assets", "Backups", "Settings",
];

describe("ProjectWorkspace — tab navigation at minimum width", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    store.workspaceTab = "cloud";
  });

  it("renders all ten tabs as real, focusable buttons", () => {
    render(createElement(ProjectWorkspace));
    const nav = screen.getByRole("navigation", { name: "Workspace" });
    for (const label of TAB_LABELS) {
      const btn = screen.getByRole("button", { name: label });
      expect(btn.tagName).toBe("BUTTON"); // natively keyboard-focusable
      expect((btn as HTMLButtonElement).disabled).not.toBe(true);
      expect(nav.contains(btn)).toBe(true);
    }
  });

  it("scrolls horizontally instead of overflowing the header", () => {
    render(createElement(ProjectWorkspace));
    const nav = screen.getByRole("navigation", { name: "Workspace" });
    // The contract: min-w-0 lets the nav shrink inside the flex header;
    // overflow-x-auto scrolls the excess — focusing an off-screen tab
    // auto-scrolls it into view (native behavior, no JS needed).
    expect(nav.className).toContain("min-w-0");
    expect(nav.className).toContain("overflow-x-auto");
    // tabs keep their intrinsic width — never compressed illegibly
    for (const label of TAB_LABELS) {
      const btn = screen.getByRole("button", { name: label });
      expect(btn.className).toContain("shrink-0");
      expect(btn.className).toContain("whitespace-nowrap");
    }
  });

  it("switches tabs on click", () => {
    render(createElement(ProjectWorkspace));
    fireEvent.click(screen.getByRole("button", { name: "Assets" }));
    expect(setWorkspaceTab).toHaveBeenCalledWith("assets");
    fireEvent.click(screen.getByRole("button", { name: "Overview" }));
    expect(setWorkspaceTab).toHaveBeenCalledWith("overview");
  });

  it("exposes the active tab via aria-current", () => {
    render(createElement(ProjectWorkspace));
    expect(screen.getByRole("button", { name: "Cloud" })
      .getAttribute("aria-current")).toBe("page");
    expect(screen.getByRole("button", { name: "Cast" })
      .getAttribute("aria-current")).toBeNull();
  });

  it("project name truncates instead of pushing the header wide", () => {
    render(createElement(ProjectWorkspace));
    const h1 = screen.getByRole("heading", {
      name: "A Very Long Project Name For Narrow Windows" });
    expect(h1.className).toContain("truncate");
    expect(h1.parentElement!.className).toContain("min-w-0");
  });
});
