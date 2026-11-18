/** A8 — small job polling helper for the wizard's awaited commands. */
import { apiFetch } from "../../api/client";
import type { Job } from "../../api/types";

export async function pollJob(
  pid: string, jobId: string, timeoutMs = 180_000,
  onTick?: (j: Job) => void,
): Promise<Job> {
  const t0 = Date.now();
  for (;;) {
    const jobs = await apiFetch<Job[]>(`/api/projects/${pid}/jobs`);
    const j = jobs.find((x) => x.id === jobId);
    if (j) onTick?.(j);
    if (j && ["succeeded", "failed", "cancelled"].includes(j.status)) {
      if (j.status !== "succeeded") {
        throw new Error(j.error || `The job was ${j.status}.`);
      }
      return j;
    }
    if (Date.now() - t0 > timeoutMs) throw new Error("job timed out");
    await new Promise((r) => setTimeout(r, 1500));
  }
}
