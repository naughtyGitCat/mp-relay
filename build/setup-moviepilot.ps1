# Run the bundled Windows-MoviePilot installer.
#
# The .exe was downloaded at CI build time from the latest release of
# naughtyGitCat/Windows-MoviePilot and shipped as part of mp-relay's
# install payload. This script just wraps the spawn so the user gets:
#
#   - A clean console showing what's about to happen
#   - Auto-detection of an existing install (skip if MoviePilot service
#     is already registered + running)
#   - Optional silent mode (-Silent) for unattended pipelines
#
# Re-runnable: the bundled .exe is itself idempotent (Inno Setup
# detects the previous AppId and offers upgrade-in-place).

[CmdletBinding()]
param(
    [string]$InstallDir = $PSScriptRoot,
    [switch]$Silent,
    [string]$ServiceName = "MoviePilot-V2"
)

$ErrorActionPreference = "Stop"

# Resolve the bundled installer. Inno Setup ships it under the install dir
# at the name we set in build.iss [Files].
$mpInstaller = Join-Path $InstallDir "MoviePilot-V2-Setup.exe"

Write-Host "=========================================="
Write-Host "  mp-relay -> MoviePilot setup"
Write-Host "=========================================="
Write-Host "Bundled installer: $mpInstaller"

if (-not (Test-Path $mpInstaller)) {
    Write-Error "Bundled MoviePilot installer not found at $mpInstaller. The mp-relay install may be incomplete."
    exit 1
}

# Detect an existing MoviePilot install and offer to skip.
$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host ""
    Write-Host "MoviePilot service '$ServiceName' is already registered (Status: $($existing.Status))."
    if (-not $Silent) {
        $reply = Read-Host "Run the bundled installer anyway? It will upgrade in place. [y/N]"
        if ($reply -ne 'y' -and $reply -ne 'Y') {
            Write-Host "Skipped. mp-relay's .env should already have MP_URL pointing at the existing install."
            exit 0
        }
    }
}

# Build Inno Setup CLI args. /SP- skips "This will install..." dialog;
# /SILENT shows only progress; /VERYSILENT suppresses everything.
# /SUPPRESSMSGBOXES auto-clicks the wizard's stock confirmations.
$args = @("/SP-")
if ($Silent) {
    $args += @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
}

Write-Host ""
Write-Host "Launching MoviePilot installer (about 124 MB unpack, takes 1-2 min)..."
Write-Host "  args: $($args -join ' ')"

# Start-Process so we can wait + capture exit code.
$proc = Start-Process -FilePath $mpInstaller -ArgumentList $args -Wait -PassThru
$rc = $proc.ExitCode

Write-Host ""
if ($rc -eq 0) {
    Write-Host "MoviePilot installer finished cleanly."
    # Best-effort verification: was a service installed + is it listening?
    Start-Sleep 2
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host "Service '$ServiceName': $($svc.Status)"
    } else {
        Write-Host "(Service '$ServiceName' not detected -- may not have been the wizard's service-mode pick.)"
    }
    Write-Host ""
    Write-Host "Next: open http://localhost:3000 and create the admin account."
    Write-Host "Then update mp-relay .env with MP_URL/MP_USER/MP_PASS via /setup or notepad."
    exit 0
} else {
    Write-Warning "MoviePilot installer exited with code $rc"
    Write-Warning "Possible causes: user cancelled the wizard, install path conflict, missing prerequisites."
    exit $rc
}
