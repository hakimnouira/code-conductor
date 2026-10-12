@echo off
title Stop Code Conductor
echo ========================================================
echo         Stopping Code Conductor Servers
echo ========================================================

echo [*] Freeing port 8765 (Backend)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8765 ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1

echo [*] Freeing port 5173 (Frontend)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5173 ^| findstr LISTENING') do taskkill /f /pid %%a >nul 2>&1

echo [OK] Code Conductor has been completely shut down.
timeout /t 3
