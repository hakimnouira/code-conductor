$ErrorActionPreference = "Stop"

$taskName = "CodeConductorBackgroundBackend"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Background backend task is not installed."
    exit 0
}

Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
Write-Host "Removed the per-user Code Conductor background backend task."
Write-Host "This does not stop a backend started separately by Start_CodeConductor.bat or cxcode."
