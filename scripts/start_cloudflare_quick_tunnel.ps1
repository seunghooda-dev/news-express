$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$Cloudflared = Get-Command cloudflared -ErrorAction SilentlyContinue
if (-not $Cloudflared) {
    $WingetLink = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\cloudflared.exe"
    if (Test-Path -LiteralPath $WingetLink) {
        $Cloudflared = Get-Item -LiteralPath $WingetLink
    }
}
if (-not $Cloudflared) {
    throw "cloudflared가 설치되어 있지 않습니다. winget install --id Cloudflare.cloudflared 명령으로 먼저 설치하세요."
}
$CloudflaredPath = if ($Cloudflared.PSObject.Properties.Name -contains "Source") { $Cloudflared.Source } else { $Cloudflared.FullName }

& (Join-Path $PSScriptRoot "ensure_news_express_running.ps1")
& $CloudflaredPath tunnel --url http://127.0.0.1:5000
