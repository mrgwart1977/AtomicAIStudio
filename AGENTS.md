# Agent notes — Atomic AI Studio

## Commands
- Backend tests: `cd backend && .venv\Scripts\python -m pytest tests -q`
- Backend dev server: `scripts\dev-backend.ps1` (port 8788, token `dev`)
- UI build: `cd apps\desktop && npm run build` (tsc + vite)
- Full dev app: `cd apps\desktop && npm run tauri dev` (Rust at `%USERPROFILE%\.cargo\bin`)
- FFmpeg on this machine: `%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\` (not on PATH)

## Conventions
- Spec of record: `docs/handoff/` (v0.9). Amendments extend, never silently remove.
- All project-owned rows carry `project_id` FK; isolation enforced in DB+API, not UI.
- Every mutation goes through the command system (preview → commit → audit).
- Jobs are durable rows in `generation_jobs`; idempotent via `idempotency_key`.
- Mock adapters are labeled `adapter_kind="mock"` — never present them as inference.
- Backend session token: `X-Atomic-Token` header (or `?token=` for WS/file URLs).
- No model downloads, cloud calls, or paid work without explicit owner approval.

## Milestone state
- M0 (foundation) implemented; M1 not started — requires owner approval.
