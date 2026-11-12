// C1.5 bridge consumer tests — launch safety contract + honest display.
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { CapabilityEntry, CommitResult } from "./types";
import { bridgeOpenTarget, capabilityDisplay, openBridgeResult } from "./bridge";

const openUrl = vi.fn();
vi.mock("@tauri-apps/plugin-opener", () => ({ openUrl: (...a: unknown[]) => openUrl(...a) }));

function launchResult(url: string | null, over: Record<string, unknown> = {}): CommitResult {
  return {
    command_id: "c1", status: "committed", revision: 2,
    idempotent_replay: false, job_id: null, warnings: [],
    bridge: {
      action: "launch", provider_key: "p", strategy_id: "s", method: "assisted_workflow",
      url, open_via: "client_shell", ...over,
    },
  };
}

function cap(over: Partial<CapabilityEntry>): CapabilityEntry {
  return {
    capability: "image_generation", strategy_id: "s", method: "assisted_workflow",
    tier: "T2", status: "documented", stale: false,
    verification: "catalog_documented", dispatch: ["prepare", "launch", "import"],
    dispatch_blocked: null, required_user_action: "site_workflow",
    alternate: null, ...over,
  };
}

beforeEach(() => {
  openUrl.mockClear();
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});
afterEach(() => {
  delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
});

describe("bridgeOpenTarget — the only path a URL can reach the opener", () => {
  it("accepts a validated https launch response", () => {
    expect(bridgeOpenTarget(launchResult("https://midjourney.com"))).toBe("https://midjourney.com");
  });
  it.each(["http://x.com", "javascript:alert(1)", "file:///c:/x", "ftp://x", ""])(
    "rejects unsafe or non-https scheme: %s",
    (u) => expect(bridgeOpenTarget(launchResult(u))).toBeNull(),
  );
  it("rejects a missing url", () => {
    expect(bridgeOpenTarget(launchResult(null))).toBeNull();
  });
  it("rejects unless open_via is exactly client_shell", () => {
    expect(bridgeOpenTarget(launchResult("https://x.com", { open_via: "backend" }))).toBeNull();
    expect(bridgeOpenTarget(launchResult("https://x.com", { open_via: undefined }))).toBeNull();
  });
  it("rejects non-launch actions — prepare/import results never open", () => {
    expect(bridgeOpenTarget(launchResult("https://x.com", { action: "prepare" }))).toBeNull();
    expect(bridgeOpenTarget(launchResult("https://x.com", { action: "import" }))).toBeNull();
  });
  it("rejects when the response carries no bridge payload", () => {
    const r = launchResult("https://x.com");
    delete (r as { bridge?: unknown }).bridge;
    expect(bridgeOpenTarget(r)).toBeNull();
    expect(bridgeOpenTarget({ ...r, bridge: null })).toBeNull();
  });
});

describe("openBridgeResult — desktop-gated, gesture-only", () => {
  it("refuses outside the Tauri desktop — a web context can never spawn a browser", async () => {
    await expect(openBridgeResult(launchResult("https://x.com")))
      .rejects.toThrow(/requires the desktop/);
    expect(openUrl).not.toHaveBeenCalled();
  });
  it("opens the exact validated URL inside the desktop", async () => {
    (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const url = await openBridgeResult(launchResult("https://provider.example/app"));
    expect(url).toBe("https://provider.example/app");
    expect(openUrl).toHaveBeenCalledTimes(1);
    expect(openUrl).toHaveBeenCalledWith("https://provider.example/app");
  });
  it("never calls the opener for an invalid result — even inside the desktop", async () => {
    (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    expect(await openBridgeResult(launchResult("http://insecure.com"))).toBeNull();
    expect(await openBridgeResult(launchResult("https://x.com", { action: "import" }))).toBeNull();
    expect(openUrl).not.toHaveBeenCalled();
  });
});

describe("capabilityDisplay — honest labels, never implies operational", () => {
  it("marks documented dispatchable capabilities as unverified", () => {
    const d = capabilityDisplay(cap({}));
    expect(d.statusLabel).toMatch(/[Dd]ocumented/);
    expect(d.statusLabel).toMatch(/[Uu]nverified/);
    expect(d.actionLabel).toBe("Prepare & open provider site");
  });
  it("marks T3 import-only capabilities", () => {
    const d = capabilityDisplay(cap({ dispatch: ["import"], required_user_action: "file_import" }));
    expect(d.actionLabel).toBe("Import a file");
  });
  it("surfaces stale verification and needs_recheck", () => {
    for (const over of [
      { stale: true }, { status: "needs_recheck" },
    ] satisfies Partial<CapabilityEntry>[]) {
      const d = capabilityDisplay(cap(over));
      expect(d.statusClass).toBe("warn");
      expect(d.statusLabel).toMatch(/stale|re-check/i);
    }
  });
  it("marks prohibited strategies", () => {
    const d = capabilityDisplay(cap({
      status: "refused_prohibited", dispatch: [], dispatch_blocked: "prohibited",
      required_user_action: "none_available",
    }));
    expect(d.statusLabel).toMatch(/[Pp]rohibited/);
    expect(d.statusClass).toBe("muted");
  });
  it("T1 unimplemented is 'documented, not implemented' — never connected/operational", () => {
    const d = capabilityDisplay(cap({
      tier: "T1", method: "official_api", dispatch: [],
      dispatch_blocked: "t1_not_implemented", required_user_action: "none_available",
    }));
    expect(d.actionLabel).toMatch(/[Dd]ocumented/);
    expect(d.actionLabel).toMatch(/not implemented/i);
    for (const s of [d.statusLabel, d.actionLabel]) {
      expect(s).not.toMatch(/connected|operational|ready/i);
    }
  });
  it("T4 informational gets no action", () => {
    const d = capabilityDisplay(cap({
      tier: "T4", method: "external_info", dispatch: [],
      dispatch_blocked: "informational_only", required_user_action: "none_available",
    }));
    expect(d.actionLabel).toMatch(/[Ii]nformational/);
  });
});

describe("launch permission model — capability file is the narrowest https-only scope", () => {
  const caps = JSON.parse(readFileSync(
    resolve(import.meta.dirname, "../../src-tauri/capabilities/default.json"), "utf8"));
  const perms: unknown[] = caps.permissions;

  it("grants opener:allow-open-url scoped to explicit catalog hosts only", () => {
    const opener = perms.find((p) =>
      typeof p === "object" && p !== null &&
      (p as { identifier?: string }).identifier === "opener:allow-open-url") as
      { allow?: { url?: string }[] } | undefined;
    expect(opener).toBeDefined();
    expect(opener!.allow!.length).toBeGreaterThan(0);
    for (const a of opener!.allow!) {
      // Shape must be exactly https://host or https://host/** — the host
      // portion can never contain a wildcard (https://h.com** would also
      // match h.com.evil.com) and no bare https://* may exist.
      expect(a.url).toMatch(/^https:\/\/[a-z0-9.-]+(\/\*\*)?$/);
      expect(a.url).not.toBe("https://*");
    }
    // Representative catalog destinations must be reachable (exact + path).
    const urls = opener!.allow!.map((a) => a.url);
    expect(urls).toContain("https://www.midjourney.com");
    expect(urls).toContain("https://www.midjourney.com/**");
    expect(urls).toContain("https://suno.com");
    // A lookalike host must NOT be reachable — suffix-spoofing is excluded
    // by construction (no host** patterns anywhere).
    expect(urls).not.toContain("https://midjourney.com.evil.example/**");
  });
  it("grants no command-execution or file-path permissions", () => {
    const ids = perms.map((p) =>
      typeof p === "string" ? p : (p as { identifier?: string }).identifier);
    for (const id of ids) {
      expect(id).not.toMatch(/execute|shell:|open-path|reveal/);
    }
  });
  it("no unrestricted opener permission exists alongside the scoped one", () => {
    const plain = perms.filter((p) => typeof p === "string" && /opener/.test(p));
    expect(plain).toHaveLength(0); // scoped object form only
  });
});
