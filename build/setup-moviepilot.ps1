# Download + run the latest Windows-MoviePilot installer.
#
# Fetches the most-recent release of naughtyGitCat/Windows-MoviePilot
# from GitHub at runtime and spawns its installer. Lives outside the
# mp-relay installer (which used to bundle the .exe at build time --
# moved to download-on-demand to keep mp-relay's installer at ~56 MB
# and decouple from Windows-MoviePilot's release cadence).
#
# Idempotent: detects an already-running MoviePilot service and offers
# to skip. Both the in-app /setup wizard and the Start Menu shortcut
# call this script.

[CmdletBinding()]
param(
    [string]$InstallDir = $PSScriptRoot,
    [switch]$Silent,
    [string]$ServiceName = "MoviePilot-V2",
    [string]$Repo        = "naughtyGitCat/Windows-MoviePilot",
    [string]$DownloadDir = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"  # speeds up Invoke-WebRequest

if (-not $DownloadDir) {
    $DownloadDir = Join-Path $InstallDir "downloads"
}

Write-Host "=========================================="
Write-Host "  mp-relay -> MoviePilot setup"
Write-Host "=========================================="
Write-Host "Repo        : $Repo"
Write-Host "Download to : $DownloadDir"
Write-Host ""

# --- Step 1: detect existing install + offer to skip ---
$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "MoviePilot service '$ServiceName' is already registered (Status: $($existing.Status))."
    if (-not $Silent) {
        $reply = Read-Host "Run the installer anyway (will upgrade in place) [y/N]"
        if ($reply -ne 'y' -and $reply -ne 'Y') {
            Write-Host "Skipped. Configure MP_URL/MP_USER/MP_PASS in mp-relay's /setup or .env to point at this install."
            exit 0
        }
    }
}

# --- Step 2: query GitHub for the latest release asset ---
Write-Host "[1/3] Resolving latest Windows-MoviePilot release..."
$apiUrl = "https://api.github.com/repos/$Repo/releases/latest"
try {
    $release = Invoke-RestMethod -Uri $apiUrl -UseBasicParsing -Headers @{
        "User-Agent" = "mp-relay-setup-moviepilot"
        "Accept"     = "application/vnd.github+json"
    }
} catch {
    Write-Error "Failed to query GitHub releases API: $($_.Exception.Message)"
    Write-Error "Check internet connectivity and that the repo $Repo is public."
    exit 2
}

$asset = $release.assets | Where-Object { $_.name -like "MoviePilot-V2-Setup-*.exe" } | Select-Object -First 1
if (-not $asset) {
    Write-Error "No MoviePilot-V2-Setup-*.exe asset in latest release of $Repo (tag $($release.tag_name))."
    exit 3
}
$mb = [math]::Round($asset.size / 1MB, 1)
Write-Host "      tag    : $($release.tag_name)"
Write-Host "      asset  : $($asset.name) ($mb MB)"

# --- Step 3: download (skip if already present + size matches) ---
if (-not (Test-Path $DownloadDir)) {
    New-Item -ItemType Directory -Path $DownloadDir -Force | Out-Null
}
$installerPath = Join-Path $DownloadDir $asset.name

$needDownload = $true
if (Test-Path $installerPath) {
    $existingSize = (Get-Item $installerPath).Length
    if ($existingSize -eq $asset.size) {
        Write-Host "[2/3] Cached installer matches expected size, skipping download."
        $needDownload = $false
    } else {
        Write-Host "[2/3] Cached file size mismatch ($existingSize vs $($asset.size)), redownloading."
    }
}

if ($needDownload) {
    Write-Host "[2/3] Downloading $($asset.name) ($mb MB)..."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installerPath -UseBasicParsing
    $actualSize = (Get-Item $installerPath).Length
    if ($actualSize -ne $asset.size) {
        Write-Error "Download size mismatch: expected $($asset.size), got $actualSize. Delete $installerPath and retry."
        exit 4
    }
    Write-Host "      OK ($actualSize bytes)"
}

# --- Step 4: run the installer ---
Write-Host "[3/3] Launching MoviePilot installer (~1-2 min unpack)..."
$args = @("/SP-")
if ($Silent) {
    $args += @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
}
$proc = Start-Process -FilePath $installerPath -ArgumentList $args -Wait -PassThru
$rc = $proc.ExitCode

Write-Host ""
if ($rc -eq 0) {
    Write-Host "MoviePilot installer finished cleanly."
    Start-Sleep 2
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host "Service '$ServiceName': $($svc.Status)"
    } else {
        Write-Host "(Service '$ServiceName' not detected -- the user may have unchecked service mode in MoviePilot's wizard.)"
    }
    Write-Host ""
    Write-Host "Next:"
    Write-Host "  1. Open http://localhost:3000 -> create admin account"
    Write-Host "  2. In mp-relay /setup -> 'MoviePilot' card -> enter URL + creds + 'Test connection'"
    exit 0
} else {
    Write-Warning "MoviePilot installer exited with code $rc"
    Write-Warning "User may have cancelled the wizard; or check %TEMP%\Setup Log* for details."
    exit $rc
}
