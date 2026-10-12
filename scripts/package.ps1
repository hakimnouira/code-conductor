# Packaging script for Unified AI Coding Hub (.vsix)
$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Unified AI Coding Hub - Production Packaging (.vsix)   " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$WorkspaceRoot = Split-Path -Parent $PSScriptRoot
$ExtensionDir = Join-Path $WorkspaceRoot "extension"
$DistDir = Join-Path $WorkspaceRoot "dist"

# 1. Clean previous build artifacts
Write-Host "`n[1/4] Cleaning build artifacts..." -ForegroundColor Yellow
if (Test-Path $DistDir) {
    Remove-Item -Recurse -Force $DistDir
}
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

$OutDir = Join-Path $ExtensionDir "out"
if (Test-Path $OutDir) {
    Remove-Item -Recurse -Force $OutDir
}

# 2. TypeCheck & Compile TypeScript
Write-Host "`n[2/4] Compiling TypeScript extension..." -ForegroundColor Yellow
Set-Location $ExtensionDir
cmd.exe /c "npm.cmd run compile"
if ($LASTEXITCODE -ne 0) {
    Write-Host "TypeScript compilation failed!" -ForegroundColor Red
    exit $LASTEXITCODE
}

# 3. Package extension with @vscode/vsce
Write-Host "`n[3/4] Building standalone .vsix package..." -ForegroundColor Yellow
$VsixName = "unified-ai-hub-0.1.0.vsix"
$OutputVsix = Join-Path $DistDir $VsixName

cmd.exe /c "npx.cmd @vscode/vsce package --no-git-tag-version -o `"$OutputVsix`""
if ($LASTEXITCODE -ne 0) {
    Write-Host "VSCE packaging failed!" -ForegroundColor Red
    exit $LASTEXITCODE
}

# 4. Verify artifact size and presence
Write-Host "`n[4/4] Verifying packaging integrity..." -ForegroundColor Yellow
if (Test-Path $OutputVsix) {
    $Item = Get-Item $OutputVsix
    $SizeKb = [math]::Round($Item.Length / 1024, 2)
    Write-Host "`n==========================================================" -ForegroundColor Green
    Write-Host " Packaging Successful!" -ForegroundColor Green
    Write-Host " Artifact: $OutputVsix" -ForegroundColor Green
    Write-Host " Package Size: $SizeKb KB" -ForegroundColor Green
    Write-Host "==========================================================" -ForegroundColor Green
} else {
    Write-Host "Package verification failed: Output file missing." -ForegroundColor Red
    exit 1
}

Set-Location $WorkspaceRoot
