$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$backendDir = Join-Path $projectRoot "backend"

if (-not (Test-Path $python -PathType Leaf)) {
    Write-Error "Code Conductor Python environment was not found at '$python'. Run .\scripts\setup.ps1 first."
    exit 2
}
if (-not (Test-Path $backendDir -PathType Container)) {
    Write-Error "Backend directory was not found at '$backendDir'."
    exit 2
}

# Do not start a second server if the backend port is already occupied.
$existingListener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existingListener) {
    exit 0
}

$env:PYTHONPATH = $backendDir
Set-Location $backendDir
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) {
    Write-Error "Code Conductor backend exited with code $exitCode."
}
exit $exitCode
