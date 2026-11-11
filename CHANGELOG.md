# Changelog

All notable changes to this project will be documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses semver once public releases begin.

## [Unreleased]

### Added
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
