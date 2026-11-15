import { apiFetch } from "./client";
import { useApp } from "../stores/appStore";
import type { CommitResult, CommandProposal } from "./types";

/** Single entry point for ALL project mutations — the Unified Creative
 * Command path. No direct POSTs to project-owned resources exist by design. */
export async function commitCommand(
  projectId: string,
  revision: number,
  commandType: string,
  payload: Record<string, unknown>,
  origin = "ui",
): Promise<CommitResult> {
  const r = await apiFetch<CommitResult>(
    `/api/projects/${projectId}/commands/commit`,
    {
      method: "POST",
      body: JSON.stringify({
        command_type: commandType,
        payload,
        scope: "project",
        origin,
        expected_revision: revision,
        idempotency_key: `${commandType}-${crypto.randomUUID()}`,
      }),
    },
  );
  // Feed the post-commit revision into the store — a handler issuing
  // sequential commits must pin the next one against it (without this,
  // the second commit deterministically 422s on a stale pin).
  useApp.getState().applyCommitRevision(projectId, r.revision);
  return r;
}

export async function previewCommand(
  projectId: string,
  commandType: string,
  payload: Record<string, unknown>,
): Promise<CommandProposal> {
  return apiFetch<CommandProposal>(`/api/projects/${projectId}/commands/preview`, {
    method: "POST",
    body: JSON.stringify({ command_type: commandType, payload, scope: "project", origin: "ui" }),
  });
}

/** Pick a file via the native dialog inside Tauri; returns null in plain
 * browser dev so callers can fall back to a text input. */
export async function pickFile(title: string, filters?: { name: string; extensions: string[] }[]): Promise<string | null> {
  if (!(window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const sel = await open({ title, multiple: false, directory: false, filters });
  return typeof sel === "string" ? sel : null;
}

export async function pickDirectory(title: string): Promise<string | null> {
  if (!(window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const sel = await open({ title, multiple: false, directory: true });
  return typeof sel === "string" ? sel : null;
}
