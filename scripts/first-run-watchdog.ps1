# First-run inference watchdog — independent resource safety monitor.
# Run in a SEPARATE terminal BEFORE committing the generation command.
# It never talks to the backend or sd-server APIs; it only reads
# psutil/nvidia-smi telemetry and kills the post-baseline sd-server
# process tree on a confirmed safety breach.
#
#   scripts\first-run-watchdog.ps1 [-GpuIndex 0] [-DeadlineS 420] [-DryRun]
#
# Exit codes: 0 clean, 2 breach terminated, 3 watchdog error.
param(
    [int]$GpuIndex = 0,
    [double]$DeadlineS = 420,
    [double]$IntervalS = 1.0,
    [switch]$DryRun,
    [string]$Log = "$env:LOCALAPPDATA\AtomicAIStudio\watchdog\first-run-$(Get-Date -Format 'yyyyMMdd-HHmmss').jsonl"
)

$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Error "backend venv not found: $py"; exit 3 }

$argList = @("-m", "app.domain.firstrun_watchdog",
             "--gpu-index", "$GpuIndex",
             "--deadline-s", "$DeadlineS",
             "--interval-s", "$IntervalS",
             "--log", "$Log")
if ($DryRun) { $argList += "--dry-run" }

Write-Host "watchdog log: $Log"
Write-Host "watching GPU $GpuIndex — VRAM floor 0.5 GiB, RAM floor 1.5 GiB, tree ceiling 8 GiB, deadline ${DeadlineS}s"
Push-Location (Join-Path $repo "backend")
& $py @argList
$code = $LASTEXITCODE
Pop-Location
exit $code
