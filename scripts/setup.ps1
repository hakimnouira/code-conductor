# Setup script for Unified AI Coding Hub on Windows
$ErrorActionPreference = "Stop"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Unified AI Coding Hub - Windows Environment Setup       " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$WorkspaceRoot = Split-Path -Parent $PSScriptRoot
Set-Location $WorkspaceRoot

# 1. Discover Python (3.11+)
Write-Host "`n[1/4] Checking Python 3.11+ prerequisite..." -ForegroundColor Yellow

# Check PATH for uv first
$UvCmd = Get-Command uv -ErrorAction SilentlyContinue
$UvExe = if ($UvCmd) { $UvCmd.Source } else { $null }

if (-not $UvExe) {
    $CommonUv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $CommonUv) {
        $UvExe = $CommonUv
    }
}

# 1. Discover Python
$Candidates = @()

# If uv is present, ask uv for installed Python 3.11+
if ($UvExe) {
    try {
        $UvFound = & $UvExe python find 3.11 2>$null
        if ($UvFound -and (Test-Path $UvFound.Trim())) {
            $Candidates += $UvFound.Trim()
        }
    } catch {}
}

# Add python commands from PATH, skipping Microsoft Store 0-byte stub aliases
Get-Command python -All -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Source -notlike "*WindowsApps*") {
        $Candidates += $_.Source
    }
}
Get-Command py -All -ErrorAction SilentlyContinue | ForEach-Object {
    $Candidates += $_.Source
}

# Add standard Python install directories
$CommonPythonDirs = @(
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:ProgramFiles\Python311\python.exe",
    "$env:ProgramFiles\Python312\python.exe",
    "$env:APPDATA\uv\python\cpython-3.11*\python.exe"
)
foreach ($p in $CommonPythonDirs) {
    $matches = Resolve-Path $p -ErrorAction SilentlyContinue
    if ($matches) {
        $Candidates += $matches[0].Path
    }
}

# Find first candidate that actually runs and is >= 3.11
foreach ($cand in $Candidates) {
    try {
        $check = & $cand -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = $cand
            $VersionCheck = $check
            break
        }
    } catch {}
}

if (-not $PythonExe) {
    Write-Host "`n[ERROR] Python is not installed or not found in PATH." -ForegroundColor Red
    Write-Host "Please install Python 3.11+ from https://www.python.org/downloads/ or via 'winget install Python.Python.3.11'." -ForegroundColor Red
    exit 1
}

# Validate Python version >= 3.11
try {
    $VersionCheck = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "`n[ERROR] Python version $VersionCheck is not supported. Python 3.11 or higher is required." -ForegroundColor Red
        exit 1
    }
    Write-Host "Found compatible Python: $PythonExe (v$VersionCheck)" -ForegroundColor Green
} catch {
    Write-Host "`n[ERROR] Failed to execute Python: $_" -ForegroundColor Red
    exit 1
}

# 2. Virtual Environment Setup
Write-Host "`n[2/4] Setting up Python virtual environment..." -ForegroundColor Yellow
$VenvDir = Join-Path $WorkspaceRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating new virtual environment at $VenvDir..." -ForegroundColor Gray
    if ($UvExe) {
        & $UvExe venv "$VenvDir" --python $PythonExe
    } else {
        & $PythonExe -m venv "$VenvDir"
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Write-Host "`n[ERROR] Failed to create virtual environment at $VenvDir." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "Reusing existing virtual environment at $VenvDir" -ForegroundColor Green
}

# 3. Install backend dependencies
Write-Host "`n[3/4] Installing backend dependencies..." -ForegroundColor Yellow
$ReqFile = Join-Path $WorkspaceRoot "backend\requirements.txt"
if (-not (Test-Path $ReqFile)) {
    Write-Host "`n[ERROR] requirements.txt not found at $ReqFile." -ForegroundColor Red
    exit 1
}

if ($UvExe) {
    & $UvExe pip install --python $VenvPython -r $ReqFile
} else {
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r $ReqFile
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[ERROR] Failed to install backend dependencies." -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host "Backend dependencies installed successfully." -ForegroundColor Green

# 4. Node.js & Extension Build
Write-Host "`n[4/5] Setting up VS Code Extension dependencies..." -ForegroundColor Yellow
$NpmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $NpmCmd) {
    $NpmCmd = Get-Command npm -ErrorAction SilentlyContinue
}

if (-not $NpmCmd) {
    Write-Host "`n[ERROR] Node.js / npm is not installed or not in PATH." -ForegroundColor Red
    Write-Host "Please install Node.js (v18+) from https://nodejs.org/ or via 'winget install OpenJS.NodeJS.LTS'." -ForegroundColor Red
    exit 1
}

$ExtensionDir = Join-Path $WorkspaceRoot "extension"
Set-Location $ExtensionDir

Write-Host "Installing npm packages in extension directory..." -ForegroundColor Gray
cmd.exe /c "npm.cmd install --no-audit --no-fund"
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[ERROR] 'npm install' failed in $ExtensionDir." -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "Compiling TypeScript extension..." -ForegroundColor Gray
cmd.exe /c "npm.cmd run compile"
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[ERROR] TypeScript compilation failed." -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host "Extension compiled successfully." -ForegroundColor Green

Write-Host "Extension compiled successfully." -ForegroundColor Green

# 5. Web dashboard dependencies (required by Start_CodeConductor.bat)
Write-Host "`n[5/5] Setting up web dashboard dependencies..." -ForegroundColor Yellow
$WebDir = Join-Path $WorkspaceRoot "web"
if (-not (Test-Path (Join-Path $WebDir "package-lock.json"))) {
    Write-Host "`n[ERROR] web/package-lock.json is missing." -ForegroundColor Red
    exit 1
}
Set-Location $WebDir
Write-Host "Installing web dashboard packages..." -ForegroundColor Gray
cmd.exe /c "npm.cmd ci --no-audit --no-fund"
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[ERROR] 'npm ci' failed in $WebDir." -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host "Web dashboard dependencies installed successfully." -ForegroundColor Green

Set-Location $WorkspaceRoot
Write-Host "`n==========================================================" -ForegroundColor Green
Write-Host "  Setup completed successfully!                           " -ForegroundColor Green
Write-Host "  Virtual environment: $VenvDir" -ForegroundColor Green
Write-Host "  Run '.\scripts\test.ps1' to verify tests." -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green
