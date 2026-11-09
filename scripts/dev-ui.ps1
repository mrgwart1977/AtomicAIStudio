# Start the Vite dev server only (for browser dev alongside dev-backend.ps1).
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\..\apps\desktop"
npm run dev
