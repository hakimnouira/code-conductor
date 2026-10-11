# Health check verification script for local FastAPI backend
param (
    [string]$Token = $env:AIHUB_AUTH_TOKEN,
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

if (-not $Token) {
    Write-Host "Error: AIHUB_AUTH_TOKEN environment variable or -Token parameter required." -ForegroundColor Red
    exit 1
}

$Url = "http://127.0.0.1:$Port/health"
Write-Host "Querying $Url with Bearer authentication..." -ForegroundColor Cyan

try {
    $Headers = @{
        "Authorization" = "Bearer $Token"
    }
    $Response = Invoke-RestMethod -Uri $Url -Headers $Headers -Method Get
    Write-Host "Backend is HEALTHY!" -ForegroundColor Green
    Write-Host "Version: $($Response.version)"
    Write-Host "Uptime (s): $($Response.uptime_seconds)"
    Write-Host "Workspace: $($Response.workspace)"
    Write-Host "Active Tasks: $($Response.active_tasks)"
} catch {
    Write-Host "Health check failed: $_" -ForegroundColor Red
    exit 1
}
