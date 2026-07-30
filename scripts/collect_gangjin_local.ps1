# 해외 IP가 차단되는 강진군 보도자료를 로컬(한국 IP)에서 수집해 공유 DB(Supabase)에 저장한다.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    & (Join-Path $PSScriptRoot "setup.ps1")
}

Set-Location $Root
& $Python -m news_summary.cli collect --source gangjin-county --limit 10
