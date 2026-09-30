[CmdletBinding()]
param(
    [string]$TaskName = "DevDeck",
    [string]$Distro = "Ubuntu",
    [string]$LinuxProjectDir = "/home/lsl/project/github/DevDeck",
    [string]$Python = "/home/lsl/miniconda3/bin/python",
    [int]$Port = 7310,
    [string]$DataDir = "",
    [string]$UnitName = "devdeck.service"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = "$LinuxProjectDir/.devdeck"
}

$launcherDirectory = Join-Path $env:LOCALAPPDATA "DevDeck"
$launcherPath = Join-Path $launcherDirectory "start-devdeck.ps1"
$sourceLauncher = Join-Path $PSScriptRoot "start-devdeck.ps1"
New-Item -ItemType Directory -Path $launcherDirectory -Force | Out-Null
Copy-Item -Path $sourceLauncher -Destination $launcherPath -Force

# PowerShell passes each WSL argument separately, avoiding Task Scheduler quote loss.
$powershellPath = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$launcherArguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$launcherPath`" -Distro `"$Distro`" -LinuxProjectDir `"$LinuxProjectDir`" -Python `"$Python`" -Port $Port -DataDir `"$DataDir`" -UnitName `"$UnitName`""

# Start at user logon without administrator privileges; -Force replaces the task.
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument $launcherArguments
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Start the DevDeck console in WSL after Windows user logon" `
    -Force | Out-Null

Write-Output "Registered Windows logon task: $TaskName"
Write-Output "Launcher: $launcherPath"
