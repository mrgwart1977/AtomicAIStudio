# Static regression test for the `changes` job path classifier in
# .github/workflows/ci.yml. Extracts the real `out <group> '<regex>'`
# lines from the workflow (no copied patterns -> no drift) and asserts
# expected classifications. Mirrors `grep -E` semantics: a group is
# true when ANY changed path matches its extended regex.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-ci-paths.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$wf = Get-Content (Join-Path $root ".github\workflows\ci.yml") -Raw

$patterns = @{}
foreach ($m in [regex]::Matches($wf, "out (\w+) '([^']+)'")) {
    $patterns[$m.Groups[1].Value] = $m.Groups[2].Value
}
foreach ($g in @("backend", "frontend", "tauri", "package")) {
    if (-not $patterns.ContainsKey($g)) { throw "missing classifier output: $g" }
}

function Classify([string[]]$files) {
    $r = @{}
    foreach ($g in $patterns.Keys) {
        $r[$g] = ($files | Where-Object { $_ -match $patterns[$g] }).Count -gt 0
    }
    return $r
}

$fail = 0
function Check([string]$name, [string[]]$files, [bool]$b, [bool]$f, [bool]$t, [bool]$p) {
    $r = Classify $files
    $exp = @{ backend = $b; frontend = $f; tauri = $t; package = $p }
    $bad = ($exp.Keys | Where-Object { $r[$_] -ne $exp[$_] })
    if ($bad) {
        Write-Host "FAIL $name : expected $exp got backend=$($r.backend) frontend=$($r.frontend) tauri=$($r.tauri) package=$($r.package)"
        $script:fail++
    } else {
        Write-Host "PASS $name"
    }
}

Check "backend code"            @("backend/app/api/foo.py")                     $true  $false $false $false
Check "backend spec (pkg)"      @("backend/atomic-backend.spec")                $true  $false $false $true
Check "backend deps (pkg)"      @("backend/pyproject.toml")                     $true  $false $false $true
Check "ffmpeg manifest (pkg)"   @("backend/ffmpeg-manifest.json")               $true  $false $false $true
Check "UI source"               @("apps/desktop/src/App.tsx")                   $false $true  $false $false
Check "src-tauri (pkg)"         @("apps/desktop/src-tauri/tauri.conf.json")     $false $true  $true  $true
Check "desktop package.json"    @("apps/desktop/package.json")                  $false $true  $true  $false
Check "CI workflow"             @(".github/workflows/ci.yml")                   $true  $true  $true  $true
Check "other workflow file"     @(".github/workflows/release.yml")              $true  $true  $true  $true
Check "validation script"       @("scripts/validate.ps1")                       $true  $true  $true  $true
Check "sidecar script (pkg)"    @("scripts/build-sidecar.ps1")                  $true  $true  $true  $true
Check "ffmpeg script (pkg)"     @("scripts/fetch-ffmpeg.ps1")                   $true  $true  $true  $true
Check "dev-only script"         @("scripts/capture-window.ps1")                 $true  $true  $true  $false
Check "docs only"               @("docs/handoff/spec.md", "README.md")          $false $false $false $false
Check "gitignore only"          @(".gitignore")                                 $false $false $false $false
Check "rename docs->backend"    @("docs/old.md", "backend/new.py")              $true  $false $false $false
Check "rename backend->docs"    @("backend/old.py", "docs/new.md")              $true  $false $false $false
Check "mixed code+docs"         @("backend/app/x.py", "docs/y.md")              $true  $false $false $false

if ($fail -gt 0) { Write-Host "`n$fail classification check(s) FAILED"; exit 1 }
Write-Host "`nAll path classifications correct"
exit 0
