# mp-relay → mdcx lazy-setup bootstrap.
#
# Invoked once after first install (manually, or via the [Run] postinstall
# task in build.iss). Bootstraps everything mdcx needs:
#
#   1. uv (Python+package manager) — installs to %USERPROFILE%\.local\bin
#      if missing
#   2. mdcx source — cloned from the user's fork at naughtyGitCat/mdcx
#      (configurable via -MdcxRepo). Falls back to GitHub zipball if git
#      isn't on PATH.
#   3. Python 3.13.4+ runtime — uv auto-installs the right version via
#      ``uv python install`` triggered by ``uv sync``
#   4. mdcx Python deps (~250 MB: pyqt5, av, lxml, openai, curl-cffi, etc.)
#   5. patchright + headless Chromium — *the* requirement that makes mdcx
#      able to bypass JavBus's Cloudflare driver-verify wall. Browsers
#      land at ``$InstallDir\mdcx\browsers\`` (override of the default
#      ``%USERPROFILE%\.cache\ms-playwright``) so an uninstall takes
#      everything with it.
#   6. mp-relay .env — patches MDCX_DIR / MDCX_PYTHON / MDCX_MODULE to
#      point at the bundle, then restarts the mp-relay service so the
#      new config takes effect.
#
# Idempotent: re-running detects an existing mdcx tree and does
# ``git pull`` + ``uv sync`` + chromium re-check (cheap if up-to-date).

[CmdletBinding()]
param(
    [string]$InstallDir = $PSScriptRoot,
    [string]$MdcxRepo   = "https://github.com/naughtyGitCat/mdcx.git",
    [string]$MdcxRef    = "master",
    [string]$ServiceName = "mp-relay",
    [switch]$SkipChromium,
    [switch]$NoServiceRestart
)

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"  # speeds up Invoke-WebRequest dramatically

$mdcxDir   = Join-Path $InstallDir "mdcx"
$envFile   = Join-Path $InstallDir ".env"
$browsersDir = Join-Path $mdcxDir "browsers"

Write-Host "=========================================="
Write-Host "  mp-relay → mdcx setup"
Write-Host "=========================================="
Write-Host "Install dir : $InstallDir"
Write-Host "mdcx target : $mdcxDir"
Write-Host "Repo / ref  : $MdcxRepo @ $MdcxRef"
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1: ensure uv
# ---------------------------------------------------------------------------
function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        "$env:LOCALAPPDATA\uv\uv.exe"
    )) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$uv = Find-Uv
if (-not $uv) {
    Write-Host "[1/6] Installing uv (Astral) — Python+package manager…"
    # Official one-liner. Lands uv in $env:USERPROFILE\.local\bin (PATH for
    # current session is patched by the installer, but new shells need a
    # session restart — we resolve below explicitly so this script keeps
    # working without restart.)
    $uvInstall = Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1"
    Invoke-Expression $uvInstall
    $uv = Find-Uv
    if (-not $uv) {
        throw "uv install reported success but binary not found. Check $env:USERPROFILE\.local\bin"
    }
    Write-Host "      → $uv"
} else {
    Write-Host "[1/6] uv already present: $uv"
}

# ---------------------------------------------------------------------------
# Step 2: clone or update mdcx
# ---------------------------------------------------------------------------
$git = Get-Command git -ErrorAction SilentlyContinue
$existingClone = Test-Path (Join-Path $mdcxDir ".git")

if ($existingClone) {
    Write-Host "[2/6] mdcx already cloned — updating to $MdcxRef…"
    if (-not $git) { throw "git not on PATH but $mdcxDir is a git checkout. Install git or remove the dir to start clean." }
    & git -C $mdcxDir fetch origin --quiet
    & git -C $mdcxDir checkout $MdcxRef --quiet
    & git -C $mdcxDir pull --ff-only --quiet
} elseif ($git) {
    Write-Host "[2/6] Cloning mdcx via git…"
    if (Test-Path $mdcxDir) { Remove-Item $mdcxDir -Recurse -Force }
    & git clone --depth 1 --branch $MdcxRef $MdcxRepo $mdcxDir --quiet
} else {
    Write-Host "[2/6] git not found — falling back to GitHub zipball download…"
    if (Test-Path $mdcxDir) { Remove-Item $mdcxDir -Recurse -Force }
    New-Item -ItemType Directory -Path $mdcxDir -Force | Out-Null

    # Strip .git from the URL: https://github.com/foo/bar.git → foo/bar
    $repoSlug = $MdcxRepo -replace 'https?://github\.com/', '' -replace '\.git$', ''
    $zipUrl   = "https://github.com/$repoSlug/archive/refs/heads/$MdcxRef.zip"
    $zipPath  = Join-Path $env:TEMP "mdcx-zip-$(Get-Random).zip"
    $extract  = Join-Path $env:TEMP "mdcx-extract-$(Get-Random)"
    Write-Host "      $zipUrl"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $extract -Force
    # Zipball extracts as <reposlug>-<branch>/* — flatten one level
    $top = Get-ChildItem $extract -Directory | Select-Object -First 1
    Get-ChildItem $top.FullName -Force | ForEach-Object {
        Move-Item $_.FullName -Destination $mdcxDir -Force
    }
    Remove-Item $zipPath -Force
    Remove-Item $extract -Recurse -Force
    Write-Host "      Note: no git history, future updates require re-running this script."
}

# Sanity: pyproject.toml present?
if (-not (Test-Path (Join-Path $mdcxDir "pyproject.toml"))) {
    throw "mdcx clone/extract didn't produce pyproject.toml at $mdcxDir — repo layout changed?"
}

# ---------------------------------------------------------------------------
# Step 3 + 4: uv sync (Python runtime + deps)
# ---------------------------------------------------------------------------
Write-Host "[3/6] Resolving Python 3.13 runtime + installing deps via uv sync…"
Push-Location $mdcxDir
try {
    # --no-dev skips pre-commit / pytest / pyqt5-stubs / etc — anything tagged
    # as a dev/test dep, since the bundled mdcx is invoked headless via CLI.
    # Network: 200-300 MB total (Python 3.13 ~30 MB, deps ~250 MB).
    & $uv sync --no-dev
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE)" }
} finally {
    Pop-Location
}

# Resolve the venv python created by uv. uv defaults to .venv next to pyproject.
$mdcxPython = Join-Path $mdcxDir ".venv\Scripts\python.exe"
if (-not (Test-Path $mdcxPython)) {
    throw "Expected venv at $mdcxPython after uv sync — check uv version / sync output"
}
Write-Host "      → $mdcxPython"

# ---------------------------------------------------------------------------
# Step 5: patchright + chromium
# ---------------------------------------------------------------------------
if ($SkipChromium) {
    Write-Host "[5/6] -SkipChromium passed — leaving browsers uninstalled. JavBus / sites that use driver-verify will fail."
} else {
    Write-Host "[5/6] Installing patchright Chromium (~150 MB to $browsersDir)…"
    $env:PLAYWRIGHT_BROWSERS_PATH = $browsersDir
    Push-Location $mdcxDir
    try {
        # ``patchright install chromium`` downloads to PLAYWRIGHT_BROWSERS_PATH.
        # We use the venv's python to make sure we hit the patchright we just
        # installed, not a stale playwright from elsewhere on PATH.
        & $mdcxPython -m patchright install chromium
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "patchright install chromium exited with $LASTEXITCODE — JavBus scrapes may fail."
            Write-Warning "Re-run setup-mdcx.ps1 later, or run manually: cd $mdcxDir && .venv\Scripts\python.exe -m patchright install chromium"
        }
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------------------
# Step 6: detect CLI entry + patch mp-relay .env
# ---------------------------------------------------------------------------
Write-Host "[6/6] Wiring mp-relay → mdcx via .env…"

# mp-relay calls ``python -m <MDCX_MODULE>``. Auto-detect which entry actually
# exists in the cloned mdcx — the LLM-friendly ``mdcx.cmd.main`` wrapper isn't
# yet on upstream master at the time of writing, but ``mdcx.cmd.crawl``
# (typer-based) is and exposes the same scrape verbs.
$candidates = @("mdcx.cmd.main", "mdcx.cmd.crawl")
$mdcxModule = $null
foreach ($mod in $candidates) {
    Push-Location $mdcxDir
    try {
        $null = & $mdcxPython -m $mod --help 2>&1
        if ($LASTEXITCODE -eq 0) { $mdcxModule = $mod }
    } finally {
        Pop-Location
    }
    if ($mdcxModule) { break }
}

if (-not $mdcxModule) {
    Write-Warning "Neither $($candidates -join ' nor ') responded to --help. mp-relay won't be able to invoke mdcx."
    Write-Warning "Push your LLM-CLI patch (E:\mdcx-src\mdcx\cmd\main.py) to your fork's $MdcxRef branch and re-run this script."
    $mdcxModule = "mdcx.cmd.main"   # write what the user *wants* to point at, even if missing
}
Write-Host "      mdcx CLI module : $mdcxModule"

# Patch .env in-place via line-by-line edit. Avoids regex-replacement
# pitfalls with paths containing $ or backslashes. Line is replaced if the
# key already exists (anywhere, even commented-out variants are NOT touched
# — match anchors at start-of-line on uncommented assignments only); else
# the line is appended.
function Set-EnvKey([string[]]$lines, [string]$key, [string]$value) {
    $line = "$key=$value"
    $pat  = "^$([regex]::Escape($key))\s*="
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pat) {
            $lines[$i] = $line
            return $lines
        }
    }
    return $lines + $line
}

if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $InstallDir ".env.example") $envFile -Force
}
$envLines = @(Get-Content $envFile -Encoding UTF8)
$envLines = Set-EnvKey $envLines "MDCX_DIR"    $mdcxDir
$envLines = Set-EnvKey $envLines "MDCX_PYTHON" $mdcxPython
$envLines = Set-EnvKey $envLines "MDCX_MODULE" $mdcxModule
Set-Content -Path $envFile -Value $envLines -Encoding UTF8
Write-Host "      Patched: $envFile"
Write-Host "        MDCX_DIR    = $mdcxDir"
Write-Host "        MDCX_PYTHON = $mdcxPython"
Write-Host "        MDCX_MODULE = $mdcxModule"

# ---------------------------------------------------------------------------
# Restart the mp-relay service so the new config takes effect
# ---------------------------------------------------------------------------
if (-not $NoServiceRestart) {
    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host ""
        Write-Host "Restarting service '$ServiceName' to pick up new mdcx config…"
        Restart-Service $ServiceName -Force
        Start-Sleep 4
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:5000/health" -UseBasicParsing -TimeoutSec 5
            Write-Host "/health: $($r.Content)"
        } catch {
            Write-Warning "Service restarted but /health didn't respond: $($_.Exception.Message)"
            Write-Warning "Tail logs: $InstallDir\service-logs\stderr.log"
        }
    } else {
        Write-Host ""
        Write-Host "(Service '$ServiceName' not registered — start it manually or restart your launcher.)"
    }
}

Write-Host ""
Write-Host "=========================================="
Write-Host "  mdcx setup complete."
Write-Host "  open http://localhost:5000/health to verify mdcx is reachable"
Write-Host "=========================================="
