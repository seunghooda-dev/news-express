$ErrorActionPreference = "Stop"

$EnsureScript = Join-Path $PSScriptRoot "ensure_news_express_running.ps1"
$Url = "http://127.0.0.1:5000"

& $EnsureScript
Start-Process $Url
