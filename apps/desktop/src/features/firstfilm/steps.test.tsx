// C5-D — MediaStep: native-picker behaviour (incl. cancellation),
// existing-asset selection, and in-flight import cancellation.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MediaStep } from "./steps";
import { initialState } from "./wizard";

const pickFile = vi.fn();
vi.mock("../../api/commands", () => ({
  pickFile: (...a: unknown[]) => pickFile(...a),
  previewCommand: vi.fn(),
}));

const state = () => ({
  ...initialState(),
  shot_ids: ["sh1"],
  shot_assets: {},
});
const shots = new Map([["sh1", { title: "Wide", prompt: "", sceneId: "s1" }]]);
const videoAsset = { id: "a1", name: "clip.mp4", kind: "video", status: "ready",
  media_json: { probe: { ok: true, fps: 24, duration_s: 4 } } } as const;

describe("MediaStep", () => {
  beforeEach(() => { cleanup(); pickFile.mockReset(); });

  it("native picker fills the path; cancel leaves it empty", async () => {
    pickFile.mockResolvedValue("D:\\Videos\\take1.mp4");
    render(<MediaStep state={state()} shotsById={shots} assets={[]} busy={null}
      onImport={vi.fn()} onPickAsset={vi.fn()} />);
    fireEvent.click(screen.getByText("Browse…"));
    await screen.findByDisplayValue(/take1\.mp4/);
    // cancelled picker → path unchanged, nothing imported
    pickFile.mockResolvedValue(null);
    fireEvent.change(screen.getByLabelText(/File path for Wide/), { target: { value: "" } });
    fireEvent.click(screen.getByText("Browse…"));
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.getByLabelText(/File path for Wide/)).toHaveProperty("value", "");
  });

  it("existing project clip can fill a shot without a path", () => {
    const onPick = vi.fn();
    render(<MediaStep state={state()} shotsById={shots}
      assets={[videoAsset as unknown as Parameters<typeof MediaStep>[0]["assets"][number]]}
      busy={null} onImport={vi.fn()} onPickAsset={onPick} />);
    fireEvent.change(screen.getByLabelText(/Use existing clip/), { target: { value: "a1" } });
    expect(onPick).toHaveBeenCalledWith("sh1", "a1");
  });

  it("shows progress + a Cancel button while an import job runs", () => {
    const onCancel = vi.fn();
    render(<MediaStep state={state()} shotsById={shots} assets={[]}
      busy="imp-sh1" importJobs={{ sh1: { jobId: "j9", message: "Probing media…" } }}
      onCancel={onCancel} onImport={vi.fn()} onPickAsset={vi.fn()} />);
    expect(screen.getByText(/Probing media/)).toBeTruthy();
    fireEvent.click(screen.getByText("Cancel"));
    expect(onCancel).toHaveBeenCalledWith("sh1");
  });

  it("attached shots stop offering the import UI", () => {
    const s = { ...state(), shot_assets: { sh1: "a1" } };
    render(<MediaStep state={s} shotsById={shots}
      assets={[videoAsset as unknown as Parameters<typeof MediaStep>[0]["assets"][number]]}
      busy={null} onImport={vi.fn()} onPickAsset={vi.fn()} />);
    expect(screen.getByText(/Media attached/)).toBeTruthy();
    expect(screen.queryByText("Import")).toBeNull();
  });

  it("unprobeable videos are never offered as existing clips", () => {
    const bad = { ...videoAsset, id: "a9", name: "fake.mp4",
      media_json: { probe: { ok: false, reason: "invalid data" } } };
    const onPick = vi.fn();
    render(<MediaStep state={state()} shotsById={shots}
      assets={[videoAsset, bad] as unknown as Parameters<typeof MediaStep>[0]["assets"]}
      busy={null} onImport={vi.fn()} onPickAsset={onPick} />);
    const sel = screen.getByLabelText(/Use existing clip/);
    // Only the probe-ok clip is selectable; the unreadable one is
    // excluded and its presence is surfaced as a note.
    expect(sel.innerHTML).toContain("clip.mp4");
    expect(sel.innerHTML).not.toContain("fake.mp4");
    expect(screen.getByText(/couldn't be read as video/)).toBeTruthy();
  });
});
