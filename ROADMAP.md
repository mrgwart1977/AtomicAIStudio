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
| M4.1 (Phase 1–2A) | Persistent worker infrastructure + real sd-server integration (no inference path yet) |
| C1 (in PR) | Provider Manager foundation — 60-entry catalog, DPAPI credential vault, settings UI, mock connectivity tests |
| Tooling | `scripts/validate.ps1` local validation, optimized CI (path-gating, caching, timeouts, concurrency) |

## In progress / next

| Item | Notes |
|---|---|
| CI reliability | cost-optimized workflow; then merge #55 → #57 → #53 → #56 |
| M4.1 Phase 2B | real sd-server inference path — blocked pending authorization |

## Planned (not implemented)

| Item | Notes |
|---|---|
| C1.5 | Import-only workflows: file-import jobs, `user_reported` provenance |
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
