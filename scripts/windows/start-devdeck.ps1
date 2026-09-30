[CmdletBinding()]
param(
    [string]$Distro = "Ubuntu",
    [string]$LinuxProjectDir = "/home/lsl/project/github/DevDeck",
    [string]$Python = "/home/lsl/miniconda3/bin/python",
    [int]$Port = 7310,
    [string]$DataDir = "/home/lsl/project/github/DevDeck/.devdeck",
    [string]$UnitName = "devdeck.service"
)

$ErrorActionPreference = "Stop"
$wslPath = Join-Path $env:WINDIR "System32\wsl.exe"
$linuxScript = "$LinuxProjectDir/scripts/linux/start-devdeck.sh"

# WSL 里直接 nohup 或 systemd-run --no-block 后退出 wsl.exe，用户 systemd 会进入
# exit.target，把刚拉起的服务停掉。即使单元已经是 enabled，这次登录任务的
# wsl.exe 退出同样会拆掉用户 systemd。因此必须留下一个隐藏会话。
# 端口探测走 Windows 本机 TCP，避免再开一个短命 wsl.exe 会话。
$holdArguments = @(
    "-d", $Distro, "--",
    "bash", $linuxScript,
    "--python", $Python,
    "--project-dir", $LinuxProjectDir,
    "--port", "$Port",
    "--data-dir", $DataDir,
    "--unit", $UnitName,
    "--hold"
)
Start-Process -FilePath $wslPath -ArgumentList $holdArguments -WindowStyle Hidden | Out-Null

$deadline = (Get-Date).AddSeconds(45)
do {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $Port)
        $client.Close()
        exit 0
    } catch {
        if ($client.Connected) {
            $client.Close()
        }
    }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)

Write-Error "DevDeck 未能在超时前监听 127.0.0.1:$Port"
exit 1
