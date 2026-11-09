# Atomic AI Studio

A modular, offline-first Windows desktop application for planning, generating,
editing, and finishing AI-assisted films. Windows executable: `AtomicAIStudio.exe`.

Status: **Milestone 0 — foundation**. The specification lives in
[`docs/handoff/`](docs/handoff/README.md) (v0.9); the accepted roadmap is in
[`docs/plans/ARCHITECTURAL_REVIEW_AND_ROADMAP.md`](docs/plans/ARCHITECTURAL_REVIEW_AND_ROADMAP.md).

## Architecture (M0)

```
┌────────────────────────────────────────────────────────┐
│ Tauri 2 shell (Rust)  ── spawns/stops sidecar ──┐      │
│ React 19 + TS + Tailwind/Radix + Zustand        │      │
│        │ HTTP + WS (loopback, session token)    ▼      │
│        └──────────► FastAPI backend (Python 3.12+)     │
│                     SQLAlchemy 2 + SQLite(WAL) + Alembic│
│                     Job engine + adapter registry       │
│                     (mock adapters only in M0)          │
└────────────────────────────────────────────────────────┘
```

- `apps/desktop` — Tauri shell (`src-tauri/`) + React UI (`src/`)
- `backend` — FastAPI sidecar: `app/` (api, domain, db, adapters) + `tests/`
- `packages/contracts` — typed API contract notes (OpenAPI → TS)
- `docs` — handoff spec, architecture review, plans
- `scripts` — dev entry points

## Dev prerequisites

| Tool | Version used | License |
|---|---|---|
| Node.js | ≥ 20 (26 dev) | MIT |
| Python | ≥ 3.12 (3.14 dev) | PSF |
| Rust (rustup, MSVC toolchain) | 1.99 stable | MIT/Apache-2.0 |
| VS 2022 Build Tools (C++ workload) | 17.14 | Microsoft (free for OSS dev) |
| FFmpeg (for media features) | 9.0.2 | LGPL/GPL — see ffmpeg.org/legal |

Install: `winget install Rustlang.Rustup Microsoft.VisualStudio.2022.BuildTools Gyan.FFmpeg`
(Build Tools needs the VCTools workload for Rust's MSVC linker.)

## Run it

```powershell
# one-time
cd backend && python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"
cd apps\desktop && npm install

# full desktop app (shell spawns the backend sidecar itself)
npm run tauri dev          # in apps/desktop

# or browser-only dev (two terminals):
scripts\dev-backend.ps1  # backend on :8788, token "dev"
scripts\dev-ui.ps1       # vite on :1420
```

## Test

```powershell
cd backend && .venv\Scripts\python -m pytest tests -q   # 19 tests
cd apps\desktop && npm run build                        # typecheck + bundle
```

## What M0 does (and does not do)

**Works:** desktop shell with managed backend lifecycle; loopback API with
per-launch session token; project create/list/open/rename/archive/restore with
enforced isolation; persistent prompt draft; Unified Creative Command System
(preview → approve → commit → audit → undo, idempotency + optimistic
concurrency); durable job engine with restart reconciliation; WebSocket
progress; a **clearly labeled mock** image adapter that proves the whole path
prompt → proposal → job → asset without any model; hardware/environment probe
(GPU vendor-agnostic); SQLite WAL + Alembic migrations; contract tables for
manifests and render cache.

**Deliberately not in M0:** real AI inference, model downloads, cloud calls,
project dashboard extras (templates applied/duplicate/delete), character/world
systems, asset import, backups UI, editor. Disabled nav entries are labeled
with their target milestone — nothing nonfunctional is presented as working.

## Layout

Project data lives under `%LOCALAPPDATA%\AtomicAIStudio\` by default
(`ATOMIC_DATA_DIR` overrides): `atomic.db` (SQLite), `assets/`, `jobs/`,
`backups/`, `logs/`. Media bytes never go into SQLite.
