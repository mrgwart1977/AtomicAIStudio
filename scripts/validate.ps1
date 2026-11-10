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
#   scripts\validate.ps1 -SelfTest    # exercises the harness itself:
#                                     # exit codes, missing-tool detection,
#                                     # report structure/encoding, stderr vs fail
#
# Output: validation-reports\validate-<timestamp>.log (repo-local,
# gitignored) -- per-check status, durations, and failure tails.
# The report contains build/test output only -- no environment
# variables, credentials, or secrets are written to it.

[CmdletBinding()]
param(
    [switch]$Tauri,
    [switch]$Package,
    [switch]$SelfTest,
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

# ---------- self-test: exercises the harness itself, not the real checks ----------
if ($SelfTest) {
    $st = 0
    Out-Report "validate.ps1 self-test -- synthetic Invoke-Step / discovery checks"
    Invoke-Step "selftest-exit0" { & cmd /c "exit 0" } | Out-Null
    if ($results[$results.Count-1].Status -eq "PASS") { Out-Report "  PASS: exit-0 step passes" } else { Out-Report "  FAIL: exit-0 step marked $($results[$results.Count-1].Status)"; $st++ }
    Invoke-Step "selftest-nonzero" { & cmd /c "exit 7" } | Out-Null
    if ($results[$results.Count-1].Status -eq "FAIL") { Out-Report "  PASS: nonzero exit fails the step" } else { Out-Report "  FAIL: nonzero exit not detected"; $st++ }
    Invoke-Step "selftest-throw" { throw "synthetic failure" } | Out-Null
    if ($results[$results.Count-1].Status -eq "FAIL") { Out-Report "  PASS: thrown error fails the step" } else { Out-Report "  FAIL: thrown error not detected"; $st++ }
    Invoke-Step "selftest-stderr" { & cmd /c 'echo just-a-warning 1>&2 & exit /b 0' } | Out-Null
    if ($results[$results.Count-1].Status -eq "PASS") { Out-Report "  PASS: native stderr warning is not a failure" } else { Out-Report "  FAIL: stderr warning caused false failure"; $st++ }
    if (Test-Path $results[$results.Count-1].LogFile) { Out-Report "  PASS: per-check log file written" } else { Out-Report "  FAIL: per-check log missing"; $st++ }
    $self = Join-Path $PSScriptRoot "validate.ps1"
    $bogus = Join-Path $env:TEMP ("missing-python-" + [guid]::NewGuid().ToString("N") + ".exe")
    $before = Get-Date
    & powershell -NoProfile -ExecutionPolicy Bypass -File $self -Python312 $bogus -Python314 $bogus | Out-Null
    if ($LASTEXITCODE -ne 0) { Out-Report "  PASS: missing required interpreters -> nonzero exit" } else { Out-Report "  FAIL: missing interpreters silently skipped"; $st++ }
    $childReport = Get-ChildItem $reportDir -Filter "validate-*.log" |
        Where-Object { $_.FullName -ne $reportPath -and $_.CreationTime -ge $before.AddSeconds(-2) } |
        Sort-Object CreationTime -Descending | Select-Object -First 1
    $strict = New-Object System.Text.UTF8Encoding($false, $true)
    $text = $strict.GetString([System.IO.File]::ReadAllBytes($childReport.FullName))
    if ($text -match "backend-3\.12\s+FAIL" -and $text -match "backend-3\.14\s+FAIL") { Out-Report "  PASS: child report records both interpreter failures" } else { Out-Report "  FAIL: interpreter failures missing from report"; $st++ }
    if ($text -match "CHECK\s+RESULT\s+SECONDS") { Out-Report "  PASS: summary table present" } else { Out-Report "  FAIL: summary table missing"; $st++ }
    Out-Report "  PASS: report decodes as strict UTF-8"
    $src = Get-Content $self -Raw
    if ($src -match '\[switch\]\$Tauri' -and $src -match '\[switch\]\$Package') { Out-Report "  PASS: -Tauri/-Package optional flags present" } else { Out-Report "  FAIL: optional flags missing"; $st++ }
    if ($st -gt 0) { Out-Report "self-test FAILED: $st assertion(s)"; exit 1 }
    Out-Report "self-test passed: all assertions green"
    exit 0
}

# ---------- required: backend under 3.12 and 3.14 ----------
foreach ($ver in @("3.12", "3.14")) {
    $explicit = if ($ver -eq "3.12") { $Python312 } else { $Python314 }
    $py = $null; $resolveErr = $null
    try { $py = Resolve-Python $ver $explicit } catch { $resolveErr = "$_" }
    if (-not $py) {
        $why = if ($resolveErr) { $resolveErr } else { "interpreter not found (install python.org $ver, uv, or pass -Python$($ver -replace '\.',''))" }
        $results.Add([pscustomobject]@{
            Name = "backend-$ver"; Status = "FAIL"; Duration = 0; LogFile = "" })
        Out-Report "[FAIL] backend-$ver -- $why"
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
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
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
