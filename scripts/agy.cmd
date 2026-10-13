@echo off
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE="

if exist "%SCRIPT_DIR%..\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%SCRIPT_DIR%..\.venv\Scripts\python.exe"
) else if exist "c:\Users\hakim\Desktop\vs extention\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=c:\Users\hakim\Desktop\vs extention\.venv\Scripts\python.exe"
) else if exist "D:\work\code-conductor\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=D:\work\code-conductor\.venv\Scripts\python.exe"
) else if exist "C:\Users\hakim\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe" (
    set "PYTHON_EXE=C:\Users\hakim\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
)

if not defined PYTHON_EXE (
    echo Error: Could not locate a valid Python interpreter for Antigravity CLI. >&2
    exit /b 1
)

"%PYTHON_EXE%" "%SCRIPT_DIR%agy_bridge.py" %*
