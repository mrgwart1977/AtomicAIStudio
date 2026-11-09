import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { apiFetch } from "../api/client";
import type { CommitResult, CommandProposal } from "../api/types";
import { useApp } from "../stores/appStore";

function uuid(): string {
  return crypto.randomUUID();
}

export function PromptBar() {
  const project = useApp((s) => s.projects.find((p) => p.id === s.currentProjectId));
  const draft = useApp((s) => s.promptDraft);
  const setDraft = useApp((s) => s.setPromptDraft);
  const refreshProjects = useApp((s) => s.refreshProjects);
  const refreshProjectData = useApp((s) => s.refreshProjectData);

  const [proposal, setProposal] = useState<CommandProposal | null>(null);
  const [pendingPayload, setPendingPayload] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const disabled = !project || project.status !== "active";

  async function review() {
    if (!project) return;
    setBusy(true);
    setError(null);
    try {
      // Persist the text first — drafts survive mode switches and restarts.
      await apiFetch(`/api/projects/${project.id}/commands/commit`, {
        method: "POST",
        body: JSON.stringify({
          command_type: "prompt.set_text",
          payload: { text: draft },
          scope: "this_take",
          origin: "prompt_bar",
          expected_revision: project.revision,
          idempotency_key: `prompt-${uuid()}`,
        }),
      });
      await refreshProjects();
      const rev = useApp.getState().projects.find((p) => p.id === project.id)?.revision ?? project.revision;
      const prop = await apiFetch<CommandProposal>(`/api/projects/${project.id}/commands/preview`, {
        method: "POST",
        body: JSON.stringify({
          command_type: "generate.image.mock",
          payload: { prompt: draft },
          scope: "this_take",
          origin: "prompt_bar",
        }),
      });
      setPendingPayload(draft);
      setProposal({ ...prop, current_revision: rev });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!project || !proposal) return;
    setBusy(true);
    setError(null);
    try {
      const res = await apiFetch<CommitResult>(`/api/projects/${project.id}/commands/commit`, {
        method: "POST",
        body: JSON.stringify({
          command_type: "generate.image.mock",
          payload: { prompt: pendingPayload },
          scope: "this_take",
          origin: "prompt_bar",
          expected_revision: proposal.current_revision,
          idempotency_key: `gen-${uuid()}`,
        }),
      });
      void res;
      setProposal(null);
      await Promise.all([refreshProjects(), refreshProjectData()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-t border-line bg-panel px-6 py-3">
      <div className="mb-1 text-xs font-semibold tracking-wide text-teal">
        PROMPT • Editable in every workspace
      </div>
      <div className="flex items-end gap-3">
        <div className="flex-1">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={2}
            placeholder={
              disabled ? "Open or create a project to start…" : "Tell me what you want to create or change…"
            }
            disabled={disabled}
            className="w-full resize-none rounded-md border border-line bg-card px-3 py-2 text-sm text-fg placeholder:text-faint disabled:opacity-50"
            aria-label="Prompt"
          />
          <div className="mt-1 flex items-center gap-2">
            <span className="rounded-full border border-line px-2.5 py-0.5 text-xs text-muted">
              Scope: this take
            </span>
            <span className="text-xs text-faint">Broader changes always ask first</span>
          </div>
        </div>
        <button
          onClick={review}
          disabled={disabled || busy || !draft.trim()}
          className="rounded-md bg-action px-4 py-2.5 text-sm font-medium text-white hover:bg-action-hi disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? "Working…" : "Review request"}
        </button>
      </div>
      {error && <p className="mt-2 text-sm text-danger">{error}</p>}

      <Dialog.Root open={proposal !== null} onOpenChange={(o) => !o && setProposal(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60" />
          <Dialog.Content
            className="fixed left-1/2 top-1/2 w-[480px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line bg-panel p-5 shadow-xl"
            aria-describedby="proposal-desc"
          >
            <Dialog.Title className="text-lg font-semibold">Proposed action</Dialog.Title>
            <p id="proposal-desc" className="mt-1 text-sm text-muted">
              {proposal?.summary}
            </p>
            {proposal && proposal.warnings.length > 0 && (
              <ul className="mt-3 space-y-1">
                {proposal.warnings.map((w, i) => (
                  <li key={i} className="flex gap-2 text-sm text-amber">
                    <span aria-hidden>⚠</span>
                    {w}
                  </li>
                ))}
              </ul>
            )}
            <dl className="mt-3 grid grid-cols-2 gap-2 rounded-md border border-line bg-card p-3 text-xs text-muted">
              <dt>Scope</dt>
              <dd className="text-fg">this take only</dd>
              <dt>Compute</dt>
              <dd className="text-fg">{proposal?.estimated_cost.compute}</dd>
              <dt>Cost</dt>
              <dd className="text-fg">${proposal?.estimated_cost.currency_usd.toFixed(2)}</dd>
              <dt>Launches a job</dt>
              <dd className="text-fg">{proposal?.launches_generation ? "Yes — after approval" : "No"}</dd>
            </dl>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setProposal(null)}
                className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg"
              >
                Cancel
              </button>
              <button
                onClick={approve}
                disabled={busy}
                className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
              >
                {busy ? "Submitting…" : "Approve & run"}
              </button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
