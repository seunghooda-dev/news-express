$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

function Find-Python {
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($systemPython) {
        try {
            & $systemPython.Source --version | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return $systemPython.Source
            }
        } catch {
        }
    }

    $codexPython = Join-Path $HOME ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path $codexPython) {
        return $codexPython
    }

    throw "파이썬 3.11 이상을 찾지 못했습니다. https://www.python.org/downloads/windows/ 에서 설치한 뒤 다시 실행하세요."
}

if (-not (Test-Path $VenvPython)) {
    $Python = Find-Python
    & $Python -m venv (Join-Path $Root ".venv")
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e $Root
& $VenvPython -m news_summary.cli init-db

$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
}

Write-Host "설정이 끝났습니다."
Write-Host "실행: .\scripts\run_review_app.ps1"
