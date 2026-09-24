[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$step = 'starting'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
Set-Location -LiteralPath $root

function New-Secret {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($bytes)).Replace('+', '-').Replace('/', '_')
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

try {
    $step = 'checking Docker Compose'
    & docker compose version | Out-Null

    $envPath = Join-Path $root '.env'
    $secretDir = Join-Path $root 'secrets'
    New-Item -ItemType Directory -Force -Path $secretDir | Out-Null
    $firstInstall = -not (Test-Path -LiteralPath $envPath)
    $adminPassword = $null

    if ($firstInstall) {
        $step = 'generating installation secrets'
        $wahaApiKey = New-Secret
        $wahaDashboardPassword = New-Secret
        $webhookSecret = New-Secret
        $panelDataKey = New-Secret
        $adminPassword = New-Secret

        Write-Utf8NoBom $envPath @"
WAHA_API_KEY=$wahaApiKey
WAHA_IMAGE=devlikeapro/waha:latest-2026.9.1
WAHA_IMAGE_TAG=latest-2026.9.1
WAHA_DASHBOARD_USERNAME=admin
WAHA_DASHBOARD_PASSWORD=$wahaDashboardPassword
WAHA_WEBHOOK_SECRET=$webhookSecret
PANEL_DATA_ENCRYPTION_KEY=$panelDataKey
PANEL_IMAGE=docker.io/cangnan88/waha-panel:1.0.10
PANEL_VERSION=1.0.10
PANEL_PORT=3003
PANEL_BIND_ADDRESS=127.0.0.1
WAHA_PORT=3002
WAHA_BIND_ADDRESS=127.0.0.1
PORTABLE_VOLUME_PREFIX=waha-release
PORTABLE_NETWORK_NAME=waha-release-internal
PANEL_SPONSOR_ENABLED=1
PANEL_SPONSOR_IMAGE_URL=https://www.6spring.com/wp-content/uploads/2026/09/cangnan.jpg
"@
        Write-Utf8NoBom (Join-Path $secretDir 'waha_credentials') @"
WAHA_API_KEY=$wahaApiKey
WAHA_WEBHOOK_SECRET=$webhookSecret
WAHA_DASHBOARD_USERNAME=admin
WAHA_DASHBOARD_PASSWORD=$wahaDashboardPassword
"@
        Write-Utf8NoBom (Join-Path $secretDir 'panel_admin_bootstrap') @"
PANEL_ADMIN_USERNAME=admin
PANEL_ADMIN_PASSWORD=$adminPassword
"@
    } else {
        $step = 'checking existing installation files'
        foreach ($required in @('.env', 'secrets/waha_credentials')) {
            if (-not (Test-Path -LiteralPath (Join-Path $root $required))) {
                throw "Existing .env was found but required file is missing: $required"
            }
        }
    }

    $step = 'pulling release images'
    & docker compose pull
    if ($LASTEXITCODE -ne 0) { throw 'docker compose pull failed' }
    $step = 'starting isolated release services'
    & docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed' }
    $step = 'checking panel response'
    $panelReadyDeadline = (Get-Date).AddSeconds(90)
    $lastStatus = 0
    Write-Host 'Waiting for panel to become ready (up to 90s)...'
    do {
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:3003/' -UseBasicParsing -SkipHttpErrorCheck -TimeoutSec 5
            $lastStatus = $response.StatusCode
        } catch {
            $lastStatus = 0
        }
        if ($lastStatus -in @(200, 401)) { break }
        if ((Get-Date) -lt $panelReadyDeadline) { Start-Sleep -Seconds 2 }
    } while ((Get-Date) -lt $panelReadyDeadline)
    if ($lastStatus -notin @(200, 401)) {
        $lastStatusLabel = if ($lastStatus) { [string]$lastStatus } else { '000' }
        throw "Panel did not become ready within 90 seconds (last HTTP status $lastStatusLabel). Run: docker compose logs --tail=100 waha-panel"
    }

    Write-Host 'Portable WAHA + panel release is running.'
    Write-Host 'Panel: http://127.0.0.1:3003/'
    Write-Host 'WAHA:  http://127.0.0.1:3002/'
    if ($adminPassword) {
        Write-Host 'Initial panel administrator: admin'
        Write-Host "Initial panel password (shown once): $adminPassword"
    } else {
        Write-Host 'Existing administrator credentials were preserved.'
    }
} catch {
    Write-Error ("INSTALL FAILED at {0}: {1}" -f $step, $_.Exception.Message)
    exit 1
}
