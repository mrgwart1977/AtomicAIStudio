import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";

export function RenameDialog({
  open,
  onOpenChange,
  title,
  initialValue,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  initialValue: string;
  onSubmit: (name: string) => void;
}) {
  const [value, setValue] = useState(initialValue);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/60" />
        <Dialog.Content className="fixed left-1/2 top-1/2 w-[380px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-line bg-panel p-5 shadow-xl">
          <Dialog.Title className="text-lg font-semibold">{title}</Dialog.Title>
          <input
            autoFocus
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && value.trim()) {
                onSubmit(value.trim());
                onOpenChange(false);
              }
            }}
            className="mt-4 w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg"
          />
          <div className="mt-5 flex justify-end gap-2">
            <Dialog.Close className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg">
              Cancel
            </Dialog.Close>
            <button
              onClick={() => {
                if (value.trim()) {
                  onSubmit(value.trim());
                  onOpenChange(false);
                }
              }}
              disabled={!value.trim()}
              className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
            >
              Rename
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
