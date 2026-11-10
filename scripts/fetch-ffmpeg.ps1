# Fetches the owner-approved FFmpeg binaries pinned in
# backend/ffmpeg-manifest.json, verifies the SHA-256, and stages the
# minimal ship set into backend/vendor/ffmpeg/. Fails closed on any
# verification mismatch — never substitutes another binary.
# Run from repo root: scripts\fetch-ffmpeg.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$manifest = Get-Content (Join-Path $backend "ffmpeg-manifest.json") -Raw | ConvertFrom-Json
$vendor = Join-Path $backend "vendor\ffmpeg"
$binDir = Join-Path $vendor "bin"
$licDir = Join-Path $vendor "licenses"
$zipPath = Join-Path $backend ("vendor\" + $manifest.archive.file)

if ((Test-Path (Join-Path $binDir "ffmpeg.exe")) -and (Test-Path (Join-Path $binDir "ffprobe.exe"))) {
    Write-Output "ffmpeg already staged at $binDir"
    return
}

New-Item -ItemType Directory -Force $binDir, $licDir | Out-Null

if (-not (Test-Path $zipPath)) {
    Write-Output "downloading $($manifest.archive.url)"
    Invoke-WebRequest -Uri $manifest.archive.url -OutFile $zipPath
}

$actual = (Get-FileHash $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$expected = $manifest.archive.sha256.ToLowerInvariant()
if ($actual -ne $expected) {
    throw "SHA-256 mismatch for $($manifest.archive.file)`n  expected $expected`n  got      $actual"
}
Write-Output "sha256 verified: $actual"

$tmp = Join-Path $backend "vendor\_ffextract"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive $zipPath -DestinationPath $tmp
$pkg = Get-ChildItem $tmp -Directory | Select-Object -First 1
foreach ($f in $manifest.ship_files) {
    Copy-Item (Join-Path $pkg.FullName $f) $binDir
}
Copy-Item (Join-Path $pkg.FullName "LICENSE.txt") $licDir
& (Join-Path $binDir "ffmpeg.exe") -buildconf | Out-File -Encoding utf8 (Join-Path $licDir "BUILDCONF.txt")
Remove-Item -Recurse -Force $tmp
Write-Output "ffmpeg $($manifest.ffmpeg_version) staged at $binDir"
