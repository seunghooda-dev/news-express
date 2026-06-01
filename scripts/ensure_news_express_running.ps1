$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$SetupScript = Join-Path $PSScriptRoot "setup.ps1"
$Url = "http://127.0.0.1:5000"
$LogDir = Join-Path $Root "data\logs"
$StdoutLog = Join-Path $LogDir "news_express_stdout.log"
$StderrLog = Join-Path $LogDir "news_express_stderr.log"

function Test-NewsExpressRunning {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    & $SetupScript
}

if (Test-NewsExpressRunning) {
    Write-Output "News Express is already running."
    exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Start-Process `
    -FilePath $Python `
    -ArgumentList @("-m", "news_summary.cli", "serve", "--host", "127.0.0.1", "--port", "5000") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog

$deadline = (Get-Date).AddSeconds(45)
do {
    Start-Sleep -Seconds 1
    if (Test-NewsExpressRunning) {
        Write-Output "News Express started."
        exit 0
    }
} while ((Get-Date) -lt $deadline)

Write-Error "News Express did not start within 45 seconds. Check $StderrLog"
