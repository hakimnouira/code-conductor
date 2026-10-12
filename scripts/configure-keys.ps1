<#
.SYNOPSIS
    Interactive Key & Subscription Configurator for Code Conductor.
.DESCRIPTION
    Launches Python configuration assistant using project virtual environment.
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot = Split-Path -Parent $ScriptDir
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

$ConfigScript = Join-Path $ScriptDir "configure_keys.py"
& $PythonExe $ConfigScript
