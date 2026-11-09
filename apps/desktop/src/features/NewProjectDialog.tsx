import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { apiFetch } from "../api/client";
import type { Project, StorageLocation } from "../api/types";
import { useApp } from "../stores/appStore";

const KINDS = [
  { id: "movie", label: "Movie", desc: "A full production with scenes and shots" },
  { id: "short", label: "Short film", desc: "A compact standalone story" },
  { id: "scene", label: "Scene", desc: "One cinematic sequence" },
  { id: "image", label: "Image project", desc: "Stills, concepts and artwork" },
] as const;

const FORMATS = [
  { label: "Widescreen 16:9 — 1080p", ratio: "16:9", w: 1920, h: 1080 },
  { label: "Cinemascope 2.39:1 — 2K", ratio: "2.39:1", w: 2048, h: 858 },
  { label: "Vertical 9:16 — 1080×1920", ratio: "9:16", w: 1080, h: 1920 },
  { label: "Square 1:1 — 1080×1080", ratio: "1:1", w: 1080, h: 1080 },
  { label: "Classic 4:3 — 1440×1080", ratio: "4:3", w: 1440, h: 1080 },
] as const;

const FPS = ["24", "25", "30", "23.976", "48", "60"] as const;
const STYLES = ["none", "cinematic", "documentary", "anime", "noir", "dreamlike"] as const;

export function NewProjectDialog({
  open,
  onOpenChange,
  initialKind = "movie",
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  initialKind?: string;
}) {
  const [step, setStep] = useState(0);
  const [advanced, setAdvanced] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<string>(initialKind);
  const [fmt, setFmt] = useState(0);
  const [fps, setFps] = useState<string>("24");
  const [style, setStyle] = useState<string>("none");
  const [locationId, setLocationId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const locations = useApp((s) => s.locations);
  const refresh = useApp((s) => s.refreshProjects);
  const setProject = useApp((s) => s.setCurrentProject);
  const setPage = useApp((s) => s.setPage);

  function reset() {
    setStep(0);
    setAdvanced(false);
    setName("");
    setKind(initialKind);
    setFmt(0);
    setFps("24");
    setStyle("none");
    setLocationId("");
    setError(null);
  }

  async function create() {
    setBusy(true);
    setError(null);
    const f = FORMATS[fmt];
    const [num, den] = fps === "23.976" ? [24000, 1001] : [parseInt(fps, 10), 1];
    try {
      const project = await apiFetch<Project>("/api/projects", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          kind,
          aspect_ratio: f.ratio,
          width: f.w,
          height: f.h,
          fps_num: num,
          fps_den: den,
          style,
          storage_location_id: locationId || null,
        }),
      });
      await refresh();
      setProject(project.id);
      onOpenChange(false);
      reset();
      setPage("project");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const canNext = step === 0 ? name.trim().length > 0 : true;

  return (
    <Dialog.Root open={open} onOpenChange={(o) => { if (!busy) { if (o) setKind(initialKind); else reset(); onOpenChange(o); } }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60" />
        <Dialog.Content className="fixed left-1/2 top-1/2 w-[520px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line bg-panel p-6 shadow-xl">
          <Dialog.Title className="text-lg font-semibold">New project</Dialog.Title>
          <p className="mt-1 text-sm text-muted">
            Step {step + 1} of 3 — {["the basics", "picture format", "location & style"][step]}
          </p>
          <div className="mt-2 flex gap-1" aria-hidden>
            {[0, 1, 2].map((i) => (
              <div key={i} className={`h-1 flex-1 rounded ${i <= step ? "bg-teal" : "bg-line"}`} />
            ))}
          </div>

          {step === 0 && (
            <div className="mt-4">
              <label className="block text-sm text-muted">
                Project title
                <input
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="My film"
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg"
                />
              </label>
              <p className="mt-4 text-sm text-muted">What are you making?</p>
              <div className="mt-2 grid grid-cols-2 gap-2">
                {KINDS.map((k) => (
                  <button
                    key={k.id}
                    onClick={() => setKind(k.id)}
                    className={`rounded-md border p-3 text-left ${
                      kind === k.id ? "border-teal bg-teal/10" : "border-line bg-card hover:border-faint"
                    }`}
                  >
                    <div className="text-sm font-medium">{k.label}</div>
                    <div className="mt-0.5 text-xs text-muted">{k.desc}</div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {step === 1 && (
            <div className="mt-4">
              <p className="text-sm text-muted">Picture format</p>
              <div className="mt-2 space-y-1.5">
                {FORMATS.map((f, i) => (
                  <button
                    key={f.label}
                    onClick={() => setFmt(i)}
                    className={`w-full rounded-md border px-3 py-2 text-left text-sm ${
                      fmt === i ? "border-teal bg-teal/10" : "border-line bg-card hover:border-faint"
                    }`}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
              <label className="mt-4 block text-sm text-muted">
                Frame rate
                <select
                  value={fps}
                  onChange={(e) => setFps(e.target.value)}
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg"
                >
                  {FPS.map((f) => (
                    <option key={f} value={f}>
                      {f} fps{f === "24" ? " (film standard)" : ""}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          )}

          {step === 2 && (
            <div className="mt-4">
              <label className="block text-sm text-muted">
                Cinematic style <span className="text-faint">(optional)</span>
                <select
                  value={style}
                  onChange={(e) => setStyle(e.target.value)}
                  className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg"
                >
                  {STYLES.map((s) => (
                    <option key={s} value={s}>
                      {s === "none" ? "No preset — decide later" : s[0].toUpperCase() + s.slice(1)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                onClick={() => setAdvanced((v) => !v)}
                className="mt-4 text-sm text-teal hover:underline"
              >
                {advanced ? "Hide advanced settings" : "Advanced settings"}
              </button>
              {advanced && (
                <label className="mt-3 block text-sm text-muted">
                  Storage location
                  <select
                    value={locationId}
                    onChange={(e) => setLocationId(e.target.value)}
                    className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg"
                  >
                    <option value="">Default location</option>
                    {locations.map((l: StorageLocation) => (
                      <option key={l.id} value={l.id}>
                        {l.label} — {l.root_path}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <div className="mt-4 rounded-md border border-line bg-card p-3 text-sm text-muted">
                <div className="font-medium text-fg">{name || "Untitled"}</div>
                <div className="mt-1">
                  {KINDS.find((k) => k.id === kind)?.label} • {FORMATS[fmt].label} • {fps} fps
                  {style !== "none" ? ` • ${style}` : ""}
                </div>
              </div>
            </div>
          )}

          {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}
          <div className="mt-6 flex justify-between">
            <button
              onClick={() => (step === 0 ? onOpenChange(false) : setStep(step - 1))}
              className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg"
            >
              {step === 0 ? "Cancel" : "Back"}
            </button>
            {step < 2 ? (
              <button
                onClick={() => setStep(step + 1)}
                disabled={!canNext}
                className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
              >
                Continue
              </button>
            ) : (
              <button
                onClick={create}
                disabled={busy}
                className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
              >
                {busy ? "Creating…" : "Create project"}
              </button>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
