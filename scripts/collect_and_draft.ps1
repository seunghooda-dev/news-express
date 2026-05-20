$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    & (Join-Path $PSScriptRoot "setup.ps1")
}

& $Python -m news_summary.cli run --limit 5
