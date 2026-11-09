import { useEffect, useMemo, useState } from "react";
import { apiFetch, assetFileUrl, getBackendInfo } from "../../api/client";
import { commitCommand } from "../../api/commands";
import { useApp } from "../../stores/appStore";
import type { Asset, AssetPage, BackendInfo, ModelCatalogEntry } from "../../api/types";

const inputCls =
  "w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg placeholder:text-faint";
const labelCls = "mb-1 block text-xs font-medium text-muted";

interface GenForm {
  model_key: string;
  prompt: string;
  negative_prompt: string;
  width: number;
  height: number;
  steps: number;
  cfg: number;
  seed: number; // -1 = random
  sampler: string;
  batch: number;
  device: string; // "auto" | "cpu" | gpu index as string
  shot_id: string;
  set_thumbnail: boolean;
}

function defaults(model: ModelCatalogEntry | undefined): GenForm {
  const caps = model?.capabilities;
  const preset = caps?.sizes?.presets?.[0];
  return {
    model_key: model?.key ?? "",
    prompt: "",
    negative_prompt: "",
    width: preset?.w ?? 512,
    height: preset?.h ?? 512,
    steps: caps?.steps?.default ?? 25,
    cfg: caps?.cfg?.default ?? 7,
    seed: -1,
    sampler: caps?.samplers?.[0] ?? "euler_a",
    batch: caps?.batch?.default ?? 1,
    device: "auto",
    shot_id: "",
    set_thumbnail: true,
  };
}

function gb(b: number | null | undefined): string {
  return b == null ? "?" : `${(b / 1e9).toFixed(1)} GB`;
}

export function GeneratePage() {
  const pid = useApp((s) => s.currentProjectId);
  const project = useApp((s) => s.currentProject());
  const models = useApp((s) => s.models);
  const gpuState = useApp((s) => s.gpuState);
  const storyboard = useApp((s) => s.storyboard);
  const jobs = useApp((s) => s.jobs);
  const generateShotId = useApp((s) => s.generateShotId);
  const refreshModels = useApp((s) => s.refreshModels);
  const refreshStoryboard = useApp((s) => s.refreshStoryboard);

  const [form, setForm] = useState<GenForm>(defaults(undefined));
  const [advanced, setAdvanced] = useState(false);
  const [genAssets, setGenAssets] = useState<Asset[]>([]);
  const [selected, setSelected] = useState<Asset | null>(null);
  const [info, setInfo] = useState<BackendInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const installed = useMemo(
    () => models.filter((m) => m.install?.status === "installed"),
    [models],
  );
  const model = useMemo(
    () => models.find((m) => m.key === form.model_key) ?? installed[0],
    [models, form.model_key, installed],
  );
  const caps = model?.capabilities;

  const shots = useMemo(
    () =>
      storyboard.flatMap((s) =>
        s.shots.map((sh) => ({
          id: sh.shot.id,
          label: `${s.scene.title} — ${sh.shot.title}`,
        })),
      ),
    [storyboard],
  );

  const genJobs = useMemo(
    () =>
      Object.values(jobs)
        .filter((j) => j.adapter_id.startsWith("gen.") || j.adapter_id === "mock.image")
        .sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [jobs],
  );
  const runningJob = genJobs.find(
    (j) => j.status === "running" || j.status === "queued",
  );

  async function loadGenerated() {
    if (!pid) return;
    const page = await apiFetch<AssetPage>(
      `/api/projects/${pid}/assets?kind=image&origin=generation&limit=100`,
    );
    setGenAssets(page.items);
    setSelected((s) => (s ? page.items.find((a) => a.id === s.id) ?? s : page.items[0] ?? null));
  }

  useEffect(() => {
    void getBackendInfo().then(setInfo);
    void refreshModels();
    void refreshStoryboard();
    void loadGenerated();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid]);

  // Pre-fill the shot target when arriving from the storyboard.
  useEffect(() => {
    if (generateShotId) setForm((f) => ({ ...f, shot_id: generateShotId }));
  }, [generateShotId]);

  // Refresh gallery when a generation job finishes.
  const finished = genJobs.filter((j) => j.status === "succeeded").length;
  useEffect(() => {
    if (finished > 0) {
      void loadGenerated();
      void refreshStoryboard();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished]);

  function set<K extends keyof GenForm>(k: K, v: GenForm[K]) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  function pickModel(key: string) {
    const m = models.find((x) => x.key === key);
    setForm((f) => {
      const d = defaults(m);
      return { ...f, model_key: key, sampler: d.sampler, steps: d.steps, cfg: d.cfg };
    });
  }

  async function generate() {
    if (!pid || !project || !model) return;
    setSubmitting(true);
    setErr(null);
    try {
      await commitCommand(pid, project.revision, "generate.image", {
        model_key: model.key,
        prompt: form.prompt,
        negative_prompt: form.negative_prompt,
        width: form.width,
        height: form.height,
        steps: form.steps,
        cfg: form.cfg,
        seed: form.seed,
        sampler: form.sampler,
        batch: form.batch,
        device: form.device,
        shot_id: form.shot_id || undefined,
        set_thumbnail: form.set_thumbnail && !!form.shot_id,
        runtime_flavor: undefined,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function regenerate() {
    if (!selected) return;
    const job = genJobs.find((j) => j.output_asset_id === selected.id);
    const r = job?.result_json as Record<string, unknown> | undefined;
    if (r?.model_key) {
      setForm((f) => ({
        ...f,
        model_key: String(r.model_key),
        prompt: String(r.prompt ?? f.prompt),
        negative_prompt: String(r.negative_prompt ?? ""),
        width: Number(r.width ?? f.width),
        height: Number(r.height ?? f.height),
        steps: Number(r.steps ?? f.steps),
        cfg: Number(r.cfg ?? f.cfg),
        seed: Number(r.seed ?? -1),
        sampler: String(r.sampler ?? f.sampler),
      }));
      void generate();
    }
  }

  async function cancel(jobId: string) {
    await apiFetch(`/api/jobs/${jobId}/cancel`, { method: "POST", body: "{}" });
  }

  const estVram = model?.compatibility.estimate.vram_bytes;
  const chosenGpu =
    form.device !== "auto" && form.device !== "cpu"
      ? gpuState?.slots.find((s) => String(s.index) === form.device)
      : undefined;
  const memWarn =
    estVram != null && chosenGpu?.vram_bytes != null && estVram > chosenGpu.vram_bytes;

  if (!pid) return null;

  return (
    <div className="grid h-full grid-cols-[380px_1fr] gap-6 p-6">
      {/* ---------- controls ---------- */}
      <div className="space-y-4 overflow-y-auto pr-1">
        <h2 className="text-lg font-semibold">Generate image</h2>

        <div>
          <label className={labelCls}>Model</label>
          {installed.length === 0 ? (
            <p className="rounded bg-amber/15 px-3 py-2 text-sm text-amber">
              No models installed yet. Install one in the Model Manager — nothing is downloaded
              without your approval.
            </p>
          ) : (
            <select
              className={inputCls}
              value={model?.key ?? ""}
              onChange={(e) => pickModel(e.target.value)}
            >
              {installed.map((m) => (
                <option key={m.key} value={m.key}>
                  {m.display_name}
                  {m.compatibility.status === "limited" ? " (limited)" : ""}
                </option>
              ))}
            </select>
          )}
          {model && model.compatibility.status !== "compatible" && (
            <p className="mt-1 text-xs text-amber">
              {model.compatibility.warnings.join(" ") || model.compatibility.reasons.join(" ")}
            </p>
          )}
        </div>

        <div>
          <label className={labelCls}>Prompt — describe the image</label>
          <textarea
            className={inputCls}
            rows={4}
            value={form.prompt}
            onChange={(e) => set("prompt", e.target.value)}
            placeholder="A cozy cabin in a snowy forest at dusk, warm windows, cinematic light…"
          />
        </div>

        {caps?.negative_prompt && (
          <div>
            <label className={labelCls}>Negative prompt — what to avoid</label>
            <textarea
              className={inputCls}
              rows={2}
              value={form.negative_prompt}
              onChange={(e) => set("negative_prompt", e.target.value)}
              placeholder="blurry, low quality, watermark…"
            />
          </div>
        )}

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelCls}>Size</label>
            <select
              className={inputCls}
              value={`${form.width}x${form.height}`}
              onChange={(e) => {
                const [w, h] = e.target.value.split("x").map(Number);
                setForm((f) => ({ ...f, width: w, height: h }));
              }}
            >
              {(caps?.sizes?.presets ?? [{ w: 512, h: 512, label: "Square" }]).map((p) => (
                <option key={`${p.w}x${p.h}`} value={`${p.w}x${p.h}`}>
                  {p.w}×{p.h} {p.label ?? ""}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className={labelCls}>Batch — images per run</label>
            <select
              className={inputCls}
              value={form.batch}
              onChange={(e) => set("batch", Number(e.target.value))}
            >
              {Array.from(
                { length: Math.max(1, caps?.batch?.max ?? 1) - Math.max(1, caps?.batch?.min ?? 1) + 1 },
                (_, i) => (caps?.batch?.min ?? 1) + i,
              ).map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>
        </div>

        {gpuState && gpuState.slots.length > 0 && (
          <div>
            <label className={labelCls}>GPU</label>
            <select className={inputCls} value={form.device} onChange={(e) => set("device", e.target.value)}>
              <option value="auto">Automatic (recommended)</option>
              {gpuState.slots.map((s) => (
                <option key={s.index} value={String(s.index)}>
                  GPU {s.index}: {s.name ?? "?"} ({gb(s.vram_bytes)})
                </option>
              ))}
              {caps?.cpu_fallback && <option value="cpu">CPU only (slow)</option>}
            </select>
            {memWarn && (
              <p className="mt-1 text-xs text-amber">
                Estimated {gb(estVram)} needed may exceed this GPU's {gb(chosenGpu?.vram_bytes)} VRAM.
                Automatic mode is safer.
              </p>
            )}
          </div>
        )}

        <div>
          <label className={labelCls}>Attach result to a shot (optional)</label>
          <select className={inputCls} value={form.shot_id} onChange={(e) => set("shot_id", e.target.value)}>
            <option value="">No shot — save to assets only</option>
            {shots.map((s) => (
              <option key={s.id} value={s.id}>{s.label}</option>
            ))}
          </select>
          {form.shot_id && (
            <label className="mt-1 flex items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={form.set_thumbnail}
                onChange={(e) => set("set_thumbnail", e.target.checked)}
              />
              Set as the shot's thumbnail when it finishes
            </label>
          )}
        </div>

        <button
          type="button"
          onClick={() => setAdvanced((a) => !a)}
          className="text-xs text-muted underline-offset-2 hover:underline"
        >
          {advanced ? "Hide advanced settings" : "Advanced settings"}
        </button>

        {advanced && (
          <div className="space-y-3 rounded-md border border-line p-3">
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={labelCls} title="Same seed + same settings = same image. -1 picks a random one each run.">
                  Seed (−1 = random)
                </label>
                <div className="flex gap-1">
                  <input
                    type="number"
                    className={inputCls}
                    value={form.seed}
                    onChange={(e) => set("seed", Number(e.target.value))}
                  />
                  <button
                    type="button"
                    title="Random seed"
                    onClick={() => set("seed", Math.floor(Math.random() * 2 ** 31))}
                    className="rounded-md border border-line px-2 text-sm text-muted hover:text-fg"
                  >
                    🎲
                  </button>
                </div>
              </div>
              <div>
                <label className={labelCls} title="More steps = more detail, slower. 20–30 is a good default.">
                  Steps
                </label>
                <input
                  type="number"
                  min={caps?.steps?.min ?? 1}
                  max={caps?.steps?.max ?? 150}
                  className={inputCls}
                  value={form.steps}
                  onChange={(e) => set("steps", Number(e.target.value))}
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className={labelCls} title="How strictly to follow the prompt. 7 is the usual sweet spot.">
                  Guidance (CFG)
                </label>
                <input
                  type="number"
                  step={0.5}
                  min={caps?.cfg?.min ?? 1}
                  max={caps?.cfg?.max ?? 30}
                  className={inputCls}
                  value={form.cfg}
                  onChange={(e) => set("cfg", Number(e.target.value))}
                />
              </div>
              {caps?.samplers && caps.samplers.length > 0 && (
                <div>
                  <label className={labelCls}>Sampler</label>
                  <select className={inputCls} value={form.sampler} onChange={(e) => set("sampler", e.target.value)}>
                    {caps.samplers.map((s) => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>
            <p className="text-xs text-faint">
              Estimated VRAM: {model?.compatibility.estimate.basis ?? "unknown"}
              {estVram ? ` ≈ ${gb(estVram)}` : ""}
            </p>
          </div>
        )}

        {err && <p className="rounded bg-rose/15 px-3 py-2 text-sm text-rose">{err}</p>}

        <div className="flex items-center gap-2">
          <button
            onClick={generate}
            disabled={submitting || !model || !form.prompt.trim() || !!runningJob}
            className="rounded-md bg-action px-5 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            Generate
          </button>
          {runningJob && (
            <button
              onClick={() => void cancel(runningJob.id)}
              className="rounded-md border border-rose/40 px-4 py-2 text-sm text-rose"
            >
              Cancel
            </button>
          )}
        </div>

        {runningJob && (
          <div>
            <div className="h-1.5 w-full overflow-hidden rounded bg-line/40">
              <div
                className="h-full bg-teal transition-all"
                style={{ width: `${Math.round(runningJob.progress * 100)}%` }}
              />
            </div>
            <p className="mt-1 text-xs text-muted">
              {runningJob.message || "Working…"}
              {runningJob.gpu_index != null && runningJob.gpu_index >= 0
                ? ` · GPU ${runningJob.gpu_index}`
                : ""}
              {runningJob.model_key ? ` · ${runningJob.model_key}` : ""}
            </p>
          </div>
        )}
      </div>

      {/* ---------- results ---------- */}
      <div className="flex min-h-0 flex-col">
        <div className="flex items-baseline justify-between">
          <h2 className="text-lg font-semibold">Results</h2>
          <span className="text-xs text-faint">{genAssets.length} generated</span>
        </div>

        {selected && info ? (
          <div className="mt-3 flex min-h-0 flex-1 flex-col">
            <div className="min-h-0 flex-1 overflow-hidden rounded-lg border border-line bg-card">
              <img
                src={assetFileUrl(pid, selected.id, info)}
                alt={selected.name}
                className="h-full w-full object-contain"
              />
            </div>
            <div className="mt-2 flex items-center justify-between text-xs text-muted">
              <span>
                {selected.name} · {(selected.byte_size / 1024).toFixed(0)} KB ·{" "}
                {new Date(selected.created_at).toLocaleString()}
              </span>
              <button
                onClick={regenerate}
                className="rounded border border-line px-2 py-1 hover:text-fg"
                title="Regenerate with the same settings (new random seed still applies when seed was −1)"
              >
                Regenerate
              </button>
            </div>
          </div>
        ) : (
          <div className="mt-3 flex flex-1 items-center justify-center rounded-lg border border-dashed border-line text-sm text-muted">
            Generated images appear here — each one is saved to your project automatically.
          </div>
        )}

        {genAssets.length > 0 && info && (
          <div className="mt-3 grid max-h-40 grid-cols-6 gap-2 overflow-y-auto">
            {genAssets.map((a) => (
              <button
                key={a.id}
                onClick={() => setSelected(a)}
                className={`aspect-square overflow-hidden rounded border ${
                  selected?.id === a.id ? "border-teal" : "border-line"
                }`}
              >
                <img
                  src={assetFileUrl(pid, a.id, info)}
                  alt={a.name}
                  className="h-full w-full object-cover"
                />
              </button>
            ))}
          </div>
        )}

        {genJobs.length > 0 && (
          <details className="mt-3 text-xs text-muted">
            <summary className="cursor-pointer">Generation history ({genJobs.length} runs)</summary>
            <ul className="mt-1 space-y-1">
              {genJobs.slice(0, 20).map((j) => (
                <li key={j.id} className="flex justify-between rounded border border-line px-2 py-1">
                  <span>
                    {j.status} · {j.model_key ?? j.adapter_id}
                    {j.gpu_index != null && j.gpu_index >= 0 ? ` · GPU ${j.gpu_index}` : ""}
                    {(j.result_json as Record<string, unknown> | null)?.seed != null
                      ? ` · seed ${String((j.result_json as Record<string, unknown>).seed)}`
                      : ""}
                  </span>
                  <span className="text-faint">{j.error ?? j.message}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}
