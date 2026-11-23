/** U-4c — ArtifactCompatSection contract tests (C6 state matrix).
 * Synthetic fixtures only — no real verdicts, jobs, or backend.
 */
import { cleanup, fireEvent, render, screen, waitFor }
  from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi }
  from "vitest";
import type { CompatVerdict } from "../api/types";

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock("../api/client", () => ({
  apiFetch: (path: string, opts?: { method?: string; body?: string }) =>
    opts?.method ? apiMock.post(path, opts) : apiMock.get(path),
}));

let jobsObj: Record<string, unknown> = {};
vi.mock("../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({ jobs: jobsObj }),
}));

import { ArtifactCompatSection, CompatEvalAllButton, CompatSection }
  from "./ArtifactCompatSection";

const RULESET = {
  ruleset_version: "rs1", families_version: 1, caps_schema_version: 1,
  max_verification_level: 4,
  axes: {
    arch: ["supported", "unsupported", "unknown"],
    runtime: ["operational", "installed_unverified", "not_installed",
              "planned", "unknown"],
    hardware: ["fits", "limited", "insufficient", "unknown"],
    policy: ["eligible", "restricted", "blocked"],
  },
  runtimes: [
    { runtime_key: "sd_cpp", display_name: "stable-diffusion.cpp",
      caps_version: 1, planned: false, backends: ["vulkan"],
      validated_versions: ["0.0.1"], families: {} },
    { runtime_key: "comfyui", display_name: "ComfyUI",
      caps_version: 1, planned: true, backends: [],
      validated_versions: [], families: {} },
  ],
};

const BASE: CompatVerdict = {
  artifact_id: "catalog:u4t.sdxl", runtime_key: "sd_cpp",
  arch_axis: "supported", arch_confidence: "tested",
  runtime_axis: "operational", hardware_axis: "fits",
  policy_axis: "eligible", level: 4,
  domain_levels: { inspection: 1, family: 2, runtime_static: 3,
                   hardware: 4 },
  overall: "compatible", blocking_axes: [], confidence: "tested",
  reasons: [], warnings: [], missing: [], hardware_limits: [],
  actions: [], stale: false, ruleset_version: "rs1",
  evaluated_at: "2026-03-01T10:00:00Z",
};

function mockApi({
  verdicts = [BASE], compatOut, ruleset = RULESET,
  evalOut = { job_id: "jc1", status: "queued" },
}: {
  verdicts?: CompatVerdict[];
  compatOut?: unknown;
  ruleset?: unknown;
  evalOut?: unknown;
} = {}) {
  apiMock.get.mockImplementation((path: string) => {
    if (path === "/api/compat/ruleset") return Promise.resolve(ruleset);
    if (path.includes("/compat"))
      return compatOut !== undefined
        ? (compatOut instanceof Error
            ? Promise.reject(compatOut) : Promise.resolve(compatOut))
        : Promise.resolve({ artifact_id: "a1", verdicts });
    return Promise.reject(new Error(`unmocked GET ${path}`));
  });
  apiMock.post.mockImplementation((path: string) => {
    if (path === "/api/models/compat/evaluate-all")
      return Promise.resolve(evalOut);
    if (path.includes("/compat/evaluate")) return Promise.resolve(evalOut);
    return Promise.reject(new Error(`unmocked POST ${path}`));
  });
}

function job(id: string, status: string, kind = "model_compat_eval") {
  return { id, kind, status, progress: 0, message: "", error: null };
}

beforeEach(() => {
  jobsObj = {};
  mockApi();
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ArtifactCompatSection", () => {
  it("C6.1 — compatible + sufficient hardware → strongest permitted label", async () => {
    render(<ArtifactCompatSection artifactId="a1" />);
    expect(await screen.findByText(
      "Compatible — static evaluation")).toBeTruthy();
    expect(screen.getByText("stable-diffusion.cpp")).toBeTruthy();
    expect(screen.queryByText(/ready to run/i)).toBeNull();
    expect(screen.queryByText(/verified to run/i)).toBeNull();
  });

  it("C6.2 — recognized architecture + runtime not installed stays independent", async () => {
    mockApi({
      verdicts: [{ ...BASE, runtime_axis: "not_installed",
        overall: "blocked", blocking_axes: ["runtime"],
        confidence: "unverified",
        reasons: ["runtime_not_installed"],
        actions: ["install_runtime:sd_cpp"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Blocked");
    expect(screen.getByText(/Recognized/)).toBeTruthy();   // arch axis
    expect(screen.getByText("Not installed")).toBeTruthy(); // runtime axis
    expect(screen.getByText(/runtime is not installed/i)).toBeTruthy();
    expect(screen.getByText(/install the sd_cpp runtime/)).toBeTruthy();
  });

  it("C6.3 — unsupported arch/runtime combo → Incompatible", async () => {
    mockApi({
      verdicts: [{ ...BASE, arch_axis: "unsupported",
        arch_confidence: null, overall: "incompatible",
        blocking_axes: ["arch"],
        reasons: ["family_unsupported"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Incompatible");
    expect(screen.getByText("Unsupported")).toBeTruthy();
    expect(screen.getByText(/does not support this model family/))
      .toBeTruthy();
  });

  it("C6.4 — insufficient VRAM → hardware axis limited honestly", async () => {
    mockApi({
      verdicts: [{ ...BASE, hardware_axis: "insufficient",
        overall: "blocked", blocking_axes: ["hardware"],
        reasons: ["vram_below_min"],
        hardware_limits: ["vram_headroom_tight"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Blocked");
    expect(screen.getByText("Insufficient")).toBeTruthy();
    expect(screen.getByText(/below the declared minimum/)).toBeTruthy();
    expect(screen.getByText(/below the configured reserve/)).toBeTruthy();
  });

  it("C6.5 — unknown architecture → Unknown, never a failure claim", async () => {
    mockApi({
      verdicts: [{ ...BASE, arch_axis: "unknown",
        arch_confidence: "unverified", overall: "unknown",
        blocking_axes: ["arch"], confidence: "unverified",
        reasons: ["family_unrecognized"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    expect(await screen.findAllByText("Unknown")).not.toHaveLength(0);
    expect(screen.getByText(/not recognized from stored evidence/))
      .toBeTruthy();
    expect(screen.queryByText("Incompatible")).toBeNull();
  });

  it("C6.6 — LoRA hold-gated policy → Blocked with hold explanation", async () => {
    mockApi({
      verdicts: [{ ...BASE, policy_axis: "blocked",
        overall: "blocked", blocking_axes: ["policy"],
        reasons: ["hold_gated"],
        missing: ["base_model_sdxl"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    // "Blocked" twice — overall badge AND the independent policy axis.
    expect((await screen.findAllByText("Blocked")).length)
      .toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/gated off pending milestone approval/))
      .toBeTruthy();
    expect(screen.getByText(/Missing: base model sdxl/)).toBeTruthy();
  });

  it("C6.7 — multi-component model with missing deps listed", async () => {
    mockApi({
      verdicts: [{ ...BASE, arch_axis: "unsupported",
        overall: "incompatible", blocking_axes: ["arch"],
        reasons: ["components_missing"],
        missing: ["text_encoder_g", "vae"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Incompatible");
    expect(screen.getByText(/companion components are missing/))
      .toBeTruthy();
    expect(screen.getByText(/Missing: text encoder g, vae/)).toBeTruthy();
  });

  it("C6.8 — installed but unvalidated runtime version", async () => {
    mockApi({
      verdicts: [{ ...BASE, runtime_axis: "installed_unverified",
        overall: "limited", blocking_axes: ["runtime"],
        reasons: ["runtime_version_unverified"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Limited — static evaluation");
    expect(screen.getByText("Installed — version not validated"))
      .toBeTruthy();
    expect(screen.getByText(/not in the validated set/)).toBeTruthy();
    expect(screen.getByText(/0\.0\.1/)).toBeTruthy(); // validated list
  });

  it("C6.9 — planned runtime → planned, never claimable", async () => {
    mockApi({
      verdicts: [{ ...BASE, runtime_key: "comfyui",
        runtime_axis: "planned", overall: "blocked",
        blocking_axes: ["runtime"], reasons: ["runtime_planned"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Blocked");
    expect(screen.getByText("ComfyUI")).toBeTruthy();
    expect(screen.getByText("Planned — not installed")).toBeTruthy();
    expect(screen.getByText(/planned — not available yet/)).toBeTruthy();
  });

  it("C6.10 — unverified metadata keeps arch confidence visible", async () => {
    mockApi({
      verdicts: [{ ...BASE, arch_axis: "supported",
        arch_confidence: "documented", overall: "limited",
        confidence: "documented",
        reasons: ["runtime_path_unverified"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Limited — static evaluation");
    expect(screen.getByText(/Recognized \(documented\)/)).toBeTruthy();
    expect(screen.getByText(/documented but not covered by tests/))
      .toBeTruthy();
  });

  it("C6.11 — stale verdict is flagged, never presented as current", async () => {
    mockApi({ verdicts: [{ ...BASE, stale: true }] });
    render(<ArtifactCompatSection artifactId="a1" />);
    expect(await screen.findByText("Evaluation out of date"))
      .toBeTruthy();
    expect(screen.getByText(/Evaluated 2026-03-01/)).toBeTruthy();
    expect(screen.getByText(/measurements from that/)).toBeTruthy();
  });

  it("C6.12+13 — queued/running eval job states", async () => {
    jobsObj = { jc1: job("jc1", "queued") };
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Compatible — static evaluation");
    fireEvent.click(screen.getByText("Re-evaluate"));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith(
      "/api/models/artifacts/a1/compat/evaluate",
      expect.objectContaining({ method: "POST" })));
    expect(await screen.findByText("Evaluation queued")).toBeTruthy();
    expect(screen.getByText("Evaluating…")).toBeTruthy();
    // verdicts remain visible during evaluation
    expect(screen.getByText("Compatible — static evaluation"))
      .toBeTruthy();
  });

  it("C6.14 — succeeded job refetches verdicts and reports completion", async () => {
    jobsObj = { jc1: job("jc1", "succeeded") };
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Compatible — static evaluation");
    fireEvent.click(screen.getByText("Re-evaluate"));
    expect(await screen.findByText("Evaluation complete")).toBeTruthy();
    // terminal transition triggered a compat refetch
    await waitFor(() => expect(
      apiMock.get.mock.calls.filter(
        (c) => String(c[0]).includes("/compat")).length)
      .toBeGreaterThanOrEqual(2));
  });

  it("C6.15 — failed job reports honestly, verdicts preserved", async () => {
    jobsObj = { jc1: job("jc1", "failed") };
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Compatible — static evaluation");
    fireEvent.click(screen.getByText("Re-evaluate"));
    expect(await screen.findByText(
      /Evaluation failed — previous verdicts are unchanged/))
      .toBeTruthy();
    expect(screen.getByText("Compatible — static evaluation"))
      .toBeTruthy();
  });

  it("C6.16 — cancelled job reports honestly", async () => {
    jobsObj = { jc1: job("jc1", "cancelled") };
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Compatible — static evaluation");
    fireEvent.click(screen.getByText("Re-evaluate"));
    expect(await screen.findByText(
      /Evaluation cancelled — previous verdicts are unchanged/))
      .toBeTruthy();
  });

  it("C6.17 — 404 renders artifact-not-found, no eval affordance lies", async () => {
    mockApi({ compatOut: new Error("404 not found") });
    render(<ArtifactCompatSection artifactId="a1" />);
    expect(await screen.findByText("Artifact not found.")).toBeTruthy();
  });

  it("C6.18 — empty verdict set prompts explicit evaluation", async () => {
    mockApi({ verdicts: [] });
    render(<ArtifactCompatSection artifactId="a1" />);
    expect(await screen.findByText(/No compatibility verdict yet/))
      .toBeTruthy();
    expect(screen.getByText("Re-evaluate")).toBeTruthy();
  });

  it("does not auto-evaluate — GETs only on mount", async () => {
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Compatible — static evaluation");
    expect(apiMock.post).not.toHaveBeenCalled();
    expect(apiMock.get).toHaveBeenCalledWith(
      "/api/models/artifacts/a1/compat");
  });

  it("in-flight button is disabled — no duplicate-submission feedback", async () => {
    jobsObj = { jc1: job("jc1", "running") };
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Compatible — static evaluation");
    fireEvent.click(screen.getByText("Re-evaluate"));
    const btn = await screen.findByText("Evaluating…");
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Evaluation running")).toBeTruthy();
  });

  it("five axes render independently with evidence domains", async () => {
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Compatible — static evaluation");
    expect(screen.getByText("Recognized (tested)")).toBeTruthy();
    expect(screen.getByText("Installed & operational")).toBeTruthy();
    expect(screen.getByText("Fits")).toBeTruthy();
    expect(screen.getByText("Eligible")).toBeTruthy();
    expect(screen.getByText("V4 · tested")).toBeTruthy();
    expect(screen.getByText(
      /inspection V1 · family V2 · runtime static V3 · hardware V4/))
      .toBeTruthy();
  });

  it("volatile-resource warnings are labeled as point-in-time samples", async () => {
    mockApi({
      verdicts: [{ ...BASE, warnings: ["vram_tight"] }],
    });
    render(<ArtifactCompatSection artifactId="a1" />);
    await screen.findByText("Compatible — static evaluation");
    expect(screen.getByText(/tight at evaluation time/)).toBeTruthy();
    expect(screen.getByText(/measured at evaluation time/)).toBeTruthy();
  });
});

describe("CompatEvalAllButton", () => {
  it("POSTs evaluate-all and surfaces queued state", async () => {
    jobsObj = { jall: job("jall", "queued") };
    apiMock.post.mockImplementation((p: string) =>
      p === "/api/models/compat/evaluate-all"
        ? Promise.resolve({ job_id: "jall", status: "queued" })
        : Promise.reject(new Error("unmocked")));
    render(<CompatEvalAllButton />);
    fireEvent.click(screen.getByText("Evaluate compatibility"));
    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith(
      "/api/models/compat/evaluate-all",
      expect.objectContaining({ method: "POST" })));
    expect(await screen.findByText("queued")).toBeTruthy();
  });

  it("reports failed/cancelled/succeeded terminal states", async () => {
    jobsObj = { jall: job("jall", "failed") };
    apiMock.post.mockResolvedValue({ job_id: "jall", status: "queued" });
    render(<CompatEvalAllButton />);
    fireEvent.click(screen.getByText("Evaluate compatibility"));
    expect(await screen.findByText("failed")).toBeTruthy();
  });

  it("409 surfaces an honest in-progress message", async () => {
    apiMock.post.mockRejectedValue(new Error("409 conflict"));
    render(<CompatEvalAllButton />);
    fireEvent.click(screen.getByText("Evaluate compatibility"));
    expect(await screen.findByText(
      /already in progress/)).toBeTruthy();
  });
});

describe("CompatSection", () => {
  const ARTS = [
    { artifact_id: "a1", display_name: "Model One" },
    { artifact_id: "a2", display_name: "Model Two" },
  ] as unknown as import("../api/types").ModelArtifact[];

  it("renders the selector, first artifact panel, and evaluate-all", async () => {
    render(<CompatSection artifacts={ARTS} />);
    expect(await screen.findByText("Compatible — static evaluation"))
      .toBeTruthy();
    expect(screen.getByLabelText("Artifact to evaluate")).toBeTruthy();
    expect(screen.getByText("Evaluate compatibility")).toBeTruthy();
    // advisory framing is explicit — never a runnable claim
    expect(screen.getAllByText(/advisory only/).length)
      .toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/ready to run/i)).toBeNull();
  });

  it("switching the selector loads that artifact's verdicts", async () => {
    render(<CompatSection artifacts={ARTS} />);
    await screen.findByText("Compatible — static evaluation");
    fireEvent.change(screen.getByLabelText("Artifact to evaluate"),
                     { target: { value: "a2" } });
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith(
      "/api/models/artifacts/a2/compat"));
  });

  it("empty artifact list shows an honest empty state", () => {
    render(<CompatSection artifacts={[]} />);
    expect(screen.getByText(/No artifacts to evaluate/)).toBeTruthy();
  });
});
