# Install a user-wide cxcode command for Windows.
# Run from the Code Conductor checkout:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-cxcode.ps1

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "This installer is for Windows 10/11."
}

$WorkspaceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LauncherSource = Join-Path $WorkspaceRoot "Start_CodeConductor.bat"
if (-not (Test-Path $LauncherSource)) {
    throw "Start_CodeConductor.bat was not found. Run this installer from a valid Code Conductor checkout."
}

$BinDir = Join-Path $env:USERPROFILE "bin"
$CommandPath = Join-Path $BinDir "cxcode.cmd"
New-Item -ItemType Directory -Path $BinDir -Force | Out-Null

$Launcher = @'
@echo off
setlocal
set "PROJECT_DIR=__PROJECT_DIR__"

if not exist "%PROJECT_DIR%\Start_CodeConductor.bat" (
  echo [ERROR] Code Conductor checkout not found at "%PROJECT_DIR%".
  echo Re-run scripts\install-cxcode.ps1 from the current checkout.
  exit /b 2
)

if /I "%~1"=="stop" goto :stop
if /I "%~1"=="test" goto :test
if /I "%~1"=="setup" goto :setup
if /I "%~1"=="where" goto :where
if not "%~1"=="" if /I not "%~1"=="start" (
  echo Usage: cxcode [start^|stop^|test^|setup^|where]
  exit /b 2
)
goto :start

:start
if not exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
  echo [ERROR] Python environment is missing.
  echo Run: cxcode setup
  exit /b 3
)
if not exist "%PROJECT_DIR%\web\node_modules" (
  echo [ERROR] Web dependencies are missing.
  echo Run: cxcode setup
  exit /b 3
)
call "%PROJECT_DIR%\Start_CodeConductor.bat"
exit /b %ERRORLEVEL%

:stop
call "%PROJECT_DIR%\Stop_CodeConductor.bat"
exit /b %ERRORLEVEL%

:test
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\test.ps1"
exit /b %ERRORLEVEL%

:setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%\scripts\setup.ps1"
exit /b %ERRORLEVEL%

:where
echo %PROJECT_DIR%
exit /b 0
'@

$Launcher = $Launcher.Replace("__PROJECT_DIR__", $WorkspaceRoot)
Set-Content -LiteralPath $CommandPath -Value $Launcher -Encoding Ascii

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathEntries = @()
if ($UserPath) {
    $PathEntries = @($UserPath -split ";" | Where-Object { $_ })
}
if (-not ($PathEntries | Where-Object { $_.TrimEnd("\") -ieq $BinDir.TrimEnd("\") })) {
    [Environment]::SetEnvironmentVariable("Path", ((@($PathEntries) + $BinDir) -join ";"), "User")
}
if (-not (($env:Path -split ";") | Where-Object { $_.TrimEnd("\") -ieq $BinDir.TrimEnd("\") })) {
    $env:Path = "$BinDir;$env:Path"
}

Write-Host ""
Write-Host "Installed the global command: $CommandPath" -ForegroundColor Green
Write-Host "Use it from any folder:" -ForegroundColor Cyan
Write-Host "  cxcode         Start the Code Conductor dashboard"
Write-Host "  cxcode stop    Stop the backend and dashboard"
Write-Host "  cxcode test    Run backend tests and extension compile"
Write-Host "  cxcode setup   Install/repair local dependencies"
Write-Host "  cxcode where   Show the checkout used by the command"
Write-Host ""
Write-Host "If an already-open terminal cannot find cxcode, open a new terminal." -ForegroundColor Yellow
