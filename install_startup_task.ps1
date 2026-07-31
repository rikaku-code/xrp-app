# 注册 XRP 监控为「登录时自动启动」任务（关闭 Cursor 后仍运行）
# 用法：PowerShell 中右键「以管理员身份运行」或在普通 PowerShell 执行：
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#   .\install_startup_task.ps1

$TaskName = "XRP_Monitor_24h"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatchFile = Join-Path $ProjectDir "run_monitor_24h.bat"

if (-not (Test-Path $BatchFile)) {
    Write-Error "找不到 run_monitor_24h.bat：$BatchFile"
    exit 1
}

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatchFile`"" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "XRP/JPY 24h 监控，Lark 推送（0-8点静默）" `
    -Force | Out-Null

Write-Host "已注册计划任务：$TaskName"
Write-Host "登录 Windows 后将自动启动监控。"
Write-Host ""
Write-Host "常用命令："
Write-Host "  立即启动：schtasks /Run /TN $TaskName"
Write-Host "  停止任务：schtasks /End /TN $TaskName"
Write-Host "  删除任务：Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
