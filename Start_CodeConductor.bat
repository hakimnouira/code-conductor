@echo off
title Code Conductor Launcher
echo ========================================================
echo       Starting Code Conductor AI Engineering Studio
echo ========================================================

cd /d "D:\work\code-conductor"

echo [*] Starting Backend server on http://127.0.0.1:8765...
start /b "" "D:\work\code-conductor\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8765

timeout /t 2 /nobreak >nul

echo [*] Starting Web Interface on http://127.0.0.1:5173...
start /b "" cmd.exe /c "npm --prefix web run dev"

timeout /t 2 /nobreak >nul

set "CLOAK_EXE=D:\Downloads\cloakbrowser-windows-x64\chrome.exe"
set "CLOAK_PROFILE=D:\work\code-conductor\.cloak_profile"

if exist "%CLOAK_EXE%" (
    echo [*] Opening in CloakBrowser with stealth session and port 9222...
    start "" "%CLOAK_EXE%" --remote-debugging-port=9222 --user-data-dir="%CLOAK_PROFILE%" --no-first-run --no-default-browser-check "http://127.0.0.1:5173"
) else (
    echo [*] Opening system browser...
    start http://127.0.0.1:5173
)

echo.
echo ========================================================
echo   Code Conductor is running!
echo   To STOP and turn off servers, run Stop_CodeConductor.bat
echo   or click the Turn Off button inside the app.
echo ========================================================
