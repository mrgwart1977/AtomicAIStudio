$exe = "C:\Projects\atomic-ai-studio\AtomicAIStudio\apps\desktop\src-tauri\target\release\atomic-dream-engine.exe"
for ($i = 1; $i -le 3; $i++) {
    & $exe
    Start-Sleep -Seconds 12
    $be = Get-Process atomic-backend -ErrorAction SilentlyContinue
    $fe = Get-Process atomic-dream-engine -ErrorAction SilentlyContinue
    if (-not $be -or -not $fe) { Write-Output "cycle ${i}: SPAWN FAIL"; continue }
    Stop-Process -Id $fe.Id -Force
    Start-Sleep -Seconds 3
    $be2 = Get-Process atomic-backend -ErrorAction SilentlyContinue
    if ($be2) { Write-Output "cycle ${i}: backend=$($be.Id) ORPHANED - FAIL" }
    else { Write-Output "cycle ${i}: backend=$($be.Id) reaped - PASS" }
}
Write-Output "cycles done"
