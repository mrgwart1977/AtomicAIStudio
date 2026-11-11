// C1.5 access-bridge desktop consumer.
//
// bridge.launch is deliberately split (PR #72): the backend command validates
// the catalog destination + audits the request, then returns the URL with
// open_via="client_shell". This module is the ONLY place a browser open can
// happen — invoked solely from explicit user-gesture handlers. The backend
// contains no browser-spawn mechanism at all.
import type { BridgeResult, CapabilityEntry, CommitResult } from "./types";

/** Extract the launch destination a commit response authorizes us to open.
 *  Returns null unless ALL of: action==="launch", open_via==="client_shell",
 *  url present, https:// scheme. Anything else is never opened. */
export function bridgeOpenTarget(result: CommitResult): string | null {
  const b = result.bridge as BridgeResult | null | undefined;
  if (!b || b.action !== "launch" || b.open_via !== "client_shell") return null;
  const url = b.url ?? "";
  return url.startsWith("https://") ? url : null;
}

/** Open a bridge.launch destination in the system browser.
 *  MUST be called from a direct user-gesture handler only (spec §6/§9).
 *  Throws outside the Tauri desktop — the browser path is web-only
 *  (no navigator.open fallback; a silent new-tab is not the contract). */
export async function openBridgeResult(result: CommitResult): Promise<string | null> {
  const url = bridgeOpenTarget(result);
  if (!url) return null;
  if (!(window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__) {
    throw new Error("Bridge site launch requires the desktop app");
  }
  const { openUrl } = await import("@tauri-apps/plugin-opener");
  await openUrl(url);
  return url;
}

// ---- capability report → honest display mapping ----

export interface CapDisplay {
  statusLabel: string;
  statusClass: string; // badge | warn | muted
  actionLabel: string | null;
}

const BLOCKED_LABEL: Record<string, string> = {
  t1_not_implemented: "Documented — direct API not implemented",
  informational_only: "Informational record only",
  prohibited: "Not permitted by provider terms",
  stale: "Re-check verification",
};

/** What the report says the user can do next — honest, never "connected". */
export function capabilityDisplay(c: CapabilityEntry): CapDisplay {
  let actionLabel: string | null = null;
  if (c.dispatch.includes("launch")) actionLabel = "Prepare & open provider site";
  else if (c.dispatch.includes("import")) actionLabel = "Import a file";
  else if (c.dispatch_blocked) actionLabel = BLOCKED_LABEL[c.dispatch_blocked] ?? null;

  if (c.status === "refused_prohibited" || c.dispatch_blocked === "prohibited")
    return { statusLabel: "Prohibited by provider terms", statusClass: "muted", actionLabel };
  if (c.stale || c.status === "needs_recheck")
    return {
      statusLabel: "Verification stale — re-check required",
      statusClass: "warn",
      actionLabel,
    };
  // catalog_documented ≠ operational: the label must say so.
  return { statusLabel: "Documented (unverified)", statusClass: "badge", actionLabel };
}
