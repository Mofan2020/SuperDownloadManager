<#
.SYNOPSIS
    SDM v2.0 install — puts the CLI on PATH and registers the daemon.

.DESCRIPTION
    Windows 上没有 launchd 这样的轻量后台管理器，用计划任务
    （Task Scheduler）代替：注册一个「登录时启动」的常驻任务跑
    sdmd.exe。

    CLI 用 .cmd shim 放到 PATH 上——Windows 没有软链（除非开开发者
    模式），.cmd shim 是最稳的做法。

.PARAMETER Root
    SDM 的安装目录。脚本默认找脚本自己所在的目录的上级。

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Root "C:\Program Files\SuperDownloadManager"
#>
[CmdletBinding()]
param(
    [string]$Root = "",
    [switch]$NoTask
)

$ErrorActionPreference = "Stop"

$TaskName = "SuperDownloadManagerDaemon"

function Resolve-SdmRoot {
    param([string]$Given)

    if ($Given) {
        if (Test-Path $Given) { return (Resolve-Path $Given).Path }
        throw "指定的目录不存在：$Given"
    }

    # 默认：脚本在 <root>/scripts/install.ps1
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $candidate = Split-Path -Parent $here
    if ($candidate -and (Test-Path (Join-Path $candidate "sdm"))) {
        return (Resolve-Path $candidate).Path
    }
    # 或者就在当前目录
    $cwd = (Get-Location).Path
    if (Test-Path (Join-Path $cwd "sdm")) { return $cwd }
    if (Test-Path (Join-Path $cwd "sdm-Windows")) { return (Resolve-Path (Join-Path $cwd "sdm-Windows")).Path }

    throw "找不到 SDM 目录。请解压全部压缩包到同一个目录，或用 -Root 指定。"
}

$root = Resolve-SdmRoot -Given $Root
Write-Host "[install.ps1] SDM 目录: $root"

# 1. CLI：找 sdm.exe
$cliExe = Join-Path $root "sdm\sdm.exe"
if (-not (Test-Path $cliExe)) {
    $alt = Join-Path $root "sdm.exe"
    if (Test-Path $alt) { $cliExe = $alt } else { throw "找不到 CLI：$cliExe" }
}
Write-Host "[install.ps1] CLI: $cliExe"

# 2. .cmd shim 放到 PATH 上的目录
$shimDir = Join-Path $env:LOCALAPPDATA "SuperDownloadManager\bin"
New-Item -ItemType Directory -Force -Path $shimDir | Out-Null
$shim = Join-Path $shimDir "sdm.cmd"
@"
@echo off
REM SDM CLI shim
"$cliExe" %*
"@ | Set-Content -Path $shim -Encoding ASCII

# 把 shim 目录加进用户 PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
$parts = $userPath -split ";" | Where-Object { $_ -ne "" }
if ($parts -notcontains $shimDir) {
    $newPath = (($parts + $shimDir) -join ";")
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "[install.ps1] 已把 $shimDir 加进用户 PATH"
    Write-Host "[install.ps1] 新的 shell 里生效（当前窗口要手动加）"
} else {
    Write-Host "[install.ps1] PATH 已包含 $shimDir"
}
Write-Host "[install.ps1] CLI shim: $shim"

# 3. daemon 计划任务
$daemonExe = Join-Path $root "sdmd\sdmd.exe"
if (-not (Test-Path $daemonExe)) {
    $alt = Join-Path $root "sdmd.exe"
    if (Test-Path $alt) { $daemonExe = $alt }
}

if ($NoTask) {
    Write-Host "[install.ps1] 跳过计划任务注册（-NoTask）"
} elseif (Test-Path $daemonExe) {
    Write-Host "[install.ps1] daemon: $daemonExe"

    $action = New-ScheduledTaskAction -Execute $daemonExe
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

    # 先删掉旧的再注册——重复运行不会报错
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "SuperDownloadManager background daemon" | Out-Null
    Write-Host "[install.ps1] 计划任务已注册: $TaskName"
    Write-Host "[install.ps1] 立即启动: Start-ScheduledTask -TaskName $TaskName"
    Start-ScheduledTask -TaskName $TaskName
} else {
    Write-Host "[install.ps1] 找不到 daemon，跳过计划任务注册" -ForegroundColor Yellow
}

Write-Host "[install.ps1] 完成。验证：sdm ping"
