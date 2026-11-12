# Roadmap

Honest status. "Done" = implemented, tested, merged. "In progress" =
active PR(s). "Planned" = designed or scoped, not implemented.

## Completed

| Milestone | Content |
|---|---|
| M0 / M0.1 | Tauri 2 + FastAPI sidecar foundation, hardware detection, compatibility adapters, SQLite migrations, project isolation, PyInstaller backend, NSIS installer |
| M1 / M1.1 | Projects/dashboard, versioned bibles, CAS asset store, `.atomicpkg` backup/restore, durable jobs, workspace UI |
| M2 | Scenes/shots/casting, visual storyboard, command/audit/undo |
| M3 | Local image generation — SD 1.5 fp16 GGUF via stable-diffusion.cpp (Vulkan/CUDA), approved model downloads w/ integrity checks, provenance, cancellation |
| M4.0 | Local video — Wan 2.1 T2V 1.3B fp16, approved component downloads, supervised workers, admission control, recovery |
| M4.0.1 | Production FFmpeg media engine (pinned BtbN LGPL shared build) |
| M4.1 (Phase 1–2B) | Persistent sd-server worker path (contract, `-m` preload, first-run watchdog incl. PID-identity kills, enforced gates) — code complete; first real image run pending host RAM (see docs/first-run-watchdog.md) |
| C1 | Provider Manager foundation — 60-entry catalog, DPAPI credential vault, settings UI, mock connectivity tests |
| C1.5 foundation | Strategy evidence records (strategy_id, ToS evidence + checked dates, auth/cost/reliability), `validate_catalog()`, staleness demotion → `needs_recheck` + enable-block, `cloud-assisted`/`cloud-external` adapter kinds, `external_source` import provenance (`user_reported`) |
| C1.5 slice 2 | Bridge dispatch (`bridge.prepare`/`launch`/`import` commands — user-driven only, no programmatic calls, no browser automation) + `GET /api/providers/{key}/capabilities` per-feature report (`catalog_documented` honesty label; T1 always `t1_not_implemented`) |
| C1.5 slice 3 | Bridge UI + desktop integration: Tauri Opener launch consumer (host-scoped `opener:allow-open-url`, 47 catalog hosts, sync-tested), Settings → Providers capability report + canonical T2 assisted workflow (prepare→copy→confirm→open→import), gesture-only clipboard — see docs/plans/C1_5_ACCESS_BRIDGE.md |
| Tooling | `scripts/validate.ps1` local validation, optimized CI (path-gating, caching, timeouts, concurrency) |

## In progress / next

| Item | Notes |
|---|---|
| M4.1 first real inference | code merged; blocked on host RAM ≥8 GiB target — single-job test awaits re-preflight + owner authorization |

## Planned (not implemented)

| Item | Notes |
|---|---|
| C1.5 remaining | ImportWatcher folder watching (opt-in, candidate-only, confirmation-gated); §5 desktop CLI/plugin integrations; `verified_remote` provenance for T1 remote job ids (blocked on C2+ T1 dispatch) |
| C2–C7 | Provider connectivity phases: prompt preparation, site launchers (user-driven), MCP connector clients for T1 providers — all behind consent + mock-first testing |
| Audio workflows | generation + tracks |
| Editing timeline | assembly of generated clips |
| LTX integration | pending runtime maturity |
| Multi-model image/video backends | per spec |

## Explicit non-goals for now

- Live cloud generation calls of any kind
- Paid AI API usage inside CI or tests
- Browser automation against provider sites
- Bundling or redistributing model weights

*Cloud-provider catalog entries describe capability tiers (T1 API /
T2 assisted / T3 manual / T4 external-only) — they are not claims of
working integrations.*
