# Test runner script for Unified AI Coding Hub on Windows
$ErrorActionPreference = "Stop"

Write-Host "=== Running Unified AI Coding Hub Test Suite ===" -ForegroundColor Cyan
$WorkspaceRoot = Split-Path -Parent $PSScriptRoot
Set-Location $WorkspaceRoot

# 1. Run Python pytest suite
Write-Host "1. Running Backend Pytest..." -ForegroundColor Yellow
$PythonVenv = "$WorkspaceRoot\.venv\Scripts\python.exe"
& $PythonVenv -m pytest "$WorkspaceRoot\backend\tests" -v
if ($LASTEXITCODE -ne 0) {
    Write-Host "Backend tests failed!" -ForegroundColor Red
    exit $LASTEXITCODE
}

# 2. Run TypeScript compiler check
Write-Host "2. Running TypeScript Extension Type Check & Build..." -ForegroundColor Yellow
Set-Location "$WorkspaceRoot\extension"
cmd.exe /c "npm.cmd run compile"
if ($LASTEXITCODE -ne 0) {
    Write-Host "TypeScript compilation failed!" -ForegroundColor Red
    exit $LASTEXITCODE
}

Set-Location $WorkspaceRoot
Write-Host "=== All tests and builds passed! ===" -ForegroundColor Green
