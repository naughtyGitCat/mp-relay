# mp-relay Windows service uninstaller.
#
# Invoked by Inno Setup [UninstallRun] section. Idempotent — silent no-op
# if the service isn't installed.

param(
    [string]$InstallDir = $PSScriptRoot,
    [string]$ServiceName = "mp-relay"
)

$ErrorActionPreference = "Continue"

$nssm = Join-Path $InstallDir "nssm.exe"

$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Service '$ServiceName' not registered — nothing to uninstall."
    exit 0
}

Write-Host "Stopping service '$ServiceName'..."
if (Test-Path $nssm) {
    & $nssm stop $ServiceName 2>&1 | Out-Null
} else {
    # Fallback if nssm.exe was deleted before us
    Stop-Service $ServiceName -Force -ErrorAction SilentlyContinue
}
Start-Sleep 3

Write-Host "Removing service..."
if (Test-Path $nssm) {
    & $nssm remove $ServiceName confirm 2>&1 | Out-Null
} else {
    sc.exe delete $ServiceName | Out-Null
}

Write-Host "Done. Service '$ServiceName' removed."
