# Builds the frozen backend sidecar and syncs it into the Tauri bundle dir.
# Run from the repo root: scripts\build-sidecar.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$dist = Join-Path $backend "dist\atomic-backend"
$dest = Join-Path $root "apps\desktop\src-tauri\backend-dist\atomic-backend"

$pyinstaller = Join-Path $backend ".venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $pyinstaller)) {
    Write-Error "pyinstaller not found — run: backend\.venv\Scripts\pip install pyinstaller"
}

Push-Location $backend
try {
    & $pyinstaller atomic-backend.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed ($LASTEXITCODE)" }
} finally {
    Pop-Location
}

if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
Copy-Item -Recurse $dist $dest
Write-Output "sidecar staged at $dest"
