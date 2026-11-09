# Full desktop dev: Tauri shell spawns the backend sidecar itself.
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\..\apps\desktop"
npm run tauri dev
