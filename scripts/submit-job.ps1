<#
.SYNOPSIS
    Submit one MiniMax H3 workflow (plus its conditioning image) and print the job id.

.DESCRIPTION
    test-endpoint.ps1 submits and then blocks until the job finishes, which is the
    wrong shape when several variants must be compared: worker warmth dominates the
    timings (two identical 175-frame runs measured 507.7 s and 262.7 s), so the runs
    have to be queued together and executed back to back on one worker.

    This script only submits. Poll separately with poll-job.ps1.

    The conditioning image travels in `input.images`, which worker-comfyui writes into
    ComfyUI's input directory under the given name — that name must match the LoadImage
    node's `image` value in the workflow.

.EXAMPLE
    .\scripts\submit-job.ps1 -Workflow .\workflows\minimax_h3_i2v_vertical_1080p_lanczos_api.json `
                             -Image .\input\first_frame_768x1344.png
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Workflow,
    [string]$Image,
    [string]$EndpointId = 't9u2wy8o9ucnpz',
    [string]$ApiKey = $env:RUNPOD_API_KEY
)

$ErrorActionPreference = 'Stop'
if (-not $ApiKey) { throw "Set -ApiKey or the RUNPOD_API_KEY environment variable." }
if (-not (Test-Path $Workflow)) { throw "Workflow not found: $Workflow" }

$wf = Get-Content $Workflow -Raw | ConvertFrom-Json
$input_ = @{ workflow = $wf }

if ($Image) {
    if (-not (Test-Path $Image)) { throw "Image not found: $Image" }
    $bytes = [IO.File]::ReadAllBytes((Resolve-Path $Image))
    $input_.images = @(@{
        name  = (Split-Path $Image -Leaf)
        image = [Convert]::ToBase64String($bytes)
    })
}

$body = @{ input = $input_ } | ConvertTo-Json -Depth 100 -Compress
$mb = [math]::Round($body.Length / 1MB, 2)
if ($body.Length -gt 10MB) { throw "Payload is ${mb} MB, over RunPod's 10 MB /run cap." }

$headers = @{ Authorization = "Bearer $ApiKey"; 'Content-Type' = 'application/json' }
$job = Invoke-RestMethod -Uri "https://api.runpod.ai/v2/$EndpointId/run" -Method Post -Headers $headers -Body $body

Write-Host "$(Split-Path $Workflow -Leaf)  ->  $($job.id)   (payload ${mb} MB)"
$job.id
