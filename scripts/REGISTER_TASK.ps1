param(
    [ValidateRange(1,10080)][int]$IntervalMinutes = 60,
    [string]$TaskName = "SCUBACollector",
    [string]$DataDirectory = (Join-Path $env:LOCALAPPDATA "SCUBACollector")
)
$ErrorActionPreference = "Stop"
$ProjectDirectory = Split-Path $PSScriptRoot -Parent
$PythonExe = Join-Path $ProjectDirectory ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    throw "Run START_WINDOWS.bat first to prepare the Python environment."
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Task '$TaskName' already exists. Remove it or choose another -TaskName."
}
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "-m collector --once --home `"$DataDirectory`"" `
    -WorkingDirectory $ProjectDirectory
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -Principal $Principal `
    -Description "SCUBA Community Collector: one collection cycle per interval. Close the GUI when using this task."
Write-Host "Registered task '$TaskName' every $IntervalMinutes minutes for $Identity."
Write-Host "PC must be awake and this user must be logged in. Close the GUI to release the data-directory lock."
