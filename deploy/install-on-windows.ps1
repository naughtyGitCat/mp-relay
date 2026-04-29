<#
.SYNOPSIS
    One-shot installer for mp-relay on Windows. Idempotent — safe to re-run.

.PARAMETER Source
    Path to the freshly-uploaded source tree (e.g. C:\mp-relay-tmp).
    If omitted and Target already has the source, just (re)installs the service.

.PARAMETER Target
    Final installation directory. Default C:\mp-relay.

.PARAMETER PythonExe
    Path to system Python 3.11+ executable used to create the venv.
    Default: auto-detect.

.PARAMETER Nssm
    Path to nssm.exe. Default: reuse the one from MoviePilot install.
#>
[CmdletBinding()]
param(
    [string]$Source = "",
    [string]$Target = "C:\mp-relay",
    [string]$PythonExe = "",
    [string]$Nssm = "C:\Program Files (x86)\MoviePilot\nssm.exe"
)

$ErrorActionPreference = "Stop"
$ServiceName = "mp-relay"

function Find-Python {
    foreach ($candidate in @(
        "C:\Program Files\Python312\python.exe",
        "C:\Program Files\Python311\python.exe",
        "C:\Python312\python.exe"
    )) {
        if (Test-Path $candidate) { return $candidate }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "No Python 3.11+ found. Pass -PythonExe explicitly."
}

# 1. Move source if Source given
if ($Source -and (Test-Path $Source)) {
    if (Test-Path $Target) {
        # Preserve config + state across redeploys
        $preserve = @{}
        foreach ($f in @(".env", "state.db", "state.db-wal", "state.db-shm", "service-stdout.log", "service-stderr.log")) {
            $p = Join-Path $Target $f
            if (Test-Path $p) {
                $tmp = Join-Path $env:TEMP ("mp-relay-preserve-" + (Get-Random) + "-" + $f)
                Move-Item $p $tmp -Force
                $preserve[$f] = $tmp
            }
        }
        # Remove old files except .venv (re-install deps via pip below)
        Get-ChildItem $Target -Force | Where-Object { $_.Name -ne ".venv" } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        # Copy new source in
        Get-ChildItem $Source -Force | ForEach-Object {
            Copy-Item $_.FullName -Destination $Target -Recurse -Force
        }
        # Restore preserved files
        foreach ($kv in $preserve.GetEnumerator()) {
            Move-Item $kv.Value -Destination (Join-Path $Target $kv.Key) -Force
        }
        Remove-Item $Source -Recurse -Force
        Write-Host "Updated source at $Target (preserved .env + state.db)"
    } else {
        Move-Item $Source $Target
        Write-Host "Installed source at $Target"
    }
}

if (-not (Test-Path $Target)) {
    throw "Target $Target does not exist and no -Source provided"
}

# 2. .env
$envFile = Join-Path $Target ".env"
if (-not (Test-Path $envFile)) {
    $envExample = Join-Path $Target ".env.example"
    if (Test-Path $envExample) {
        Copy-Item $envExample $envFile
        Write-Host "[!] Created $envFile from .env.example — REVIEW AND EDIT BEFORE STARTING SERVICE"
    } else {
        throw "$envFile is missing and no .env.example found"
    }
}

# 3. venv
$venv = Join-Path $Target ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    if (-not $PythonExe) { $PythonExe = Find-Python }
    Write-Host "Creating venv with $PythonExe"
    & $PythonExe -m venv $venv
}

# 4. pip install
Write-Host "Installing requirements"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $Target "requirements.txt") --quiet

# 5. NSSM service
if (-not (Test-Path $Nssm)) {
    throw "nssm.exe not found at $Nssm. Pass -Nssm explicitly."
}

if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "Stopping existing service"
    & $Nssm stop $ServiceName 2>&1 | Out-Null
    Start-Sleep 2
    & $Nssm remove $ServiceName confirm 2>&1 | Out-Null
}

Write-Host "Registering service $ServiceName"
& $Nssm install $ServiceName $venvPython "-m" "uvicorn" "app.main:app" "--host" "0.0.0.0" "--port" "5000"
& $Nssm set $ServiceName AppDirectory $Target | Out-Null
& $Nssm set $ServiceName AppEnvironmentExtra "PYTHONUNBUFFERED=1" "PYTHONIOENCODING=utf-8" | Out-Null
& $Nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $Nssm set $ServiceName AppStdout (Join-Path $Target "service-stdout.log") | Out-Null
& $Nssm set $ServiceName AppStderr (Join-Path $Target "service-stderr.log") | Out-Null
& $Nssm set $ServiceName AppRotateFiles 1 | Out-Null
& $Nssm set $ServiceName AppRotateBytes 10485760 | Out-Null
& $Nssm set $ServiceName AppExit Default Restart | Out-Null
& $Nssm set $ServiceName AppRestartDelay 5000 | Out-Null
& $Nssm set $ServiceName DisplayName "mp-relay (magnet → MoviePilot/mdcx dispatcher)" | Out-Null
& $Nssm set $ServiceName Description "Receives magnet links / media names; dispatches to MoviePilot for regular media or qBT+mdcx for JAV." | Out-Null

Write-Host "Starting service"
& $Nssm start $ServiceName

# 6. Wait + verify
$start = Get-Date
$listening = $false
while (((Get-Date) - $start).TotalSeconds -lt 30) {
    Start-Sleep 2
    $conn = Get-NetTCPConnection -State Listen -LocalPort 5000 -ErrorAction SilentlyContinue
    if ($conn) {
        $listening = $true
        break
    }
}

if ($listening) {
    Write-Host "OK: mp-relay listening on :5000"
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:5000/health" -TimeoutSec 5
        Write-Host "Health: $($r.Content)"
    } catch {
        Write-Host "Could not query /health: $($_.Exception.Message)"
    }
} else {
    Write-Host "WARN: port 5000 not listening yet. Tail $Target\service-stderr.log to debug."
}
