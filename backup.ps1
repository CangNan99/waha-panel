[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot)
Set-Location -LiteralPath $root
$step = 'checking installation'
try {
    if (-not (Test-Path -LiteralPath (Join-Path $root '.env'))) { throw 'Run install.ps1 first' }
    $prefixLine = Get-Content -LiteralPath (Join-Path $root '.env') | Where-Object { $_ -like 'PORTABLE_VOLUME_PREFIX=*' } | Select-Object -First 1
    $prefix = if ($prefixLine) { ($prefixLine -split '=', 2)[1] } else { 'waha-release' }
    $backupDir = Join-Path $root ('backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    & docker compose version | Out-Null
    foreach ($name in @('panel_data', 'sessions', 'media')) {
        $step = "backing up $name"
        $volume = "${prefix}_$name"
        $archive = "${name}.tar.gz"
        & docker run --rm "-v${volume}:/source:ro" "-v${backupDir}:/backup" busybox:1.36 sh -c "tar -czf /backup/$archive -C /source ."
        if ($LASTEXITCODE -ne 0) { throw "backup failed for $name" }
    }
    Write-Host "Backup created at $backupDir"
} catch {
    Write-Error ("BACKUP FAILED at {0}: {1}" -f $step, $_.Exception.Message)
    exit 1
}
