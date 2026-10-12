# PowerShell script to compile and launch the Extension Development Host window in VS Code
$ErrorActionPreference = "Stop"

$WorkspaceRoot = Split-Path -Parent $PSScriptRoot
Set-Location $WorkspaceRoot

Write-Host "=== 1. Compiling TypeScript Extension ===" -ForegroundColor Cyan
Set-Location "$WorkspaceRoot\extension"
cmd.exe /c "npm.cmd run compile"
if ($LASTEXITCODE -ne 0) {
    Write-Host "TypeScript compilation failed!" -ForegroundColor Red
    exit 1
}

Set-Location $WorkspaceRoot
Write-Host "=== 2. Launching VS Code Extension Development Host ===" -ForegroundColor Green

$CodeExe = "d:\Users\hakim\AppData\Local\Programs\Microsoft VS Code\Code.exe"
if (-not (Test-Path $CodeExe)) {
    $CodeExe = "C:\Users\hakim\AppData\Local\Programs\Microsoft VS Code\Code.exe"
}
if (-not (Test-Path $CodeExe)) {
    $CodeCmd = Get-Command code -ErrorAction SilentlyContinue
    if ($CodeCmd) { $CodeExe = $CodeCmd.Source }
}

Write-Host "Using VS Code binary: $CodeExe" -ForegroundColor Yellow
Write-Host "Loading extension: $WorkspaceRoot\extension" -ForegroundColor Yellow

$Arguments = @(
    "--extensionDevelopmentPath=$WorkspaceRoot\extension",
    "--new-window",
    "$WorkspaceRoot"
)

Start-Process -FilePath $CodeExe -ArgumentList $Arguments

Write-Host "=== Extension Development Host Window Spawned! ===" -ForegroundColor Green
Write-Host "In the new VS Code window:" -ForegroundColor Cyan
Write-Host "1. Look on the Activity Bar (left) for the 'AI Coding Hub' robot icon." -ForegroundColor White
Write-Host "2. Click it to open the AI Coding Hub Chat Panel." -ForegroundColor White
Write-Host "3. The backend automatically boots on 127.0.0.1:8765 with dynamic token authentication." -ForegroundColor White
Write-Host "4. Select an agent (e.g. 'Mock Echo Agent') and type a prompt to test live SSE streaming and cancellation!" -ForegroundColor White
