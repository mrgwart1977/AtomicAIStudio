# Atomic Dream Engine -- local validation (Actions-free CI equivalent)
#
# Mirrors the GitHub CI gates without spending Actions minutes:
#   REQUIRED  backend test suite under Python 3.12 AND 3.14
#   REQUIRED  frontend (npm ci + build)
#   OPTIONAL  -Tauri    cargo build of the desktop shell
#   OPTIONAL  -Package  frozen backend sidecar + NSIS installer
#
# Missing REQUIRED toolchains fail the check (never silently skipped).
# Exit code is nonzero if any required or requested check fails.
# Nothing here downloads models, calls cloud providers, or runs real
# AI inference -- the suite exercises mock workers/adapters only.
#
# PREREQUISITES
#   - Python 3.12 and 3.14 interpreters (python.org, uv/Astral, or
#     explicit -Python312/-Python314 paths). Discovery order:
#     param > `py -3.XX` launcher > uv pythons > C:\Python3XX dirs.
#   - Node.js LTS + npm on PATH.
#   - For -Tauri/-Package: Rust toolchain (cargo) on PATH.
#   - For -Package: backend\.venv with pyinstaller installed.
#   - PowerShell 5.1+ (no pwsh requirement).
#
# USAGE
#   scripts\validate.ps1                 # backend (3.12+3.14) + frontend
#   scripts\validate.ps1 -Tauri          # + cargo build check
#   scripts\validate.ps1 -Package        # + sidecar + NSIS installer
#   scripts\validate.ps1 -Python312 C:\path\python.exe
#
# Output: validation-reports\validate-<timestamp>.log (repo-local,
# gitignored) -- per-check status, durations, and failure tails.
# The report contains build/test output only -- no environment
# variables, credentials, or secrets are written to it.

[CmdletBinding()]
param(
    [switch]$Tauri,
    [switch]$Package,
    [string]$Python312,
    [string]$Python314
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$desktop = Join-Path $root "apps\desktop"
$reportDir = Join-Path $root "validation-reports"
New-Item -ItemType Directory -Force $reportDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$reportPath = Join-Path $reportDir "validate-$stamp.log"

$results = New-Object System.Collections.Generic.List[object]

function Out-Report([string]$line) {
    Write-Host $line
    Add-Content -Path $reportPath -Value $line -Encoding UTF8
}

function Resolve-Python([string]$ver, [string]$explicit) {
    if ($explicit) {
        if (Test-Path $explicit) { return $explicit }
        throw "Explicit Python path not found: $explicit"
    }
    # 1) py launcher (python.org installs)
    $probe = & cmd /c "py -$ver --version 2>nul"
    if ($LASTEXITCODE -eq 0 -and $probe) {
        $exe = & cmd /c "py -$ver -c `"import sys;print(sys.executable)`" 2>nul"
        if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
    # 2) uv/Astral-managed pythons (Roaming + Local appdata both seen)
    foreach ($uvRoot in @("$env:APPDATA\uv\python", "$env:LOCALAPPDATA\uv\python")) {
        if (Test-Path $uvRoot) {
            $cand = Get-ChildItem $uvRoot -Directory |
                Where-Object { $_.Name -like "cpython-$ver*" } |
                Sort-Object Name -Descending | Select-Object -First 1
            if ($cand) {
                $exe = Join-Path $cand.FullName "python.exe"
                if (Test-Path $exe) { return $exe }
            }
        }
    }
    # 3) conventional C:\PythonXX installs
    foreach ($suffix in @("$($ver -replace '\.','')", $ver)) {
        foreach ($base in @("C:\", "$env:LOCALAPPDATA\Programs\Python\")) {
            $exe = Join-Path $base "Python$suffix\python.exe"
            if (Test-Path $exe) { return $exe }
        }
    }
    return $null
}

function Invoke-Step([string]$name, [scriptblock]$body) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $tmp = Join-Path $env:TEMP ("validate-$name-" + [guid]::NewGuid().ToString("N") + ".log")
    New-Item -ItemType File -Force $tmp | Out-Null
    $status = "PASS"
    # Steps run with Continue semantics: native stderr lines must not
    # become terminating errors, only real exit codes/throws fail a check.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    try {
        & $body 2>&1 | ForEach-Object {
            Add-Content -Path $tmp -Value ($_ | Out-String).TrimEnd() -Encoding UTF8
        }
        if ($LASTEXITCODE -ne 0) { $status = "FAIL" }
    } catch {
        $status = "FAIL"
        Add-Content -Path $tmp -Value ("EXCEPTION: " + ($_ | Out-String) + $_.ScriptStackTrace) -Encoding UTF8
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    $sw.Stop()
    $results.Add([pscustomobject]@{
        Name = $name; Status = $status; Duration = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        LogFile = $tmp
    })
    Out-Report ("[{0}] {1}  ({2}s)" -f $status, $name, $sw.Elapsed.TotalSeconds.ToString("0.0"))
    if ($status -eq "FAIL") {
        Out-Report "---- $name tail ----"
        Get-Content $tmp -Tail 30 | ForEach-Object { Out-Report $_ }
        Out-Report "----------------------"
    }
    return ($status -eq "PASS")
}

Out-Report "Atomic Dream Engine -- validation $stamp"
Out-Report "repo: $root"
Out-Report ""

# ---------- required: backend under 3.12 and 3.14 ----------
foreach ($ver in @("3.12", "3.14")) {
    $explicit = if ($ver -eq "3.12") { $Python312 } else { $Python314 }
    $py = Resolve-Python $ver $explicit
    if (-not $py) {
        $results.Add([pscustomobject]@{
            Name = "backend-$ver"; Status = "FAIL"; Duration = 0
            LogFile = ""; Detail = "Python $ver interpreter not found (install python.org $ver, uv, or pass -Python$($ver -replace '\.',''))" })
        Out-Report "[FAIL] backend-$ver -- interpreter not found"
        continue
    }
    Out-Report "Python $ver = $py"
    $venv = Join-Path $backend ".venv-ci-$($ver -replace '\.','')"
    Invoke-Step "backend-$ver" {
        if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
            & $py -m venv $venv | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "venv creation failed for Python $ver" }
        }
        $vpy = Join-Path $venv "Scripts\python.exe"
        Push-Location $backend
        try {
            & $vpy -m pip install -q -e ".[dev]"
            if ($LASTEXITCODE -ne 0) { throw "pip install failed for Python $ver" }
            & $vpy -m pytest tests -q
            if ($LASTEXITCODE -ne 0) { $global:LASTEXITCODE = $LASTEXITCODE; return "pytest exit $LASTEXITCODE" }
            "pytest passed under Python $ver"
        } finally { Pop-Location }
    } | Out-Null
}

# ---------- required: ffmpeg staging for M4.0.1 tests ----------
Invoke-Step "ffmpeg-stage" {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\fetch-ffmpeg.ps1")
    if ($LASTEXITCODE -ne 0) { "fetch-ffmpeg failed ($LASTEXITCODE)" } else { "ffmpeg staged" }
} | Out-Null

# ---------- required: frontend ----------
Invoke-Step "frontend" {
    Push-Location $desktop
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
        & npm run build
        if ($LASTEXITCODE -ne 0) { $global:LASTEXITCODE = $LASTEXITCODE; return "build exit $LASTEXITCODE" }
        "frontend build OK"
    } finally { Pop-Location }
} | Out-Null

# ---------- optional: tauri ----------
if ($Tauri -or $Package) {
    Invoke-Step "tauri" {
        $cargo = Get-Command cargo -ErrorAction SilentlyContinue
        if (-not $cargo) { throw "cargo not found -- install Rust toolchain for -Tauri/-Package" }
        Push-Location $desktop
        try {
            & npm ci | Out-Null
            & npm run build | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "frontend build failed (required by tauri config)" }
            New-Item -ItemType Directory -Force "src-tauri\backend-dist\atomic-backend" | Out-Null
            Push-Location "src-tauri"
            try {
                & cargo build
                if ($LASTEXITCODE -ne 0) { throw "cargo build failed ($LASTEXITCODE)" }
            } finally { Pop-Location }
            "tauri shell build OK"
        } finally { Pop-Location }
    } | Out-Null
}

# ---------- optional: packaging ----------
if ($Package) {
    Invoke-Step "package" {
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\build-sidecar.ps1")
        if ($LASTEXITCODE -ne 0) { throw "build-sidecar failed ($LASTEXITCODE)" }
        Push-Location $desktop
        try {
            & npm run tauri build
            if ($LASTEXITCODE -ne 0) { throw "tauri build failed ($LASTEXITCODE)" }
        } finally { Pop-Location }
        "NSIS installer built"
    } | Out-Null
}

# ---------- summary ----------
Out-Report ""
Out-Report ("{0,-16} {1,-7} {2,8}   {3}" -f "CHECK", "RESULT", "SECONDS", "LOG")
$failed = 0
foreach ($r in $results) {
    Out-Report ("{0,-16} {1,-7} {2,8}   {3}" -f $r.Name, $r.Status, $r.Duration, $r.LogFile)
    if ($r.Status -eq "FAIL") { $failed++ }
}
Out-Report ""
if ($failed -gt 0) {
    Out-Report "$failed check(s) FAILED -- see tails above and $reportPath"
    exit 1
}
Out-Report "All checks passed -- report: $reportPath"
exit 0
