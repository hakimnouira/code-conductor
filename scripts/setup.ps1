# Setup script for Unified AI Coding Hub on Windows
$ErrorActionPreference = "Stop"

Write-Host "=== Setting up Unified AI Coding Hub on Windows ===" -ForegroundColor Cyan

$WorkspaceRoot = Split-Path -Parent $PSScriptRoot
Set-Location $WorkspaceRoot

# 1. Check uv and Python
$UvPath = "C:\Users\hakim\.local\bin\uv.exe"
if (-not (Test-Path $UvPath)) {
    $UvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($UvCmd) { $UvPath = $UvCmd.Source } else { throw "uv not found." }
}

$PythonExe = "C:\Users\hakim\AppData\Roaming\uv\python\cpython-3.11.16-windows-x86_64-none\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python.exe"
}

Write-Host "Creating Python 3.11 virtual environment..." -ForegroundColor Yellow
& $UvPath venv --python $PythonExe "$WorkspaceRoot\.venv"

Write-Host "Installing backend dependencies..." -ForegroundColor Yellow
& $UvPath pip install --python "$WorkspaceRoot\.venv\Scripts\python.exe" -r "$WorkspaceRoot\backend\requirements.txt"

# 2. Node & Extension
Write-Host "Installing extension node dependencies..." -ForegroundColor Yellow
Set-Location "$WorkspaceRoot\extension"
cmd.exe /c "npm.cmd install --no-audit --no-fund"
cmd.exe /c "npm.cmd run compile"

Set-Location $WorkspaceRoot
Write-Host "=== Setup completed successfully! ===" -ForegroundColor Green
