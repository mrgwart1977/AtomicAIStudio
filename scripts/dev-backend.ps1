# Start the backend standalone for browser-based dev (vite on :1420, token "dev").
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\..\backend"
& .\.venv\Scripts\python -m app.main --port 8788 --token dev --data-dir .\data
