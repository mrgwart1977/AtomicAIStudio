// U-2b-1 — WS job.progress payloads carry adapter_id, not the DB `kind`
// field; the frontend must map ops.model.import correctly so JobsPanel
// and Model Manager agree on the job's identity.
import { describe, expect, it } from "vitest";
import { jobKindFor } from "./App";

describe("jobKindFor — WS adapter_id → UI kind mapping", () => {
  it("maps ops model jobs to their UI kinds", () => {
    expect(jobKindFor("ops.model.download")).toBe("model_download");
    expect(jobKindFor("ops.model.benchmark")).toBe("model_benchmark");
    expect(jobKindFor("ops.model.import")).toBe("model_import");
  });

  it("everything else stays the generic image kind", () => {
    expect(jobKindFor("gen.image.sdcpp")).toBe("image");
    expect(jobKindFor("cloud.image.txt2img.sim")).toBe("image");
    expect(jobKindFor("")).toBe("image");
  });
});
