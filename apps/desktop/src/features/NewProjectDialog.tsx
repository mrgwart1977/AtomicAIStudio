import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { apiFetch } from "../api/client";
import type { Project } from "../api/types";
import { useApp } from "../stores/appStore";

const TEMPLATES = [
  { id: "blank", label: "Blank project" },
  { id: "short_film", label: "Short film" },
  { id: "series", label: "Series / episodic" },
  { id: "music_video", label: "Music video" },
] as const;

export function NewProjectDialog({
  open,
  onOpenChange,
  defaultTemplate = "blank",
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  defaultTemplate?: string;
}) {
  const [name, setName] = useState("");
  const [template, setTemplate] = useState<string>(defaultTemplate);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refresh = useApp((s) => s.refreshProjects);
  const setProject = useApp((s) => s.setCurrentProject);
  const setPage = useApp((s) => s.setPage);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const project = await apiFetch<Project>("/api/projects", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), template }),
      });
      await refresh();
      setProject(project.id);
      onOpenChange(false);
      setPage("projects");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60" />
        <Dialog.Content className="fixed left-1/2 top-1/2 w-[420px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line bg-panel p-5 shadow-xl">
          <Dialog.Title className="text-lg font-semibold">New project</Dialog.Title>
          <label className="mt-4 block text-sm text-muted">
            Name
            <input
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My film"
              className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg"
            />
          </label>
          <label className="mt-3 block text-sm text-muted">
            Template
            <select
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg"
            >
              {TEMPLATES.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          {error && <p className="mt-3 text-sm text-danger">{error}</p>}
          <div className="mt-5 flex justify-end gap-2">
            <Dialog.Close className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg">
              Cancel
            </Dialog.Close>
            <button
              onClick={create}
              disabled={busy || !name.trim()}
              className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
            >
              {busy ? "Creating…" : "Create project"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
