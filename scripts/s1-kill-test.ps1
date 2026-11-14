$fe = Get-Process atomic-dream-engine -ErrorAction SilentlyContinue
$be = Get-Process atomic-backend -ErrorAction SilentlyContinue
Write-Output "frontend=$($fe.Id) backend=$($be.Id)"
Stop-Process -Id $fe.Id -Force
Start-Sleep -Seconds 3
$be2 = Get-Process atomic-backend -ErrorAction SilentlyContinue
if ($be2) { Write-Output "BACKEND STILL RUNNING - FAIL" } else { Write-Output "backend reaped by OS - PASS" }
