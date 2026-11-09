import { useEffect } from "react";
import { useApp } from "../stores/appStore";

function gb(bytes: number | null | undefined): string {
  if (bytes == null) return "unknown";
  return `${(bytes / 1e9).toFixed(1)} GB`;
}

export function ModelManagerPage() {
  const hw = useApp((s) => s.hardware);
  const adapters = useApp((s) => s.adapters);
  const refresh = useApp((s) => s.refreshHardware);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="mx-auto max-w-5xl p-8">
      <h1 className="text-2xl font-bold">Set up your studio</h1>
      <p className="mt-1 text-muted">
        Detected hardware and registered adapters. Model downloads arrive in M3 and always ask first.
      </p>

      {!hw ? (
        <p className="mt-8 text-muted">Probing hardware…</p>
      ) : (
        <div className="mt-6 grid grid-cols-2 gap-5">
          <section className="rounded-lg border border-line bg-card p-4">
            <h2 className="font-semibold">System</h2>
            <dl className="mt-2 space-y-1 text-sm text-muted">
              <div className="flex justify-between">
                <dt>OS</dt>
                <dd className="text-fg">
                  {hw.os.system} {hw.os.release}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt>CPU</dt>
                <dd className="text-right text-fg">
                  {hw.cpu.name} ({hw.cpu.logical_cores ?? "?"} threads)
                </dd>
              </div>
              <div className="flex justify-between">
                <dt>Memory</dt>
                <dd className="text-fg">{gb(hw.memory.total_bytes)}</dd>
              </div>
            </dl>
          </section>

          <section className="rounded-lg border border-line bg-card p-4">
            <h2 className="font-semibold">GPU</h2>
            {hw.gpus.length === 0 ? (
              <p className="mt-2 text-sm text-muted">
                No GPU reported. Atomic AI Studio runs without one; local inference adapters will be
                suggested only where verified.
              </p>
            ) : (
              <ul className="mt-2 space-y-2 text-sm text-muted">
                {hw.gpus.map((g, i) => (
                  <li key={i} className="rounded border border-line p-2">
                    <div className="text-fg">{g.name ?? "Unknown adapter"}</div>
                    <div>
                      {g.vendor} • VRAM {gb(g.vram_bytes)}
                      {g.vram_capped_at_4gb && " (reported value capped)"} • driver{" "}
                      {g.driver_version ?? "unknown"}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="rounded-lg border border-line bg-card p-4">
            <h2 className="font-semibold">Storage</h2>
            <ul className="mt-2 space-y-1 text-sm text-muted">
              {hw.disks.map((d) => (
                <li key={d.mount} className="flex justify-between">
                  <span>{d.mount}</span>
                  <span className="text-fg">
                    {gb(d.free_bytes)} free of {gb(d.total_bytes)}
                  </span>
                </li>
              ))}
            </ul>
          </section>

          <section className="rounded-lg border border-line bg-card p-4">
            <h2 className="font-semibold">Runtimes</h2>
            <dl className="mt-2 space-y-1 text-sm text-muted">
              <div className="flex justify-between">
                <dt>Python</dt>
                <dd className="text-fg">{hw.runtimes.python}</dd>
              </div>
              <div className="flex justify-between">
                <dt>FFmpeg</dt>
                <dd className="text-fg">
                  {hw.runtimes.ffmpeg.available ? (hw.runtimes.ffmpeg.version ?? "installed") : "not found on PATH"}
                </dd>
              </div>
              <div className="flex justify-between">
                <dt>PyTorch</dt>
                <dd className="text-fg">{hw.runtimes.torch_installed ? "installed" : "not installed"}</dd>
              </div>
            </dl>
          </section>
        </div>
      )}

      <section className="mt-6 rounded-lg border border-line bg-card p-4">
        <h2 className="font-semibold">Registered provider adapters</h2>
        <ul className="mt-2 space-y-2 text-sm">
          {adapters.map((a) => (
            <li key={a.adapter_id} className="rounded border border-line p-3">
              <div className="flex items-center justify-between">
                <span className="text-fg">{a.display_name}</span>
                <span className="rounded bg-amber/15 px-2 py-0.5 text-xs uppercase text-amber">
                  {a.adapter_kind}
                </span>
              </div>
              <p className="mt-1 text-muted">{a.notes}</p>
              <p className="mt-0.5 text-xs text-faint">
                tasks: {a.tasks.join(", ")} • cloud: {a.requires_cloud ? "yes" : "no"} • cost: {a.cost_model}
              </p>
            </li>
          ))}
        </ul>
        <p className="mt-3 text-xs text-faint">
          Real local and cloud adapters are added only after hardware verification and explicit approval.
        </p>
      </section>
    </div>
  );
}
