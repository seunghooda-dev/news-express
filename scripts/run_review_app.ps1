$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    & (Join-Path $PSScriptRoot "setup.ps1")
}

& $Python -m news_summary.cli serve --host 127.0.0.1 --port 5000
