$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$backendDir = Join-Path $projectRoot "backend"
$logDir = Join-Path $env:USERPROFILE ".code-conductor"
$logPath = Join-Path $logDir "backend.log"

if (-not (Test-Path $python -PathType Leaf)) {
    Write-Error "Code Conductor Python environment was not found at '$python'. Run .\scripts\setup.ps1 first."
    exit 2
}
if (-not (Test-Path $backendDir -PathType Container)) {
    Write-Error "Backend directory was not found at '$backendDir'."
    exit 2
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Set-Location $backendDir
$env:PYTHONPATH = $backendDir

# Stay alive while another launcher owns port 8765. If the backend exits, restart it.
while ($true) {
    $existingListener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($existingListener) {
        Start-Sleep -Seconds 10
        continue
    }

    try {
        "[$(Get-Date -Format o)] Starting Code Conductor backend." | Add-Content -Path $logPath
        & $python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 *>> $logPath
        $exitCode = $LASTEXITCODE
        "[$(Get-Date -Format o)] Backend exited with code $exitCode; restarting in 5 seconds." | Add-Content -Path $logPath
    } catch {
        "[$(Get-Date -Format o)] Backend launcher error: $($_.Exception.Message)" | Add-Content -Path $logPath
    }
    Start-Sleep -Seconds 5
}
