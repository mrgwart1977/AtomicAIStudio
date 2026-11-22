import { useEffect, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { apiFetch } from "../api/client";
import { pickFile } from "../api/commands";
import { useApp } from "../stores/appStore";
import type { Job, ModelImportOut } from "../api/types";

/**
 * U-2b-1 — reference-only model file import.
 *
 * Privacy contract: the user-selected absolute path lives ONLY in this
 * component's local state (cleared on close/success) — never in the
 * global store, never persisted, never rendered outside the dialog.
 * The backend inspects + registers the file in place; nothing is
 * copied, moved, or modified (managed-copy is a later phase).
 */

// Beginner-facing text for the sanitized backend taxonomy codes —
// job.error carries the code verbatim, HTTP errors embed it in the body.
const ERR_TEXT: Record<string, string> = {
  license_required: "A license name is required — you declare it; the app can't check it for you.",
  path_not_absolute: "The file path must be absolute (e.g. D:\\models\\file.safetensors).",
  path_not_found: "That file doesn't exist at the given path.",
  path_is_link: "The file or one of its folders is a link/junction — imports must be regular files.",
  path_not_file: "That path isn't a regular file.",
  unsupported_format: "Only safetensors and GGUF files can be imported.",
  malformed_safetensors: "The safetensors file is malformed or truncated.",
  malformed_gguf: "The GGUF file is malformed or truncated.",
  path_changed: "The file changed while it was being inspected — try again.",
  io_error: "The file couldn't be read.",
  db_error: "Registration failed in the local database — try again.",
  identity_exhausted: "Couldn't assign an identity for this content — report this as a bug.",
  mode_not_supported: "That import mode isn't available yet.",
  display_name_too_long: "Display name is too long (200 characters max).",
  display_name_invalid: "Display name can't contain control characters.",
  duplicate_in_flight: "An import of this file is already in progress.",
};

function friendlyError(raw: string): string {
  for (const [code, text] of Object.entries(ERR_TEXT)) {
    if (raw.includes(code)) return text;
  }
  // 409 responses carry prose, not a taxonomy code.
  if (raw.startsWith("409")) return ERR_TEXT.duplicate_in_flight;
  return raw || "Import failed.";
}

const inputCls =
  "mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg";
const labelCls = "block text-sm text-muted";

export function ImportModelDialog({
  open,
  onOpenChange,
  onImported,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onImported: () => void;
}) {
  const jobs = useApp((s) => s.jobs);
  // --- sensitive: picker/manual path lives here and ONLY here ---
  const [path, setPath] = useState("");
  const [licenseName, setLicenseName] = useState("");
  const [licenseUrl, setLicenseUrl] = useState("");
  const [commercialOk, setCommercialOk] = useState(false);
  const [licenseNotes, setLicenseNotes] = useState("");
  const [family, setFamily] = useState("");
  const [roleHint, setRoleHint] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const doneRef = useRef(false);

  // Between POST→202 and the first WS job.progress event the job row is
  // queued but not yet in the map — render it as queued, never the form
  // (showing the form again would invite a duplicate submit).
  const tracking = jobId !== null;
  const job: Job | undefined = jobId ? jobs[jobId] : undefined;
  const status = job?.status ?? "queued";

  function reset() {
    setPath(""); setLicenseName(""); setLicenseUrl(""); setCommercialOk(false);
    setLicenseNotes(""); setFamily(""); setRoleHint(""); setDisplayName("");
    setJobId(null); setErr(null); setSubmitting(false);
    doneRef.current = false;
  }

  // Refresh the library once when the tracked job lands terminal.
  useEffect(() => {
    if (job && (status === "succeeded" || status === "failed"
                || status === "cancelled") && !doneRef.current) {
      doneRef.current = true;
      if (status === "succeeded") onImported();
    }
  }, [job, status, onImported]);

  const running = status === "queued" || status === "running";

  async function browse() {
    const picked = await pickFile("Import model file", [
      { name: "Model weights", extensions: ["safetensors", "gguf"] },
    ]);
    if (picked) setPath(picked);
  }

  async function submit() {
    setErr(null);
    if (!path.trim()) { setErr(ERR_TEXT.path_not_found); return; }
    if (!licenseName.trim()) { setErr(ERR_TEXT.license_required); return; }
    setSubmitting(true);
    try {
      const license: Record<string, unknown> = {
        name: licenseName.trim(), commercial_ok: commercialOk,
      };
      if (licenseUrl.trim()) license.url = licenseUrl.trim();
      if (licenseNotes.trim()) license.notes = licenseNotes.trim();
      const r = await apiFetch<ModelImportOut>("/api/models/import", {
        method: "POST",
        body: JSON.stringify({
          path: path.trim(), license,
          family: family.trim(), role_hint: roleHint,
          display_name: displayName.trim(), mode: "reference",
        }),
      });
      setJobId(r.job_id); // track THIS job — never scan for an arbitrary one
    } catch (e) {
      setErr(friendlyError(e instanceof Error ? e.message : String(e)));
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel() {
    if (!jobId) return;
    try {
      await apiFetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    } catch (e) {
      // 409 = job already terminal — the committed artifact stands;
      // not an error worth alarming about.
      const m = e instanceof Error ? e.message : String(e);
      if (!m.startsWith("409")) setErr(friendlyError(m));
    }
  }

  const succeeded = status === "succeeded";
  const failed = status === "failed";

  return (
    <Dialog.Root open={open} onOpenChange={(o) => { if (!o) reset(); onOpenChange(o); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60" />
        <Dialog.Content
          aria-describedby="import-model-desc"
          className="fixed left-1/2 top-1/2 max-h-[85vh] w-[480px] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-lg border border-line bg-panel p-5 shadow-xl"
        >
          <Dialog.Title className="text-lg font-semibold">Import a model file</Dialog.Title>
          <p id="import-model-desc" className="mt-1 text-sm text-muted">
            Registers one model or LoRA file you already have. The file is
            inspected where it sits — nothing is copied, moved, or changed.
            It is added to the library for tracking only; it cannot generate
            anything yet.
          </p>

          {!tracking && (
            <>
              <div className="mt-4">
                <label htmlFor="import-path" className={labelCls}>File</label>
                <div className="mt-1 flex gap-2">
                  <input
                    id="import-path"
                    value={path}
                    onChange={(e) => setPath(e.target.value)}
                    placeholder="Absolute path, e.g. D:\models\weights.safetensors"
                    className={`${inputCls} mt-0 flex-1`}
                  />
                  <button
                    type="button"
                    onClick={() => void browse()}
                    className="rounded-md border border-line px-3 py-2 text-sm text-muted hover:text-fg"
                  >
                    Browse…
                  </button>
                </div>
                <p className="mt-1 text-xs text-faint">
                  .safetensors or .gguf — the backend checks the real format,
                  not the extension.
                </p>
              </div>

              <fieldset className="mt-4 rounded-md border border-line p-3">
                <legend className="px-1 text-xs font-semibold text-muted">
                  What you declare — the app does not verify these
                </legend>
                <label htmlFor="import-license" className={labelCls}>
                  License name <span className="text-rose">*</span>
                  <input
                    id="import-license"
                    value={licenseName}
                    onChange={(e) => setLicenseName(e.target.value)}
                    placeholder="e.g. Apache-2.0, CC-BY-NC-4.0, OpenRAIL-M"
                    list="license-suggestions"
                    className={inputCls}
                  />
                  <datalist id="license-suggestions">
                    <option value="Apache-2.0" />
                    <option value="MIT" />
                    <option value="CC-BY-NC-4.0" />
                    <option value="OpenRAIL-M" />
                    <option value="CreativeML OpenRAIL" />
                    <option value="Other / see source" />
                  </datalist>
                </label>
                <div className="mt-3 grid grid-cols-2 gap-3">
                  <label htmlFor="import-license-url" className={labelCls}>
                    License URL
                    <input id="import-license-url" value={licenseUrl}
                      onChange={(e) => setLicenseUrl(e.target.value)}
                      placeholder="optional" className={inputCls} />
                  </label>
                  <label htmlFor="import-commercial" className={`${labelCls} flex items-end gap-2 pb-2`}>
                    <input id="import-commercial" type="checkbox"
                      checked={commercialOk}
                      onChange={(e) => setCommercialOk(e.target.checked)}
                      className="h-4 w-4" />
                    Commercial use allowed
                  </label>
                </div>
                <label htmlFor="import-license-notes" className={`${labelCls} mt-3`}>
                  License notes
                  <input id="import-license-notes" value={licenseNotes}
                    onChange={(e) => setLicenseNotes(e.target.value)}
                    placeholder="optional" className={inputCls} />
                </label>
              </fieldset>

              <fieldset className="mt-4 rounded-md border border-line p-3">
                <legend className="px-1 text-xs font-semibold text-muted">
                  Optional labels — the file's detected format is checked separately
                </legend>
                <div className="grid grid-cols-2 gap-3">
                  <label htmlFor="import-family" className={labelCls}>
                    Base family
                    <input id="import-family" value={family}
                      onChange={(e) => setFamily(e.target.value)}
                      placeholder="e.g. sd15, sdxl" className={inputCls} />
                  </label>
                  <label htmlFor="import-role" className={labelCls}>
                    Role
                    <select id="import-role" value={roleHint}
                      onChange={(e) => setRoleHint(e.target.value)}
                      className={inputCls}>
                      <option value="">auto — let the inspector decide</option>
                      <option value="lora">LoRA adapter</option>
                      <option value="model">base model</option>
                    </select>
                  </label>
                </div>
                <label htmlFor="import-name" className={`${labelCls} mt-3`}>
                  Display name
                  <input id="import-name" value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    maxLength={200}
                    placeholder="defaults to the file name" className={inputCls} />
                </label>
              </fieldset>
            </>
          )}

          {tracking && (
            <div className="mt-4">
              <div className="flex items-center justify-between text-xs">
                <span className="text-muted">ops.model.import</span>
                <span className={
                  succeeded ? "text-teal" : failed ? "text-rose"
                  : status === "cancelled" ? "text-muted" : "text-sky"}>
                  {status === "running" && job?.cancel_requested
                    ? "cancelling" : status}
                </span>
              </div>
              <div className="mt-2 h-1.5 rounded-full bg-line/40">
                <div
                  className={`h-full rounded-full transition-all ${failed ? "bg-rose" : "bg-teal"}`}
                  style={{ width: `${Math.round((job?.progress ?? 0) * 100)}%` }}
                />
              </div>
              <p className={`mt-1.5 text-xs ${failed ? "text-rose" : "text-muted"}`}>
                {succeeded
                  ? "Registered — it now appears in the library below."
                  : failed
                    ? friendlyError(job?.error ?? "")
                    : job?.message || job?.error ||
                      (status === "queued" ? "queued" : "inspecting file…")}
              </p>
              {succeeded && (
                <p className="mt-1 text-xs text-faint">
                  Registered for tracking only — imported models can't be used
                  for generation until a later compatibility milestone.
                </p>
              )}
              {status === "cancelled" && (
                <p className="mt-1 text-xs text-muted">
                  Cancelled. If the artifact was already committed it stays
                  registered — cancelling never removes a completed import.
                </p>
              )}
            </div>
          )}

          {err && (
            <p role="alert" className="mt-3 rounded bg-rose/15 px-3 py-2 text-sm text-rose">{err}</p>
          )}

          <div className="mt-5 flex justify-end gap-2">
            {running && (
              <button
                type="button"
                onClick={() => void cancel()}
                className="rounded-md border border-rose/40 px-4 py-2 text-sm text-rose"
              >
                Cancel import
              </button>
            )}
            {!tracking && (
              <Dialog.Close className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg">
                Close
              </Dialog.Close>
            )}
            {!tracking && (
              <button
                type="button"
                onClick={() => void submit()}
                disabled={submitting || !path.trim() || !licenseName.trim()}
                className="rounded-md bg-action px-4 py-2 text-sm font-medium text-ink hover:bg-action-hi disabled:opacity-40"
              >
                {submitting ? "Starting…" : "Import"}
              </button>
            )}
            {tracking && !running && (
              <Dialog.Close className="rounded-md bg-action px-4 py-2 text-sm font-medium text-ink hover:bg-action-hi">
                Done
              </Dialog.Close>
            )}
            {tracking && running && (
              // Dialog.Close fires the root onOpenChange → reset() clears
              // the path; the job itself keeps running (jobs panel tracks it).
              <Dialog.Close className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg">
                Keep importing in background
              </Dialog.Close>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
