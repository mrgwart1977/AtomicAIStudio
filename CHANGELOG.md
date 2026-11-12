# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses semver once public releases begin.

## [Unreleased]

### Added
- C1.5 ImportWatcher Stage B — opt-in background polling: a single
  app-level `ImportWatchService` runs one shared asyncio task (default
  60 s interval, 30 s floor) over providers whose persisted
  `settings.import_behavior == "poll"`. Opt-in only via audited
  `bridge.watch.enable` / `bridge.watch.disable` commands (enable
  requires a currently-valid designated folder). All filesystem work
  runs on one worker thread off the event loop; one scan per provider in
  flight — a still-running scan is never overlapped. Each poll
  re-validates the ORIGINAL designated folder with the same Stage A
  logic (symlink/junction/traversal rejection, containment, extension
  gating, entry/candidate bounds); an invalid folder marks the watcher
  `degraded` with a reason, stops scanning it, and only resumes when
  that same folder validates again — never silently re-anchors. Polling
  pauses under critical memory pressure and while a `gen.*` inference
  job runs. Candidates live in a bounded in-memory cache (200/provider,
  2000 global, newest-first eviction) surfaced read-only via
  `GET /api/providers/{key}/watch` with per-project dedup; they can
  never become assets without an explicit per-file `bridge.import`.
  Polling ticks write no durable audit rows; enable/disable are audited
  commands carrying no absolute paths. Every pending scan is tagged
  with its session's (generation, folder fingerprint): disable,
  re-enable, or folder redesignation orphans the in-flight scan —
  tracked to completion but its results are discarded and can never
  publish into a newer session's cache. Shutdown cancels the task,
  waits a bounded grace (5 s) for in-flight scans, cancels queued work,
  and lets any wedged filesystem call finish on the daemon worker
  thread — entry/candidate bounds limit work items, not wall-clock,
  and a stalled syscall is documented as beyond cooperative
  cancellation. The Providers page refreshes watch status every ~12 s
  while auto-check is enabled (read-only GET, one request in flight,
  never triggers a scan, silent on transient backend errors). Still
  stat-only, non-recursive, and never auto-imports.
- C1.5 ImportWatcher Stage A — manual candidate scanning: providers can
  designate an `import_dir` in settings; `bridge.watch.scan` performs an
  audited, bounded, non-recursive, stat-only enumeration (never reads
  file contents, rejects symlink/junction/traversal paths, skips
  anything resolving outside the folder, dedupes already-imported
  files). Candidates display with "provider origin unverified" labeling
  and import only via per-file explicit clicks through `bridge.import`
  (`from_watch` revalidates against the live filesystem at import time —
  symlink swaps, deletions, folder escapes, and ineligible extensions are
  re-rejected). Folder (re)designation writes a path-minimized app-level
  audit event; scan audit records carry candidate counts, never paths.
  Background polling (Stage B) is not implemented — no watch loop exists.
- C1.5 Bridge UI + desktop integration: Settings → AI Providers now shows
  the per-feature capability report (`catalog_documented` honesty labels,
  stale/`needs_recheck` warnings, required user actions, prohibited and
  T1-unimplemented states) and the canonical T2 assisted workflow —
  prepare-and-copy prompt, validated site link with exact destination
  display, explicit click-to-open via the Tauri Opener plugin
  (`opener:allow-open-url` scoped to `https://*`, no execute/path
  permissions), and file/clipboard import with user-reported external
  provenance. Browser open is desktop-only, gesture-only; the backend
  remains side-effect-free.
- C1.5 Access Bridge dispatch + capability reporting: `GET
  /api/providers/{key}/capabilities` (per-feature tier/method/strategy,
  `catalog_documented` verification label, dispatch vs blocked actions),
  and `bridge.prepare`/`bridge.launch`/`bridge.import` commands through
  preview → commit → audit. Prepare renders prompt templates for manual
  paste; launch opens only validated catalog HTTPS URLs in the user's
  browser; import routes user-selected files/clipboard text through the
  `external_source` provenance path. Prohibited/stale/T1 strategies are
  refused — no programmatic provider calls, no browser automation.
- C1.5 Access Bridge foundation: enriched AccessStrategy records across
  the 60-provider catalog (strategy_id, ToS evidence + verification
  dates, auth/cost/reliability), catalog integrity validation,
  staleness demotion (`needs_recheck`, enable blocked while stale),
  `cloud-assisted`/`cloud-external` adapter kinds, and `external_source`
  provenance tagging on asset import (`origin=external`,
  `provenance_confidence=user_reported` — `verified_remote` stays
  reserved for T1 remote job ids). No live provider contact.
- Provider Manager foundation (C1): 60-entry catalog, DPAPI credential
  vault, Settings → AI Providers UI, project overrides — **management
  foundation only; no live cloud generation**
- `scripts/validate.ps1`: local validation entry point (backend on
  Python 3.12 + 3.14, frontend build, optional Tauri/package checks)
- Optimized CI: path-gated jobs, timeouts, concurrency cancellation,
  dependency + FFmpeg caching, Linux frontend job
- Open-source preparation: Apache-2.0 LICENSE, third-party notices,
  contributing/conduct/security docs, issue templates

### Fixed
- Worker shutdown lifecycle: reservations release only after confirmed
  OS process exit (#51/#52)
- Windows process liveness: signaled-object check fixes
  `GetExitCodeProcess` STILL_ACTIVE race (#54)
- CI executor-thread starvation on unkillable workers (poll-based waits)

## Notes

Historical milestone detail lives in `docs/plans/` and `docs/*-evidence/`.
A public release history starts at `v0.1.0`.
