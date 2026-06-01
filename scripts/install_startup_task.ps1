$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnsureScript = Join-Path $PSScriptRoot "ensure_news_express_running.ps1"
$TaskName = "News Express Keepalive"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$WrapperDir = Join-Path $env:LOCALAPPDATA "NewsExpress"
$WrapperScript = Join-Path $WrapperDir "keepalive.ps1"

New-Item -ItemType Directory -Force -Path $WrapperDir | Out-Null
Set-Content -LiteralPath $WrapperScript -Encoding UTF8 -Value @"
`$ErrorActionPreference = "Stop"
& "$EnsureScript"
"@

$action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WrapperScript`"" `
    -WorkingDirectory $Root

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
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
    $taskCommand = "$PowerShell -NoProfile -ExecutionPolicy Bypass -File $WrapperScript"
    $command = "schtasks.exe /Create /TN `"$TaskName`" /TR `"$taskCommand`" /SC MINUTE /MO 5 /F"
    cmd.exe /c $command
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks.exe failed with exit code $LASTEXITCODE"
    }
}

& schtasks.exe /Query /TN $TaskName | Out-Null
Write-Output "Registered scheduled task: $TaskName"
