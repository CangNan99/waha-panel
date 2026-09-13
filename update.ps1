[CmdletBinding()]
param(
    [ValidateSet('panel', 'waha')]
    [string]$Component = 'panel'
)

$ErrorActionPreference = 'Stop'
$step = 'starting'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
Set-Location -LiteralPath $root

function Get-EnvValue([string]$Name) {
    $line = Get-Content -LiteralPath (Join-Path $root '.env') | Where-Object { $_ -match "^$([regex]::Escape($Name))=" } | Select-Object -First 1
    if ($null -eq $line) { return '' }
    return ($line -split '=', 2)[1]
}

function Set-EnvValue([string]$Name, [string]$Value) {
    $path = Join-Path $root '.env'
    $lines = [Collections.Generic.List[string]]::new()
    Get-Content -LiteralPath $path | ForEach-Object { [void]$lines.Add($_) }
    $pattern = "^$([regex]::Escape($Name))="
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) { $lines[$i] = "$Name=$Value"; $found = $true; break }
    }
    if (-not $found) { [void]$lines.Add("$Name=$Value") }
    [IO.File]::WriteAllLines($path, $lines, [Text.UTF8Encoding]::new($false))
}

try {
    $step = 'checking installation'
    if (-not (Test-Path -LiteralPath (Join-Path $root '.env'))) { throw 'Run install.ps1 first' }
    & docker compose version | Out-Null
    $repository = if ($Component -eq 'panel') { 'cangnan88/waha-panel' } else { 'devlikeapro/waha' }
    $step = 'reading public release tags'
    $payload = Invoke-RestMethod -Uri "https://hub.docker.com/v2/repositories/$repository/tags?page_size=100&ordering=last_updated" -TimeoutSec 15
    $candidates = foreach ($item in @($payload.results)) {
        $tag = [string]$item.name
        if ($Component -eq 'panel' -and $tag -match '^v?(\d+)\.(\d+)\.(\d+)$') {
            [PSCustomObject]@{ Tag = $tag; Version = [version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3]) }
        } elseif ($Component -eq 'waha' -and $tag -match '^latest-(\d{4})\.(\d{1,2})\.(\d{1,2})$') {
            [PSCustomObject]@{ Tag = $tag; Version = [version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3]) }
        }
    }
    $latest = $candidates | Sort-Object Version -Descending | Select-Object -First 1
    if ($null -eq $latest) { throw 'No stable release tag was found' }
    $tagName = if ($Component -eq 'panel') { 'PANEL_IMAGE' } else { 'WAHA_IMAGE' }
    $versionName = if ($Component -eq 'panel') { 'PANEL_VERSION' } else { 'WAHA_IMAGE_TAG' }
    $image = if ($Component -eq 'panel') { "docker.io/cangnan88/waha-panel:$($latest.Tag)" } else { "devlikeapro/waha:$($latest.Tag)" }
    $current = Get-EnvValue $versionName
    Write-Host "Component: $Component"
    Write-Host "Current:   $current"
    Write-Host "Latest:    $($latest.Tag)"
    if ($current -eq $latest.Tag) { Write-Host 'Already current.'; exit 0 }

    $backup = Join-Path $root ('.env.before-update-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Copy-Item -LiteralPath (Join-Path $root '.env') -Destination $backup
    try {
        $step = 'updating release selection'
        Set-EnvValue $tagName $image
        Set-EnvValue $versionName $latest.Tag
        $service = if ($Component -eq 'panel') { 'waha-panel' } else { 'waha' }
        $step = 'pulling selected image'
        & docker compose pull $service
        if ($LASTEXITCODE -ne 0) { throw 'docker compose pull failed' }
        $step = 'recreating selected service'
        & docker compose up -d --no-deps $service
        if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed' }
    } catch {
        Copy-Item -LiteralPath $backup -Destination (Join-Path $root '.env') -Force
        throw
    }
    Write-Host "Updated $Component to $($latest.Tag). Other service and persistent volumes were left unchanged."
} catch {
    Write-Error ("UPDATE FAILED at {0}: {1}" -f $step, $_.Exception.Message)
    exit 1
}
