# Atomic AI Studio

A modular, offline-first Windows desktop application for planning, generating,
editing, and finishing AI-assisted films. Windows executable: `AtomicAIStudio.exe`.

Status: **Milestone 1 — project management + production foundations**. The
specification lives in [`docs/handoff/`](docs/handoff/README.md) (v0.9); the
accepted roadmap is in
[`docs/plans/ARCHITECTURAL_REVIEW_AND_ROADMAP.md`](docs/plans/ARCHITECTURAL_REVIEW_AND_ROADMAP.md)
and requirement-to-code mapping in [`docs/plans/TRACEABILITY.md`](docs/plans/TRACEABILITY.md).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Tauri 2 shell (Rust)  ── spawns/stops sidecar ──┐           │
│ React 19 + TS + Tailwind/Radix + Zustand        │           │
│        │ HTTP + WS (loopback, session token)    ▼           │
│        └──────────► FastAPI backend (frozen exe in prod,    │
│                     .venv in dev)                           │
│                     SQLAlchemy 2 + SQLite(WAL) + Alembic    │
│                     Unified commands ─ jobs ─ adapters      │
│                     CAS media store (files, never in DB)    │
└─────────────────────────────────────────────────────────────┘
```

- All project mutations go through the **Unified Creative Command** system
  (preview → commit → audit + undo); job-producing commands enqueue durable
  ops (import/verify/backup/restore) that survive restarts.
- Media lives in a **content-addressed store** under configurable storage
  locations — SQLite only keeps references, so projects scale to TBs.
- Projects are isolated by `project_id` FKs everywhere; sharing is explicit
  via the shared library (link or copy).
- Whole-project `.atomicpkg` backups are hash-verified and always restore
  into a **new** project — the live one is never mutated.

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

## Standalone packaging (M0.1)

The installer ships a **self-contained backend** — no Python, pip, Node.js,
Rust, or virtualenv required on the user's machine. The backend is frozen
with PyInstaller (onedir) and bundled by Tauri next to the shell exe:

```
backend\sidecar_entry.py      # PyInstaller entry
backend\atomic-backend.spec   # freeze spec (hidden imports + alembic data)
scripts\build-sidecar.ps1     # builds dist → syncs into src-tauri\backend-dist
apps\desktop\src-tauri\
  backend-dist\atomic-backend\atomic-backend.exe   # bundled resource
```

Installed layout (per-user, `%LOCALAPPDATA%\Atomic AI Studio\`):

```
atomic-ai-studio.exe
backend\atomic-backend.exe + _internal\   (embedded Python runtime)
uninstall.exe
```

Sidecar resolution order in the shell: bundled exe → dev `.venv` /
`ATOMIC_PYTHON` override → PATH `python`. The packaged app always uses the
bundled exe; the dev fallbacks only exist for `cargo run`/`tauri dev`
without built resources. Same flags in every mode
(`--port --token --data-dir`), same per-launch token + ephemeral loopback
port + kill-on-exit.

Build the installer:

```powershell
scripts\build-sidecar.ps1     # pyinstaller → src-tauri\backend-dist
cd apps\desktop
npm run tauri build           # → target\release\bundle\nsis\*-setup.exe
```

Validated on this machine (clean of Python/dev tools on PATH):
silent install `/S` → app window opens → bundled `atomic-backend.exe`
spawned → migrations run → health + authenticated API + full
command→job→asset pipeline → restart keeps DB → `/S` uninstall removes
files, registry entry, and leaves no processes. Evidence:
`docs/m0.1-evidence/`."

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
