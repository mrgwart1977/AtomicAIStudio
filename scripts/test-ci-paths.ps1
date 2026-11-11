# Static regression test for the `changes` job path classifier in
# .github/workflows/ci.yml. Extracts the real `out <group> '<regex>'`
# lines from the workflow (no copied patterns -> no drift) and asserts
# expected classifications. Mirrors `grep -E` semantics: a group is
# true when ANY changed path matches its extended regex.
#
# An empty file list models the classifier's fail-open path: when the
# diff can't be established (ok=0), every output is forced true, so a
# gate failure can never silently skip required checks.
#
# Usage (PowerShell 5.1 or 7+, Windows or Linux):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test-ci-paths.ps1
#   pwsh -File scripts/test-ci-paths.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$wf = Get-Content (Join-Path $root ".github/workflows/ci.yml") -Raw

$patterns = @{}
foreach ($m in [regex]::Matches($wf, "out (\w+) '([^']+)'")) {
    $patterns[$m.Groups[1].Value] = $m.Groups[2].Value
}
foreach ($g in @("backend", "frontend", "tauri", "package")) {
    if (-not $patterns.ContainsKey($g)) { throw "missing classifier output: $g" }
}

function Classify([string[]]$files) {
    $r = @{}
    if ($files.Count -eq 0) {
        # Real classifier: empty/unresolvable diff -> ok=0 -> all true.
        foreach ($g in $patterns.Keys) { $r[$g] = $true }
        return $r
    }
    foreach ($g in $patterns.Keys) {
        # -cmatch: grep -E is case-sensitive; -match is not.
        $r[$g] = ($files | Where-Object { $_ -cmatch $patterns[$g] }).Count -gt 0
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
Check "empty diff (fail-open)"  @()                                             $true  $true  $true  $true
Check "rename docs->backend"    @("docs/old.md", "backend/new.py")              $true  $false $false $false
Check "rename backend->docs"    @("backend/old.py", "docs/new.md")              $true  $false $false $false
Check "mixed code+docs"         @("backend/app/x.py", "docs/y.md")              $true  $false $false $false

# --- Required-check / matrix-skip guard -----------------------------------
# A job gated on needs.changes may be skipped. A skipped MATRIX job emits
# only its parent check context ("backend"), never leg names
# ("backend (3.12)") — so any required check named after a leg can never
# report and branch protection stalls. Rule: a changes-gated job must not
# use strategy.matrix, and every leg-named required check must appear as a
# literal `name:` (or job id) in the workflow.
$jobsSection = ($wf -split "(?m)^jobs:", 2)[-1]
$jobKeys = [regex]::Matches($jobsSection, "(?m)^  ([^\s#][\w(). ""-]*):")
for ($i = 0; $i -lt $jobKeys.Count; $i++) {
    $start = $jobKeys[$i].Index
    $end = if ($i + 1 -lt $jobKeys.Count) { $jobKeys[$i + 1].Index } else { $jobsSection.Length }
    $block = $jobsSection.Substring($start, $end - $start)
    $jid = $jobKeys[$i].Groups[1].Value.Trim().Trim('"')
    if ($block -match "if:.*needs\.changes" -and $block -match "strategy:\s*\n\s*matrix:") {
        Write-Host "FAIL matrix-guard : '$jid' is changes-gated AND uses strategy.matrix (skipped matrix can't emit leg-named check contexts)"
        $script:fail++
    }
}
foreach ($leg in @("backend (3.12)", "backend (3.14)")) {
    if ($wf -notmatch "name:\s*`"$([regex]::Escape($leg))`"" -and $wf -notmatch "(?m)^  `"?$([regex]::Escape($leg))`"?:") {
        Write-Host "FAIL required-name : '$leg' must exist as a literal job name/id so its context emits when skipped"
        $script:fail++
    }
}
Write-Host "PASS matrix-guard: no changes-gated matrix jobs; required leg names are literal"

if ($fail -gt 0) { Write-Host "`n$fail classification check(s) FAILED"; exit 1 }
Write-Host "`nAll path classifications correct"
exit 0
