# Third-Party Notices

Atomic Dream Engine — Copyright 2026 Atomic Dream Engine contributors.
Licensed under Apache License 2.0 (see LICENSE).

This file lists third-party components with their licenses and
obligations. It reflects the versions pinned in this repository's
manifests (package.json / package-lock.json, pyproject.toml, Cargo.toml /
Cargo.lock, backend/ffmpeg-manifest.json) at the time of writing — not a
guarantee about future or downstream dependency changes.

## Bundled in the installer

| Component | License | Notes |
|---|---|---|
| **FFmpeg / FFprobe + libav\* DLLs** (BtbN build, `win64-lgpl-shared`) | **LGPL-2.1+** | Dynamic/shared linking only. Build has **no** `--enable-gpl` and **no** `--enable-nonfree`; libx264/libx265/libxavs2/libxvid/fdk-aac disabled. Source for the exact build: https://github.com/BtbN/FFmpeg-Builds + upstream https://github.com/FFmpeg/FFmpeg. We ship `LICENSE.txt` and `BUILDCONF.txt` inside `backend/vendor/ffmpeg/licenses/`. Users may replace the DLLs with their own LGPL-compatible build (shared linkage preserved). |
| PyInstaller bootloader (inside frozen backend) | GPL-2.0 with bootloader exception | The exception expressly permits distributing frozen applications under any license. |
| NSIS installer framework | zlib/libpng | Used by `tauri build` to produce the installer. |
| Tauri runtime + plugins | MIT OR Apache-2.0 | Desktop shell framework. |
| Rust deps (serde, serde_json, uuid) | MIT OR Apache-2.0 | See Cargo.lock for full transitive list. |

## Application dependencies (permissive)

- **Python:** FastAPI (MIT), uvicorn (BSD-3), SQLAlchemy (MIT),
  Alembic (MIT), Pydantic + pydantic-settings (MIT), psutil (BSD-3),
  Pillow (HPND). Dev-only: pytest (MIT), pytest-asyncio (Apache-2.0),
  httpx (BSD-3), ruff (MIT).
- **PyAV (`av`) — BSD-3 code, BUT its wheels bundle GPL-licensed FFmpeg**
  (libx264/libx265). PyAV is a **dev-environment fallback only and is
  permanently excluded from the frozen installer** (atomic-backend.spec).
  Production media ops use the LGPL FFmpeg binaries above.
- **Frontend:** React + react-dom (MIT), zustand (MIT), Radix UI (MIT),
  clsx (MIT), Tailwind CSS (MIT), Vite (MIT), TypeScript (Apache-2.0),
  @tauri-apps/api + plugin-dialog (MIT OR Apache-2.0),
  openapi-typescript (MIT), @types/* (MIT).

## Downloaded runtimes & models (never redistributed by this project)

| Component | License | Distribution model |
|---|---|---|
| stable-diffusion.cpp (+ sd-cli/sd-server) | MIT | User-approved download of upstream release |
| Wan 2.1 T2V (weights + code) | Apache-2.0 | User-approved download |
| Stable Diffusion 1.5 weights | CreativeML OpenRAIL-M | User-approved download; **use restrictions apply — read the license** |
| SDXL 1.0 weights | OpenRAIL++-M | User-approved download; use restrictions apply |
| ComfyUI (planned runtime) | GPL-3.0 | External user install only — never vendored or copied into this codebase |
| NVIDIA CUDA / Vulkan runtimes | NVIDIA / Khronos terms | Provided by the user's drivers; nothing redistributed |

**Model weights are not covered by Apache-2.0.** Every model retains its
original license; several OpenRAIL-family licenses carry use restrictions
that flow to generated outputs' *permitted use* terms. The app records
the license of every approved model in its catalog.

## Assets

App icon and logo are original works created for this project.
