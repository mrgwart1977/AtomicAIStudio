import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";

/**
 * Destructive-action confirmation. For irreversible operations pass
 * `requireText` — the confirm button stays disabled until the user types
 * it exactly (e.g. the project name before deletion).
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  body,
  confirmLabel = "Confirm",
  requireText,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  body: string;
  confirmLabel?: string;
  requireText?: string;
  onConfirm: () => void;
}) {
  const [typed, setTyped] = useState("");
  const allowed = requireText ? typed.trim() === requireText : true;

  return (
    <Dialog.Root open={open} onOpenChange={(o) => { setTyped(""); onOpenChange(o); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60" />
        <Dialog.Content className="fixed left-1/2 top-1/2 w-[420px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line bg-panel p-5 shadow-xl">
          <Dialog.Title className="text-lg font-semibold">{title}</Dialog.Title>
          <p className="mt-2 text-sm text-muted">{body}</p>
          {requireText && (
            <label className="mt-4 block text-sm text-muted">
              Type <span className="font-semibold text-fg">{requireText}</span> to confirm
              <input
                autoFocus
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                className="mt-1 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg"
              />
            </label>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <Dialog.Close className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg">
              Cancel
            </Dialog.Close>
            <button
              onClick={() => {
                if (allowed) {
                  onConfirm();
                  onOpenChange(false);
                }
              }}
              disabled={!allowed}
              className="rounded-md border border-danger/60 bg-danger/15 px-4 py-2 text-sm font-medium text-danger hover:bg-danger/25 disabled:opacity-40"
            >
              {confirmLabel}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
