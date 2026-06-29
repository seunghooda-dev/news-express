$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$LogPath = Join-Path $Root "data\tmp\cloudflare_quick_tunnel.err.log"
$Process = Get-Process cloudflared -ErrorAction SilentlyContinue

if (-not $Process) {
    throw "cloudflared 프로세스가 실행 중이 아닙니다. scripts\start_cloudflare_quick_tunnel.ps1을 다시 실행하세요."
}

if (-not (Test-Path -LiteralPath $LogPath)) {
    throw "Cloudflare Quick Tunnel 로그를 찾지 못했습니다: $LogPath"
}

$Matches = Select-String -LiteralPath $LogPath -Pattern "https://[-a-zA-Z0-9]+\.trycloudflare\.com" -AllMatches |
    ForEach-Object { $_.Matches.Value }
$PublicUrl = $Matches | Select-Object -Last 1

if (-not $PublicUrl) {
    throw "Cloudflare Quick Tunnel 공개 주소를 로그에서 찾지 못했습니다."
}

$Response = Invoke-WebRequest -Uri $PublicUrl -UseBasicParsing -TimeoutSec 15
if ($Response.StatusCode -lt 200 -or $Response.StatusCode -ge 400) {
    throw "외부 주소 응답이 비정상입니다. status=$($Response.StatusCode) url=$PublicUrl"
}

Write-Output "Cloudflare Quick Tunnel 정상"
Write-Output "URL: $PublicUrl"
Write-Output "Status: $($Response.StatusCode)"
