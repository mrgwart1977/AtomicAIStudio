// SEC-A1 — AuthImage: asset bytes arrive via header-authenticated fetch
// and a managed blob object URL. The session token must never appear in
// any element's src; stale responses must not overwrite newer images;
// object URLs must be revoked on change/unmount (bounded retention);
// failures must degrade to the caller's fallback.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { AuthImage, useAssetObjectUrl } from "./AuthImage";

const fetchAssetObjectUrl = vi.fn();
vi.mock("../api/client", () => ({
  fetchAssetObjectUrl: (...a: unknown[]) => fetchAssetObjectUrl(...a),
}));

function Thumb({ pid, assetId }: { pid: string; assetId: string | null }) {
  return (
    <AuthImage
      pid={pid}
      assetId={assetId}
      alt="thumb"
      className="cls"
      fallback={<div>FB</div>}
    />
  );
}

describe("AuthImage", () => {
  beforeEach(() => {
    cleanup();
    vi.restoreAllMocks();
    fetchAssetObjectUrl.mockReset();
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  });

  it("fetches with the asset path (header auth) and renders the blob URL", async () => {
    fetchAssetObjectUrl.mockResolvedValue("blob:obj-1");
    render(<Thumb pid="p1" assetId="a1" />);
    const img = await screen.findByAltText("thumb");
    expect(fetchAssetObjectUrl).toHaveBeenCalledWith("p1", "a1");
    expect(img.getAttribute("src")).toBe("blob:obj-1");
    expect(img.getAttribute("src")).not.toContain("token=");
    expect(img.className).toBe("cls");
  });

  it("renders the fallback while loading and on failure", async () => {
    let resolve!: (v: string) => void;
    fetchAssetObjectUrl.mockReturnValue(new Promise<string>((r) => (resolve = r)));
    render(<Thumb pid="p1" assetId="a1" />);
    expect(screen.getByText("FB")).toBeTruthy();
    resolve("blob:late");
    await screen.findByAltText("thumb");
    cleanup();

    fetchAssetObjectUrl.mockRejectedValue(new Error("404"));
    render(<Thumb pid="p1" assetId="a1" />);
    await waitFor(() => expect(screen.getByText("FB")).toBeTruthy());
    expect(screen.queryByAltText("thumb")).toBeNull();
  });

  it("renders the fallback (and never fetches) when assetId is null", () => {
    render(<Thumb pid="p1" assetId={null} />);
    expect(screen.getByText("FB")).toBeTruthy();
    expect(fetchAssetObjectUrl).not.toHaveBeenCalled();
  });

  it("revokes the object URL on unmount", async () => {
    fetchAssetObjectUrl.mockResolvedValue("blob:one");
    const { unmount } = render(<Thumb pid="p1" assetId="a1" />);
    await screen.findByAltText("thumb");
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:one");
  });

  it("revokes the previous URL when the asset changes", async () => {
    fetchAssetObjectUrl.mockResolvedValueOnce("blob:first").mockResolvedValueOnce("blob:second");
    const { rerender } = render(<Thumb pid="p1" assetId="a1" />);
    await screen.findByAltText("thumb");
    rerender(<Thumb pid="p1" assetId="a2" />);
    await waitFor(() =>
      expect(screen.getByAltText("thumb").getAttribute("src")).toBe("blob:second"),
    );
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:first");
  });

  it("a stale response is revoked and cannot overwrite the newer image", async () => {
    let resolveOld!: (v: string) => void;
    fetchAssetObjectUrl
      .mockReturnValueOnce(new Promise<string>((r) => (resolveOld = r)))
      .mockResolvedValueOnce("blob:new");
    const { rerender } = render(<Thumb pid="p1" assetId="a1" />);
    rerender(<Thumb pid="p1" assetId="a2" />);
    await waitFor(() =>
      expect(screen.getByAltText("thumb").getAttribute("src")).toBe("blob:new"),
    );
    // the old fetch resolves after the asset already changed
    resolveOld("blob:stale");
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:stale"));
    expect(screen.getByAltText("thumb").getAttribute("src")).toBe("blob:new");
  });
});

describe("useAssetObjectUrl", () => {
  it("does not re-fetch when unrelated props re-render", async () => {
    fetchAssetObjectUrl.mockResolvedValue("blob:stable");
    function Probe({ n }: { n: number }) {
      const url = useAssetObjectUrl("p9", "a9");
      return <div data-n={n}>{url ?? "loading"}</div>;
    }
    const { rerender } = render(<Probe n={1} />);
    await waitFor(() =>
      expect(
        fetchAssetObjectUrl.mock.calls.filter(([p, a]) => p === "p9" && a === "a9"),
      ).toHaveLength(1),
    );
    rerender(<Probe n={2} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(
      fetchAssetObjectUrl.mock.calls.filter(([p, a]) => p === "p9" && a === "a9"),
    ).toHaveLength(1);
  });
});
