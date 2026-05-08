# mp-relay Windows service installer.
#
# Invoked by Inno Setup [Run] section, but also runnable manually if the
# Service-mode task was unchecked at install time:
#   powershell -ExecutionPolicy Bypass -File service-install.ps1 -InstallDir "C:\Program Files (x86)\mp-relay"
#
# Idempotent: stops + removes any existing mp-relay service before reinstall.

param(
    [string]$InstallDir = $PSScriptRoot,
    [string]$ServiceName = "mp-relay",
    [int]$Port = 5000
)

$ErrorActionPreference = "Continue"

$nssm    = Join-Path $InstallDir "nssm.exe"
$python  = Join-Path $InstallDir "Python\python.exe"
$appDir  = $InstallDir
$logDir  = Join-Path $InstallDir "service-logs"
$envFile = Join-Path $InstallDir ".env"

if (-not (Test-Path $nssm))   { Write-Error "nssm.exe not found at $nssm";    exit 1 }
if (-not (Test-Path $python)) { Write-Error "python.exe not found at $python"; exit 1 }
if (-not (Test-Path (Join-Path $InstallDir "app\main.py"))) {
    Write-Error "app\main.py not found — install may be corrupt"; exit 1
}

# Friendly hint if .env still has the placeholder password — service will start
# but health probes will fail until config is filled in.
if (Test-Path $envFile) {
    $envContent = Get-Content $envFile -Raw
    if ($envContent -match "change-me") {
        Write-Warning ".env still contains 'change-me' placeholder passwords."
        Write-Warning "Service will start but qBT / MoviePilot logins will fail until you edit:"
        Write-Warning "  $envFile"
    }
}

# Idempotent reinstall: stop + remove first
$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Stopping existing service..."
    & $nssm stop $ServiceName 2>&1 | Out-Null
    Start-Sleep 3
    & $nssm remove $ServiceName confirm 2>&1 | Out-Null
    Start-Sleep 1
}

# Free the port if any orphan still holds it.
Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$stdoutLog = Join-Path $logDir "stdout.log"
$stderrLog = Join-Path $logDir "stderr.log"

Write-Host "Installing service '$ServiceName'..."
# uvicorn entrypoint — same as deploy/install-on-windows.ps1 used pre-installer.
& $nssm install $ServiceName $python "-m" "uvicorn" "app.main:app" "--host" "0.0.0.0" "--port" $Port
& $nssm set $ServiceName AppDirectory $appDir
& $nssm set $ServiceName AppEnvironmentExtra "PYTHONUNBUFFERED=1" "PYTHONIOENCODING=utf-8"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout $stdoutLog
& $nssm set $ServiceName AppStderr $stderrLog
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateOnline 1
& $nssm set $ServiceName AppRotateBytes 10485760
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 5000
& $nssm set $ServiceName DisplayName "mp-relay (magnet → MoviePilot/mdcx dispatcher)"
& $nssm set $ServiceName Description "Receives magnet links and media names; dispatches to MoviePilot for regular media or qBT+mdcx for JAV. Single-input web UI on port $Port."

Write-Host "Starting service..."
& $nssm start $ServiceName

Start-Sleep 5
$svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host "Service '$ServiceName' is running. Open http://127.0.0.1:$Port to access."
    # Best-effort health check — soft-fail so install doesn't block.
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
        Write-Host "/health: $($r.Content)"
    } catch {
        Write-Host "Service is running but /health didn't respond yet — give it a few seconds."
    }
    exit 0
} else {
    Write-Warning "Service installed but did not start cleanly. Check logs at $logDir"
    exit 1
}
