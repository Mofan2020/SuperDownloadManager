<#
.SYNOPSIS
    SDM v2.0 uninstall — removes the CLI shim and the scheduled task.

.EXAMPLE
    .\uninstall.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$TaskName = "SuperDownloadManagerDaemon"
$shimDir = Join-Path $env:LOCALAPPDATA "SuperDownloadManager\bin"
$shim = Join-Path $shimDir "sdm.cmd"

# 1. 计划任务
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[uninstall.ps1] 计划任务已删除: $TaskName"
} else {
    Write-Host "[uninstall.ps1] 没有计划任务: $TaskName"
}

# 2. CLI shim
if (Test-Path $shim) {
    Remove-Item $shim -Force
    Write-Host "[uninstall.ps1] CLI shim 已删除: $shim"
}

# 3. PATH 里的那一段
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath) {
    $parts = $userPath -split ";" | Where-Object { $_ -ne "" -and $_ -ne $shimDir }
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
    Write-Host "[uninstall.ps1] 已从用户 PATH 移除 $shimDir"
}

# 4. 空的 bin 目录
if ((Test-Path $shimDir) -and -not (Get-ChildItem $shimDir -Force | Select-Object -First 1)) {
    Remove-Item $shimDir -Recurse -Force
}

Write-Host "[uninstall.ps1] 完成。程序文件和数据目录没有动，手动删即可。"
