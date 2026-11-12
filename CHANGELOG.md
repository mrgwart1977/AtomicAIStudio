# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses semver once public releases begin.

## [Unreleased]

### Added
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
