# M4.0 video-generation telemetry: sample nvidia-smi (VRAM+util) and process
# RSS for the sd-cli worker while a generation job runs.
# Usage: video-bench.ps1 -JobId <id> [-IntervalMs 500] [-Out bench.json]
param(
    [Parameter(Mandatory=$true)][string]$JobId,
    [int]$IntervalMs = 500,
    [string]$Out = "bench.json"
)

$token = "dev"
$base = "http://127.0.0.1:8791"
$samples = @()
$baseline = & nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits
Write-Host "baseline VRAM: $baseline MiB"

$proc = $null
$peakRss = 0
$done = $false
$start = Get-Date
while (-not $done) {
    $jobs = Invoke-RestMethod -Headers @{"X-Atomic-Token"=$token} "$base/api/jobs"
    $job = $jobs | Where-Object { $_.id -eq $JobId }
    if ($job.status -in @("succeeded","failed","cancelled")) { $done = $true }
    # worker process = newest sd-cli.exe
    $p = Get-Process sd-cli -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending | Select-Object -First 1
    if ($p) { $peakRss = [Math]::Max($peakRss, $p.WorkingSet64) }
    $smi = & nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits
    $parts = $smi -split ",\s*"
    $samples += [pscustomobject]@{
        t_s = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
        vram_mib = [int]$parts[0]
        util_pct = [int]$parts[1]
        job_status = $job.status
        job_progress = $job.progress
        job_message = $job.message
    }
    Start-Sleep -Milliseconds $IntervalMs
}
$peak = ($samples | Measure-Object vram_mib -Maximum).Maximum
$utilPeak = ($samples | Measure-Object util_pct -Maximum).Maximum
$result = [pscustomobject]@{
    job_id = $JobId
    final_status = $job.status
    duration_s = [math]::Round(((Get-Date) - $start).TotalSeconds, 1)
    baseline_vram_mib = [int]$baseline
    peak_vram_mib = $peak
    delta_vram_mib = $peak - [int]$baseline
    peak_gpu_util_pct = $utilPeak
    worker_peak_rss_mib = [math]::Round($peakRss / 1MB)
    samples = $samples
}
$result | ConvertTo-Json -Depth 4 | Set-Content $Out -Encoding utf8
Write-Host "final: $($job.status) | peak VRAM $peak MiB (delta $($peak - [int]$baseline)) | peak util $utilPeak% | worker RSS $([math]::Round($peakRss/1MB)) MiB | $($result.duration_s)s"
