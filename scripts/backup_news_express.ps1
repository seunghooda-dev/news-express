$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$SetupScript = Join-Path $PSScriptRoot "setup.ps1"

if (-not (Test-Path -LiteralPath $Python)) {
    & $SetupScript
}

Push-Location $Root
try {
    & $Python -m news_summary.cli backup --output-dir "data\backups"
} finally {
    Pop-Location
}
