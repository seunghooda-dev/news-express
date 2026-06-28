param(
    [Parameter(Mandatory = $true)]
    [string]$Hostname,

    [string]$TunnelName = "news-express",
    [string]$LocalUrl = "http://127.0.0.1:5000"
)

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

$CloudflaredDir = Join-Path $env:USERPROFILE ".cloudflared"
$CertPath = Join-Path $CloudflaredDir "cert.pem"
if (-not (Test-Path -LiteralPath $CertPath)) {
    throw "Cloudflare 인증서가 없습니다. 먼저 cloudflared tunnel login을 실행해 Cloudflare 계정과 도메인을 승인하세요."
}

New-Item -ItemType Directory -Force -Path $CloudflaredDir | Out-Null

$tunnels = @()
try {
    $json = & $CloudflaredPath tunnel list --output json 2>$null
    if ($json) {
        $tunnels = @($json | ConvertFrom-Json)
    }
} catch {
    $tunnels = @()
}

$tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if (-not $tunnel) {
    & $CloudflaredPath tunnel create $TunnelName
    $json = & $CloudflaredPath tunnel list --output json
    $tunnels = @($json | ConvertFrom-Json)
    $tunnel = $tunnels | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
}
if (-not $tunnel) {
    throw "터널을 만들지 못했습니다: $TunnelName"
}

$TunnelId = $tunnel.id
$credentialsFile = Join-Path $CloudflaredDir "$TunnelId.json"
if (-not (Test-Path -LiteralPath $credentialsFile)) {
    throw "터널 credentials 파일을 찾지 못했습니다: $credentialsFile"
}

$ConfigPath = Join-Path $CloudflaredDir "config.yml"
if ((Test-Path -LiteralPath $ConfigPath) -and -not (Test-Path -LiteralPath "$ConfigPath.before-news-express")) {
    Copy-Item -LiteralPath $ConfigPath -Destination "$ConfigPath.before-news-express" -Force
}
@"
tunnel: $TunnelId
credentials-file: $credentialsFile

ingress:
  - hostname: $Hostname
    service: $LocalUrl
  - service: http_status:404
"@ | Set-Content -LiteralPath $ConfigPath -Encoding UTF8

& $CloudflaredPath tunnel route dns $TunnelName $Hostname
& $CloudflaredPath service install

Write-Output "Cloudflare Tunnel 설치 완료: https://$Hostname"
