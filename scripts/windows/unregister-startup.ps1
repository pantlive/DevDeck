[CmdletBinding()]
param(
    [string]$TaskName = "DevDeck",
    [string]$Distro = "Ubuntu",
    [string]$UnitName = "devdeck.service"
)

$ErrorActionPreference = "Stop"

# 取消登录自启：移除 Windows 任务并 disable 用户单元，不停止当前已运行的控制台。
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
$wslPath = Join-Path $env:WINDIR "System32\wsl.exe"
$null = & $wslPath -d $Distro -- systemctl --user disable $UnitName
Write-Output "Removed Windows logon task: $TaskName"
Write-Output "Disabled systemd user unit: $UnitName"
