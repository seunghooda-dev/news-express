# 강진군 로컬 수집(collect_gangjin_local.ps1)을 1시간 간격 예약 작업으로 등록한다.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$CollectScript = Join-Path $PSScriptRoot "collect_gangjin_local.ps1"
$TaskName = "News Express Gangjin Local Collect"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$CollectScript`"" `
    -WorkingDirectory $Root

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 60)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($logonTrigger, $repeatTrigger) `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
} catch {
    $taskCommand = "$PowerShell -NoProfile -ExecutionPolicy Bypass -File $CollectScript"
    $command = "schtasks.exe /Create /TN `"$TaskName`" /TR `"$taskCommand`" /SC HOURLY /F"
    cmd.exe /c $command
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks.exe failed with exit code $LASTEXITCODE"
    }
}

& schtasks.exe /Query /TN $TaskName | Out-Null
Write-Output "Registered scheduled task: $TaskName (hourly)"
