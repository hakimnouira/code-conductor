$ErrorActionPreference = "Stop"

$taskName = "CodeConductorBackgroundBackend"
$runner = Join-Path $PSScriptRoot "run-background-backend.ps1"
if (-not (Test-Path $runner -PathType Leaf)) {
    throw "Background backend runner not found: $runner"
}

$powerShell = Join-Path $PSHOME "powershell.exe"
$arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $runner + '"'
$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Keep the local Code Conductor backend and durable workflow scheduler running while the user is signed in." -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "Installed and started the per-user Code Conductor background backend task."
Write-Host "The task starts at logon, restarts the backend after failures, and does not launch the dashboard."
Write-Host "Run .\scripts\remove-background-task.ps1 to uninstall it."
