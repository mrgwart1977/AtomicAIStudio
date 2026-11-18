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
| C1.5 slice 4 | ImportWatcher: `import_dir` designation (path-minimized audit), `bridge.watch.scan` (bounded non-recursive stat-only), `from_watch` import revalidation, Stage B opt-in polling — `ImportWatchService` single task + daemon worker, `bridge.watch.enable`/`.disable` audited opt-in, gen/fingerprint stale-result guard, degraded/paused states, `GET /api/providers/{k}/watch` |
| Tooling | `scripts/validate.ps1` local validation, optimized CI (path-gating, caching, timeouts, concurrency) |
| A1 (Phase A) | Integrated screenplay + deterministic shot-list scaffold — ordered scenes/elements with speaker/location refs, beat→shot sync (manual-edit preservation, approval-gated destructive apply), stale-safe linear undo/redo — merged `7464d61` |
| A2 (Phase A) | Shot planning + storyboard board — `planning_json` (all 10 spec follow modes, framing/placement/lighting, rational fps, continuity refs), shot-scoped CAS media links with missing-media flags, drag/drop + keyboard reorder, duplicate, bulk-select archive, screenplay-sync preservation — merged `30a16a8` |
| A3 (Phase A) | Shot keyframes + mock image generation — durable `keyframe.generate` jobs (hold-barrier deterministic cancel), continuity-aware compose drafts with user-prompt ownership, stale-result pins (`expected_keyframe`/`expected_prompt`/`expected_result_revision`/`expected_planning_sha`), CAS result landing via audited `keyframe.result`, pin/archive/undo, mock provenance — merged `8fc1910` |
| A4 (Phase A) | Motion clips — shot-bound keyframe→video beats via `mock.video` (real H.264 encode + real ffprobe metadata, deterministic hold barrier, source-keyframe + planning + revision stale pins), timeline clip placement with rational fps conversion, export integration, backup v5, installed acceptance 15/15 — merged `9a7c0723` |
| A5 (Phase A) | Dialogue segments + mock audio — speaker-bound lines (screenplay scene/element + storyboard scene/shot refs, SET NULL unlink keeps text), character voice-profile resolution with missing-voice flags, deterministic `mock.audio` WAV (real PCM `pcm_s16le`, ffprobe + decode-check, hold-barrier cancel, no temp leaks), stale pins on segment authored fields + speaker/voice + element/parenthetical + scene/shot state (`result_stale` flag, screenplay unlink warnings), audio-lane placement via existing timeline geometry + PCM parity, backup v6, installed acceptance 17/17 — merged `ad1013a` |

## In progress / next

| Item | Notes |
|---|---|
| M4.1 first real inference | code merged; blocked on host RAM ≥8 GiB target — single-job test awaits re-preflight + owner authorization |

## Planned (not implemented)

| Item | Notes |
|---|---|
| C1.5 slice 5a | §5 CLI detection Phase 1 (merged): catalog `cli{}` metadata for documented CLIs only (openai/claude/gemini), audited `bridge.cli.detect` — PATH + declared-dir lookup, no execution/subprocess/version probing, suffix + ancestor reparse validation, `verified: false` labeling, path-minimized audit |
| C1.5 remaining | §5 follow-ups — CLI operational integration (execution/version probing — **not yet authorized**), additional documented-CLI catalog entries, plugin surfaces; `verified_remote` provenance for T1 remote job ids (blocked on C2+ T1 dispatch) |
| C2–C7 | Provider connectivity phases: prompt preparation, site launchers (user-driven), MCP connector clients for T1 providers — all behind consent + mock-first testing |
| Audio workflows | generation + tracks |
| Editing timeline | assembly of generated clips |
| LTX integration | pending runtime maturity |
| A6 (Phase A) | First-cut assembly — deterministic `assembly.plan` proposals from ordered scenes/shots + motion clips + dialogue + shot-linked audio (V1/DX/MX lanes with deterministic overflow), per-item review + notes, non-destructive `assembly.apply` riding `timeline.insert_clip` with collapsed undo + pinned source fingerprints (stale apply refuses; result-asset liveness fingerprinted), manual-edit preservation with per-item `replace_keys` approval (broad replace refused), backup v7 (timeline EDL + plans/items), `AssemblyPanel` UI, installed acceptance 19/19 — merged `fa23db9` |
| A7 (Phase A) | Multi-GPU scheduling + cloud gates — per-device VRAM reservation accounting (`_reserved` map; unknown footprints claim devices exclusively, never pool VRAM), `requires` backend/vendor matching, lost-device reporting, balanced spread admission, FIFO-skip queueing (big waiters can't starve small jobs), atomic check-then-claim (RLock) + per-job queue reasons, `mock.gpu.sim` simulated worker exercising real claim/cancel/crash/restart paths, `cloud.plan` provider-neutral approval (capability + credential presence + cost + budget + availability) with `plan_fingerprint` pinned into cloud submits and `would_admit` re-verified at commit, queued-cancel spend-hold release, `ATOMIC_HW_JSON` simulated hardware hook (env-only, always flagged `simulated`, fail-safe to real probe), `reserved_vram_bytes` job column (migration `0026_a7`), `resolved_gpu`/`resolved_cloud` preview surfaces, ResourceSection reservation + simulated-device view, CloudPage/CloudImagePanel plan-gated approvals — 46 backend + 409 frontend tests, installed acceptance 17/17 — merged `5d8129c` |
| A8 (Phase A) | Filmmaking wizard — 15-stage guided flow (project → film settings → screenplay → scenes/dialogue → cast/continuity → shot scaffold → planning incl. all ten follow modes → keyframes → motion clips → dialogue/audio → first-cut plan → review/apply → edit → export config → export+verify) implemented as a thin orchestration layer over A1–A7 commands with zero parallel persistence; `GET /wizard` derives stage truth + generation-state honesty labels (planned/draft/simulated/generated/missing/failed/cancelled/stale/approved) from live rows; progress persists in `settings_json.wizard` via `project.update_settings` (legacy `first_film` blobs seed v2 with a notice); Beginner/Advanced are view modes over the same data; screenplay paste-import limited to the documented plain-text format — **merged** in PR #155 (`8fbdf96`, reviewed head `a3161f9`); 34/34 installed acceptance on `4e6fda3`; backup-import settings sanitizer hardened post-review (provider overrides stripped at the package boundary) |
| Multi-model image/video backends | per spec |

## Explicit non-goals for now

- Live cloud generation calls of any kind
- Paid AI API usage inside CI or tests
- Browser automation against provider sites
- Bundling or redistributing model weights

*Cloud-provider catalog entries describe capability tiers (T1 API /
T2 assisted / T3 manual / T4 external-only) — they are not claims of
working integrations.*
