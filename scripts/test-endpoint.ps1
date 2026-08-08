<#
.SYNOPSIS
    Submit a MiniMax H3 workflow to the RunPod serverless endpoint and wait for the video.

.DESCRIPTION
    Uses /run + polling rather than /runsync: H3 generation runs for minutes, well
    past the point where holding a synchronous HTTP connection is sensible.

    With S3 configured on the endpoint, the worker returns a URL and this script
    reports it. Without S3 it returns base64, which this script will save to disk
    but which is capped at 10 MB by RunPod — see README.

.EXAMPLE
    $env:RUNPOD_API_KEY = "..."
    .\scripts\test-endpoint.ps1 -EndpointId abc123 -Workflow .\workflows\minimax_h3_t2v_api.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EndpointId,
    [string]$Workflow = "$PSScriptRoot\..\workflows\minimax_h3_t2v_api.json",
    [string]$ApiKey = $env:RUNPOD_API_KEY,
    [string]$OutDir = "$PSScriptRoot\..\out",
    [int]$TimeoutMinutes = 30
)

$ErrorActionPreference = 'Stop'

if (-not $ApiKey) { throw "Set -ApiKey or the RUNPOD_API_KEY environment variable." }
if (-not (Test-Path $Workflow)) { throw "Workflow not found: $Workflow" }

$headers = @{ Authorization = "Bearer $ApiKey"; 'Content-Type' = 'application/json' }
$base    = "https://api.runpod.ai/v2/$EndpointId"

$wf   = Get-Content $Workflow -Raw | ConvertFrom-Json
$body = @{ input = @{ workflow = $wf } } | ConvertTo-Json -Depth 100 -Compress

Write-Host "==> submitting $(Split-Path $Workflow -Leaf) to $EndpointId"
$job = Invoke-RestMethod -Uri "$base/run" -Method Post -Headers $headers -Body $body
Write-Host "==> job id: $($job.id)"

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$last = ''
do {
    Start-Sleep -Seconds 5
    $status = Invoke-RestMethod -Uri "$base/status/$($job.id)" -Method Get -Headers $headers
    if ($status.status -ne $last) {
        Write-Host "    status: $($status.status)"
        $last = $status.status
    }
    if ((Get-Date) -gt $deadline) { throw "Timed out after $TimeoutMinutes minutes (job $($job.id))." }
} while ($status.status -in @('IN_QUEUE', 'IN_PROGRESS'))

if ($status.status -ne 'COMPLETED') {
    Write-Host "==> job did not complete: $($status.status)" -ForegroundColor Red
    $status | ConvertTo-Json -Depth 20
    exit 1
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

foreach ($item in $status.output.images) {
    switch ($item.type) {
        's3_url' {
            Write-Host "==> $($item.filename): $($item.data)" -ForegroundColor Green
        }
        'base64' {
            # Reached only when the endpoint has no S3 env vars set.
            $dest = Join-Path $OutDir $item.filename
            New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
            [IO.File]::WriteAllBytes($dest, [Convert]::FromBase64String($item.data))
            Write-Host "==> saved $dest" -ForegroundColor Yellow
            Write-Host "    (base64 path - configure S3 on the endpoint to lift the 10 MB cap)"
        }
        default { Write-Host "==> unexpected output type: $($item.type)" -ForegroundColor Red }
    }
}

if (-not $status.output.images) {
    Write-Host "==> no outputs returned. Full payload:" -ForegroundColor Red
    $status.output | ConvertTo-Json -Depth 20
    exit 1
}

$secs = [math]::Round($status.executionTime / 1000, 1)
Write-Host "==> done in ${secs}s"
Write-Host "    Verify the audio track with:  ffprobe -hide_banner <file-or-url>"
