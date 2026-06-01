param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$SetupScript = Join-Path $PSScriptRoot "setup.ps1"

if (-not (Test-Path -LiteralPath $Python)) {
    & $SetupScript
}

Push-Location $Root
try {
    if ($DryRun) {
        & $Python -m news_summary.cli restore $BackupPath --dry-run
    } else {
        & $Python -m news_summary.cli restore $BackupPath --yes
    }
} finally {
    Pop-Location
}
