$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunScript = Join-Path $PSScriptRoot "run_review_app.ps1"
$Url = "http://127.0.0.1:5000"

function Test-NewsExpressRunning {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

if (-not (Test-NewsExpressRunning)) {
    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $RunScript) `
        -WorkingDirectory $Root `
        -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Seconds 1
        if (Test-NewsExpressRunning) {
            break
        }
    } while ((Get-Date) -lt $deadline)
}

Start-Process $Url
