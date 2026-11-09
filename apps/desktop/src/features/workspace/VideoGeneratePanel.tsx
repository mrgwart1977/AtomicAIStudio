import { useEffect, useMemo, useState } from "react";
import { apiFetch, assetFileUrl, getBackendInfo } from "../../api/client";
import { commitCommand } from "../../api/commands";
import { useApp } from "../../stores/appStore";
import type { Asset, AssetPage, BackendInfo } from "../../api/types";

const inputCls =
  "w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg placeholder:text-faint";
const labelCls = "mb-1 block text-xs font-medium text-muted";

/** M4.0 Phase-1 video generation panel. Beginner-first: a prompt, an
 *  optional source image (image-to-video), clip length, and one button.
 *  Advanced knobs (steps/cfg/sampler/backend) reuse catalog defaults. */
export function VideoGeneratePanel() {
  const pid = useApp((s) => s.currentProjectId);
  const project = useApp((s) => s.currentProject());
  const models = useApp((s) => s.models);
  const storyboard = useApp((s) => s.storyboard);
  const jobs = useApp((s) => s.jobs);
  const generateShotId = useApp((s) => s.generateShotId);

  const [modelKey, setModelKey] = useState("");
  const [prompt, setPrompt] = useState("");
  const [imageAssetId, setImageAssetId] = useState("");
  const [frames, setFrames] = useState(33);
  const [shotId, setShotId] = useState("");
  const [images, setImages] = useState<Asset[]>([]);
  const [videos, setVideos] = useState<Asset[]>([]);
  const [selected, setSelected] = useState<Asset | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [info, setInfo] = useState<BackendInfo | null>(null);

  const installedVideo = useMemo(
    () => models.filter((m) => m.modality === "video" && m.install?.status === "installed"),
    [models],
  );
  const plannedVideo = useMemo(
    () => models.filter((m) => m.modality === "video" && m.install?.status !== "installed"),
    [models],
  );
  const model = installedVideo.find((m) => m.key === modelKey) ?? installedVideo[0];
  const frameCaps = (model?.capabilities as Record<string, { min?: number; max?: number; default?: number } | undefined> | undefined)?.frames;

  const shots = useMemo(
    () =>
      storyboard.flatMap((s) =>
        s.shots.map((sh) => ({ id: sh.shot.id, label: `${s.scene.title} — ${sh.shot.title}` })),
      ),
    [storyboard],
  );

  const videoJobs = useMemo(
    () =>
      Object.values(jobs)
        .filter((j) => j.adapter_id === "gen.video.sdcpp")
        .sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [jobs],
  );
  const runningJob = videoJobs.find((j) => j.status === "running" || j.status === "queued");

  async function loadAssets() {
    if (!pid) return;
    const [img, vid] = await Promise.all([
      apiFetch<AssetPage>(`/api/projects/${pid}/assets?kind=image&limit=100`),
      apiFetch<AssetPage>(`/api/projects/${pid}/assets?kind=video&limit=50`),
    ]);
    setImages(img.items);
    setVideos(vid.items);
    setSelected((s) => s ?? vid.items[0] ?? null);
  }

  useEffect(() => {
    void getBackendInfo().then(setInfo);
    void loadAssets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid]);

  useEffect(() => {
    if (generateShotId) setShotId(generateShotId);
  }, [generateShotId]);

  const finished = videoJobs.filter((j) => j.status === "succeeded").length;
  useEffect(() => {
    if (finished > 0) void loadAssets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [finished]);

  async function generate() {
    if (!pid || !project || !model) return;
    setSubmitting(true);
    setErr(null);
    try {
      await commitCommand(pid, project.revision, "generate.video", {
        model_key: model.key,
        prompt,
        frames,
        image_asset_id: imageAssetId || undefined,
        shot_id: shotId || undefined,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel(jobId: string) {
    try {
      await apiFetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    } catch {
      /* best-effort */
    }
  }

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[380px_1fr]">
      <div className="space-y-4">
        <div>
          <label className={labelCls}>Model</label>
          {installedVideo.length === 0 ? (
            <p className="rounded border border-line bg-card px-3 py-2 text-sm text-muted">
              No video models installed yet. Install one in the Model Manager — nothing is
              downloaded without your approval.
            </p>
          ) : (
            <select className={inputCls} value={model?.key ?? ""} onChange={(e) => setModelKey(e.target.value)}>
              {installedVideo.map((m) => (
                <option key={m.key} value={m.key}>{m.display_name}</option>
              ))}
            </select>
          )}
          {plannedVideo.length > 0 && installedVideo.length === 0 && (
            <p className="mt-1 text-xs text-faint">
              {plannedVideo.length} video model{plannedVideo.length > 1 ? "s" : ""} in the catalog —
              installable from the Model Manager once approved.
            </p>
          )}
        </div>

        <div>
          <label className={labelCls}>Prompt — describe the clip</label>
          <textarea
            className={`${inputCls} h-24 resize-none`}
            placeholder="A lantern drifts down a night river, reflections rippling…"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
        </div>

        <div>
          <label className={labelCls}>Source image — optional (image → video)</label>
          <select className={inputCls} value={imageAssetId} onChange={(e) => setImageAssetId(e.target.value)}>
            <option value="">None — text to video</option>
            {images.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={labelCls} title="Total frames. 4k+1 values (33/49/81) align with the model's temporal stride.">
              Length (frames)
            </label>
            <select className={inputCls} value={frames} onChange={(e) => setFrames(Number(e.target.value))}>
              {[33, 49, 81, 129]
                .filter((f) => !frameCaps || (f >= (frameCaps.min ?? 1) && f <= (frameCaps.max ?? 129)))
                .map((f) => (
                  <option key={f} value={f}>{f} ≈ {(f / (model?.capabilities?.fps?.[0] ?? 24)).toFixed(1)}s</option>
                ))}
            </select>
          </div>
          <div>
            <label className={labelCls}>Attach to shot</label>
            <select className={inputCls} value={shotId} onChange={(e) => setShotId(e.target.value)}>
              <option value="">None</option>
              {shots.map((s) => (
                <option key={s.id} value={s.id}>{s.label}</option>
              ))}
            </select>
          </div>
        </div>

        {err && <p className="rounded bg-rose/15 px-3 py-2 text-sm text-rose">{err}</p>}

        <div className="flex items-center gap-2">
          <button
            onClick={generate}
            disabled={submitting || !model || (!prompt.trim() && !imageAssetId) || !!runningJob}
            className="rounded-md bg-action px-5 py-2 text-sm font-medium text-white disabled:opacity-50"
          >
            Generate clip
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
                className="h-full bg-action transition-all"
                style={{ width: `${Math.round((runningJob.progress || 0) * 100)}%` }}
              />
            </div>
            <p className="mt-1 text-xs text-muted">
              {runningJob.message || runningJob.status}
              {runningJob.model_key ? ` · ${runningJob.model_key}` : ""}
            </p>
          </div>
        )}
      </div>

      <div>
        {selected ? (
          <video
            key={selected.id}
            className="w-full rounded-md border border-line bg-black"
            src={info ? assetFileUrl(selected.project_id, selected.id, info) : undefined}
            controls
            preload="metadata"
          />
        ) : (
          <div className="flex h-64 items-center justify-center rounded-md border border-dashed border-line text-sm text-faint">
            Generated clips appear here
          </div>
        )}
        {videos.length > 1 && (
          <div className="mt-3 grid grid-cols-4 gap-2">
            {videos.slice(0, 12).map((v) => (
              <button
                key={v.id}
                onClick={() => setSelected(v)}
                className={`truncate rounded border px-2 py-1 text-xs ${
                  selected?.id === v.id ? "border-action text-fg" : "border-line text-muted"
                }`}
              >
                {v.name}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
