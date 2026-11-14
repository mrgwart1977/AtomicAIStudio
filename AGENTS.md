# Agent notes — Atomic Dream Engine

## Commands
- Backend tests: `cd backend && .venv\Scripts\python -m pytest tests -q`
- Backend dev server: `scripts\dev-backend.ps1` (port 8788, token `dev`)
- UI build: `cd apps\desktop && npm run build` (tsc + vite)
- Full dev app: `cd apps\desktop && npm run tauri dev` (Rust at `%USERPROFILE%\.cargo\bin`)
- Installer: `scripts\build-sidecar.ps1` then `npm run tauri build` → NSIS in `src-tauri\target\release\bundle\nsis\`
- FFmpeg on this machine: `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\` (not on PATH)

## Conventions
- Spec of record: `docs/handoff/` (v0.9). Amendments extend, never silently remove.
- All project-owned rows carry `project_id` FK; isolation enforced in DB+API, not UI.
- Every mutation goes through the command system (preview → commit → audit).
- Jobs are durable rows in `generation_jobs`; idempotent via `idempotency_key`.
- Mock adapters are labeled `adapter_kind="mock"` — never present them as inference.
- Backend session token: `X-Atomic-Token` header only — `?token=` was removed in A3; the WS authenticates via a first-frame `{"type":"auth","token":...}` message.
- No model downloads, cloud calls, or paid work without explicit owner approval.
- C3-D2 img2img: `generate.image.img2img` → `cloud.image.img2img.sim`; the provider's `EndpointSpec.edit_path` is the implemented-contract gate — catalog `editing.img2img` alone never enables the mode.
- C3-D3 edit: `generate.image.edit` (`operation: inpaint|outpaint`) → `cloud.image.edit.sim`; gates on `EndpointSpec.inpaint_path`/`outpaint_path`. Mask convention in `app/domain/image_edit.py` — exact source-dims match (never resized), luminance ≥128 = editable; outpaint is expansion-only ≤4096 px with a documented placement anchor.

## Milestone state
- M0 + M0.1 approved and merged in PR #2; **M1 implemented** on `feat/m1-projects` (project mgmt, bibles, CAS storage, backups, workspace UI).
- New M1 command types: `project.{rename,archive,restore,update_settings,duplicate,delete}`, `{character,world,location}.{create,update,archive,unarchive,revert}`, `asset.{import,share,link,relink}`, `assets.verify`, `backup.{export,import,restore}` — all via `/commands/commit` only.
- Screenshots: `scripts/cdp-shots.cjs` (Edge on :9222 → `docs/m1-evidence/`); window capture `scripts/capture-window.ps1` uses PrintWindow (required for WebView2).
- Zustand rule: never return `?? []` / `?? {}` / `.map/.filter` inside `useApp` selectors — new ref each call = infinite re-render. Select the stable field, derive outside.
- Gotchas: Git Bash mangles `$_` and `/S` in PowerShell one-liners — use files or `//S`; headless Edge `--dump-dom` is unreliable here, use `scripts/cdp-diag.cjs`.
- M2 requires owner approval.
