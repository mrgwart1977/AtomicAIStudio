# Atomic Dream Engine

A free, open-source **Windows desktop AI filmmaking suite** — local-first,
project-scoped, and recoverable by design. Atomic Dream Engine organizes
AI generation into real production structure: isolated projects, versioned
character/world/location bibles, visual storyboards, durable generation
jobs, and full provenance — not a loose pile of prompt boxes.

**Status: early development (v0.x).** Local image and video pipelines are
implemented and tested; cloud-provider integrations are a *foundation*,
not finished features. See [ROADMAP.md](ROADMAP.md) for honest status.

## What works today

- **Projects** — isolated, portable, recoverable (`.atomicpkg` backup/restore)
- **Versioned bibles** — characters, worlds, locations with history + revert
- **Content-addressed asset store** — import, verify, relink, share across projects
- **Scenes, shots & multi-character casting** — visual storyboard UI
- **Local image generation** — Stable Diffusion 1.5 via stable-diffusion.cpp (Vulkan/CUDA), explicit model-download approval, SHA-verified
- **Local video generation** — Wan 2.1 T2V 1.3B via sd.cpp, supervised workers, cancellation & recovery
- **FFmpeg media engine** — pinned LGPL build for clip inspection/validation
- **Provider Manager (foundation)** — catalog + DPAPI credential vault + settings UI; **no live cloud generation yet**
- **Durable jobs & undo** — every mutation is an audited command; jobs survive restarts
- **Windows installer** — NSIS via Tauri 2 + PyInstaller-frozen backend

## What is NOT functional yet

- **Cloud generation** — the 60-entry provider catalog is a management
  foundation only. Nothing calls a real provider API, runs a paid job, or
  drives a browser. Mock connectivity tests are labeled mock.
- **C1.5+ import/automation workflows**, LTX integration, audio, editing
  timelines — planned, not implemented.

## Platform & hardware

- **OS:** Windows 11 (x64). Windows 10 may work but is untested.
- **GPU:** optional for development; needed for local generation.
  - SD 1.5 fp16 image generation: ~4 GB VRAM realistic minimum
  - Wan 2.1 T2V 1.3B fp16 video: **substantially more VRAM + RAM** —
    this is a real video pipeline, not a toy. Low-memory systems are
    rejected by admission control rather than half-running.
- **Disk:** ~2 GB app + space for models (each 2–6 GB) and project media.

## Install

Released installers bundle everything (backend, FFmpeg, shell) — no
Python/Node/Rust needed. *(Public releases are not published yet; the
installer path exists in CI.)*

## Develop

Prereqs: Python 3.12 + 3.14, Node.js 22 LTS, Rust stable, PowerShell 5.1+.

```powershell
git clone https://github.com/mrgwart1977/AtomicAIStudio.git
cd AtomicAIStudio
backend\.venv\Scripts\python -m pytest tests -q   # backend suite (create .venv first)
cd apps\desktop; npm ci; npm run build            # frontend
npm run tauri dev                                 # full desktop app
```

Local validation (CI-equivalent): `scripts\validate.ps1` — backend on both
Pythons + frontend build; `-Tauri` and `-Package` optional. Use
`-Python312`/`-Python314` to point at explicit interpreter paths;
`-SelfTest` exercises the harness itself. Timestamped reports land in
`validation-reports\` (gitignored), per-check logs in `%TEMP%`; missing
required toolchains fail, never skip. Never downloads models, calls cloud
providers, or runs real inference.

## Architecture

```
apps/desktop        Tauri 2 shell — React 19 + TypeScript + Vite + Zustand
  src-tauri         Rust shell (spawns + supervises the backend sidecar)
backend             FastAPI service (frozen with PyInstaller for shipping)
  app/domain        projects, bibles, CAS assets, jobs, workers, providers
  app/api           REST + WebSocket; X-Atomic-Token session auth
scripts/            dev, build-sidecar, fetch-ffmpeg, validation tooling
docs/handoff        design spec of record (v0.9) — amendments extend it
```

Every mutation flows through the command system (preview → commit →
audit → undo). Project-owned rows are isolated by `project_id` at the
DB/API layer. Mock adapters are labeled `adapter_kind="mock"` and are
never presented as inference.

## License & third parties

Application code: **Apache-2.0** — see [LICENSE](LICENSE).
Bundled/dependent components carry their own licenses — see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md), including FFmpeg
(LGPL-2.1+, shared-linked, source available upstream).

**AI model weights are NOT covered by Apache-2.0.** Models (SD 1.5
OpenRAIL-M, SDXL OpenRAIL++-M, Wan 2.1 Apache-2.0, etc.) are downloaded
by the user with explicit approval and remain under their original
licenses. This project never redistributes weights.

## Security

Report vulnerabilities via GitHub private vulnerability reporting — see
[SECURITY.md](SECURITY.md). Provider credentials are stored in a
Windows DPAPI (CurrentUser) vault, never in the database, exports, or logs.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) — workflow, conventions, acceptance
criteria. [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). [ROADMAP.md](ROADMAP.md).

"Atomic Dream Engine" name and logo: see [docs/BRANDING.md](docs/BRANDING.md).

