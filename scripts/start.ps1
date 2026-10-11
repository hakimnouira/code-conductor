# Standalone startup script for testing FastAPI backend on 127.0.0.1
$ErrorActionPreference = "Stop"

$WorkspaceRoot = Split-Path -Parent $PSScriptRoot
Set-Location $WorkspaceRoot

$PythonVenv = "$WorkspaceRoot\.venv\Scripts\python.exe"
if (-not $env:AIHUB_AUTH_TOKEN) {
    # Generate random 32-byte hex token if not set
    $Bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($Bytes)
    $env:AIHUB_AUTH_TOKEN = [System.BitConverter]::ToString($Bytes).Replace("-", "").ToLower()
}

Write-Host "Starting Unified AI Hub backend on 127.0.0.1:8765..." -ForegroundColor Cyan
Write-Host "Session Auth Token: $env:AIHUB_AUTH_TOKEN" -ForegroundColor Yellow

$env:PYTHONPATH = "$WorkspaceRoot\backend"
& $PythonVenv -m uvicorn app.main:app --host 127.0.0.1 --port 8765
